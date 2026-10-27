"""SigenStor local dashboard collector.

Polls the SigenStor over Modbus TCP (read-only), keeps minute-level history in SQLite,
and serves the dashboard + a small JSON API.

Standard library only - runs anywhere Python 3.9+ runs (Docker, Raspberry Pi, NAS, PC).

Configuration (environment variables):
  SIGEN_HOST            inverter IP address                       (required unless SIGEN_SIMULATE=1)
  SIGEN_PORT            Modbus TCP port                           default 502
  SIGEN_PLANT_UNIT      plant Modbus unit id                      default 247
  SIGEN_INVERTER_UNIT   inverter Modbus unit id, 0 to skip        default 1
  SIGEN_SIMULATE        1 = run the built-in simulator instead    default 0
  POLL_SECONDS          seconds between polls (min 2)             default 5
  HTTP_PORT             dashboard/API port                        default 8080
  DATA_DIR              where the SQLite database lives           default ./data
  TZ                    local time zone for "today"               default Australia/Sydney
  DEMO_HISTORY_DAYS     days of simulated history to pre-fill     default 14 (simulator only)
  PRICE_PROVIDER        amber, localvolts or none                 default: whichever has a key
  AMBER_API_KEY         Amber Electric API key (optional)         app.amber.com.au/developers
  AMBER_SITE_ID         Amber site id, if you have more than one  default: first active site
  LOCALVOLTS_API_KEY    LocalVolts API key (optional)             localvolts.com, My Profile -> API Key
  LOCALVOLTS_PARTNER_ID LocalVolts Partner ID                     same page
  LOCALVOLTS_NMI        LocalVolts NMI, if you have more than one default: first on the account
  AEMO_REGION           NEM region for wholesale data             NSW1, QLD1, VIC1, SA1 or TAS1 (default NSW1)
  AEMO_ENABLED          0 to turn off AEMO market data            default 1
  SITE_LAT / SITE_LON   where the panels are, for weather and the solar forecast
  SITE_ADDRESS          or an address / suburb to look up instead (once, cached)
  SOLAR_KWP             array size, kW (helps the forecast before it has learned your system)
  SOLAR_PLANES          roof faces as tilt:azimuth:kW, comma separated; azimuth 0 = north, 90 = east
  WEATHER_ENABLED       0 to turn off weather                     default 1
  INSULATION_WARN_MOHM  insulation resistance "lower than usual" below this, MΩ   default 0.5
  INSULATION_MIN_MOHM   insulation fault below this (inverter's shutdown point)   default 0.37
  SIGEN_CHARGER_UNIT    Sigen AC charger's Modbus unit id         default auto (scan for it); 0 = none;
                        plant = charger has no Modbus address, use the inverter's figures only
"""
import json
import mimetypes
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_settings_file():
    """Read KEY=value lines from sigen.env in the data folder (or SETTINGS_FILE).

    Read on every start, so on a NAS you can edit the file and just restart the
    container. Values in the file override the container's environment.
    """
    data_dir = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    path = Path(os.environ.get("SETTINGS_FILE") or data_dir / "sigen.env")
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ[key] = value
    return path


SETTINGS_PATH = load_settings_file()
from aemo import Aemo  # noqa: E402
from charger import ChargerWatch  # noqa: E402
from weather import Weather  # noqa: E402
from outlook import Outlook  # noqa: E402
from money import Money  # noqa: E402
from importer import import_folder  # noqa: E402
from fuel import Fuel  # noqa: E402
from amber import Amber  # noqa: E402
from localvolts import LocalVolts  # noqa: E402
from modbus import DeviceReader, ModbusError, ModbusTCPClient  # noqa: E402
from registers import (EMS_WORK_MODE, INVERTER, MAX_PV_STRINGS, ON_OFF_GRID, PLANT,  # noqa: E402
                       RUNNING_STATE, decode_alarms)

VERSION = "1.9.1"
ENV = os.environ.get
SIMULATE = ENV("SIGEN_SIMULATE", "0") == "1"
HOST = ENV("SIGEN_HOST", "")
PORT = int(ENV("SIGEN_PORT", "502"))
PLANT_UNIT = int(ENV("SIGEN_PLANT_UNIT", "247"))
INVERTER_UNIT = int(ENV("SIGEN_INVERTER_UNIT", "1"))
POLL = max(2.0, float(ENV("POLL_SECONDS", "5")))
HTTP_PORT = int(ENV("HTTP_PORT", "8080"))
DATA_DIR = Path(ENV("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TZ = ZoneInfo(ENV("TZ", "Australia/Sydney") or "Australia/Sydney")
WEB_DIR = Path(ENV("WEB_DIR", Path(__file__).resolve().parent.parent / "web")).resolve()
DEMO_DAYS = int(ENV("DEMO_HISTORY_DAYS", "14"))
RETENTION_DAYS = int(ENV("RETENTION_DAYS", "800"))
AMBER_KEY = ENV("AMBER_API_KEY", "").strip()
AMBER_SITE = ENV("AMBER_SITE_ID", "").strip() or None
LV_KEY = ENV("LOCALVOLTS_API_KEY", "").strip()
LV_PARTNER = ENV("LOCALVOLTS_PARTNER_ID", "").strip()
LV_NMI = ENV("LOCALVOLTS_NMI", "").strip() or None
PRICE_PROVIDER = (ENV("PRICE_PROVIDER", "") or "auto").strip().lower()
AEMO_REGION = (ENV("AEMO_REGION", "") or "NSW1").strip().upper()
AEMO_ENABLED = ENV("AEMO_ENABLED", "1") != "0"
AEMO_SIMULATE = ENV("AEMO_SIMULATE", "0") == "1"
CHARGER_SETTING = (ENV("SIGEN_CHARGER_UNIT", "") or "auto").strip()
def _env_num(name, default):
    v = (ENV(name, "") or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


INSULATION_MIN = _env_num("INSULATION_MIN_MOHM", 0.37)
SITE_LAT = _env_num("SITE_LAT", None)
SITE_LON = _env_num("SITE_LON", None)
SITE_ADDRESS = ENV("SITE_ADDRESS", "").strip()
SOLAR_KWP = _env_num("SOLAR_KWP", None)
SOLAR_PLANES = ENV("SOLAR_PLANES", "").strip()
WEATHER_ENABLED = ENV("WEATHER_ENABLED", "1") != "0"
INSULATION_WARN = max(INSULATION_MIN, _env_num("INSULATION_WARN_MOHM", 0.5))
# money: plan comparison and payback
COMPARE_PLANS = (ENV("COMPARE_PLANS", "") or "").strip().lower()
SYSTEM_COST = _env_num("SYSTEM_COST", None)
SYSTEM_INSTALLED = (ENV("SYSTEM_INSTALLED", "") or "").strip()
RETAILER_SUPPLY_C_DAY = _env_num("RETAILER_SUPPLY_C_DAY", None)
RETAILER_FEE_MONTHLY = _env_num("RETAILER_FEE_MONTHLY", None)
RETAILER_SINCE = (ENV("RETAILER_SINCE", "") or "").strip()
# the car against a fuel car (see fuel.py)
FUEL_SETTINGS = {k: (_env_num(k, None) if k in ("EV_KWH_PER_100KM", "COMPARE_L_PER_100KM", "FUEL_RADIUS_KM", "FUEL_PRICE_C_L")
                     else (ENV(k, None) if k == "FUEL_EXCLUDE_BRANDS" else (ENV(k, "") or "").strip()))
                 for k in ("EV_MODEL", "EV_KWH_PER_100KM", "COMPARE_VEHICLE", "COMPARE_L_PER_100KM", "FUEL_TYPE",
                           "FUEL_RADIUS_KM", "FUEL_EXCLUDE_BRANDS", "FUEL_PRICE_C_L", "FUELCHECK_API_KEY",
                           "FUELCHECK_API_SECRET", "FUELWATCH_SUBURB")}
INSULATION_CONFIRM = 3  # consecutive readings either side of the limit before it counts

COUNTERS = ["total_pv_kwh", "total_load_kwh", "total_import_kwh", "total_export_kwh",
            "total_charge_kwh", "total_discharge_kwh"]


def log(*a):
    print(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def local_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples(
              ts INTEGER PRIMARY KEY, day TEXT,
              pv REAL, load REAL, battery REAL, grid REAL, soc REAL,
              total_pv_kwh REAL, total_load_kwh REAL, total_import_kwh REAL,
              total_export_kwh REAL, total_charge_kwh REAL, total_discharge_kwh REAL);
            CREATE INDEX IF NOT EXISTS samples_day ON samples(day);
            CREATE TABLE IF NOT EXISTS events(
              ts INTEGER, kind TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS prices(
              start INTEGER, channel TEXT, end INTEGER, per_kwh REAL, spot REAL, renewables REAL,
              descriptor TEXT, spike TEXT, PRIMARY KEY(start, channel));
            CREATE TABLE IF NOT EXISTS string_samples(
              ts INTEGER, n INTEGER, v REAL, a REAL, PRIMARY KEY(ts, n));
            CREATE TABLE IF NOT EXISTS aemo_prices(
              start INTEGER, region TEXT, rrp REAL, demand REAL, PRIMARY KEY(start, region));
            CREATE TABLE IF NOT EXISTS ev_sessions(
              start INTEGER PRIMARY KEY, end INTEGER, start_kwh REAL, kwh REAL, grid_kwh REAL, cost REAL,
              peak_w REAL, charging_s INTEGER, states TEXT, priced INTEGER);
            CREATE TABLE IF NOT EXISTS ev_log(
              ts INTEGER, state INTEGER, code TEXT, power REAL, set_current REAL);
            CREATE INDEX IF NOT EXISTS ev_log_ts ON ev_log(ts);
            CREATE TABLE IF NOT EXISTS daily_stats(
              day TEXT, key TEXT, min REAL, max REAL, sum REAL, n INTEGER, PRIMARY KEY(day, key));
            CREATE TABLE IF NOT EXISTS amber_days(
              day TEXT PRIMARY KEY, import_kwh REAL, import_cost REAL, export_kwh REAL,
              export_cost REAL, estimated INTEGER);
            CREATE TABLE IF NOT EXISTS fuel_prices(
              day TEXT, fuel TEXT, sum_c REAL, n INTEGER, stations INTEGER, source TEXT, PRIMARY KEY(day, fuel));
            CREATE TABLE IF NOT EXISTS imported_days(
              day TEXT PRIMARY KEY, pv REAL, load REAL, ev REAL, charge REAL, discharge REAL,
              import REAL, export REAL, source TEXT);
            """)
            acols = {r[1] for r in self.db.execute("PRAGMA table_info(amber_days)")}
            if "avg_buy_c" not in acols:  # the day's average buy price (for days without minute readings)
                self.db.execute("ALTER TABLE amber_days ADD COLUMN avg_buy_c REAL")
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
            # buy_c/sell_c: retailer price (c/kWh, Amber sign) in force that minute; ev: AC charger W
            for c in ("buy_c", "sell_c", "ev"):
                if c not in cols:
                    self.db.execute(f"ALTER TABLE samples ADD COLUMN {c} REAL")
            self.db.commit()

    def empty(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0

    def insert(self, rows):
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO samples (ts, day, pv, load, battery, grid, soc, total_pv_kwh, total_load_kwh, "
                "total_import_kwh, total_export_kwh, total_charge_kwh, total_discharge_kwh, buy_c, sell_c, ev) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def save_prices(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def save_amber_days(self, days):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO amber_days (day, import_kwh, import_cost, export_kwh, "
                                "export_cost, estimated, avg_buy_c) VALUES (?,?,?,?,?,?,?)", [
                (d, v["import_kwh"], v["import_cost"], v["export_kwh"], v["export_cost"], int(v["estimated"]),
                 v.get("avg_buy_c")) for d, v in days.items()])
            self.db.commit()

    def amber_days_have(self, since):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT day FROM amber_days WHERE day>=?", (since,))}

    # ---- days imported from a mySigen export (before the collector was running)
    def save_imported_days(self, days, source=""):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO imported_days VALUES (?,?,?,?,?,?,?,?,?)", [
                (d, v.get("pv"), v.get("load"), v.get("ev"), v.get("charge"), v.get("discharge"), v.get("import"),
                 v.get("export"), source) for d, v in days.items()])
            self.db.commit()
        return len(days)

    def apply_bill(self, b):
        """Spread a retailer bill over its days that have no costs yet (see importer.py)."""
        with self.lock:
            known = {r[0]: r for r in self.db.execute(
                "SELECT day, import_cost, export_cost FROM amber_days WHERE day>=? AND day<=?", (b["start"], b["end"]))}
            imp = {r[0]: (r[1] or 0.0, r[2] or 0.0) for r in self.db.execute(
                "SELECT day, import, export FROM imported_days WHERE day>=? AND day<=?", (b["start"], b["end"]))}
        energy = b["energy_cost"] * 100 - sum(r[1] or 0 for r in known.values())     # cents
        credit = b["export_credit"] * 100 + sum(r[2] or 0 for r in known.values())   # cents (export_cost < 0 when paid)
        todo = {d: v for d, v in imp.items() if d not in known}
        ti, te = sum(v[0] for v in todo.values()), sum(v[1] for v in todo.values())
        days = {}
        for d, (i, e) in todo.items():
            days[d] = {"import_kwh": i, "import_cost": max(0.0, energy) * (i / ti if ti else 1 / len(todo)),
                       "export_kwh": e, "export_cost": -max(0.0, credit) * (e / te if te else 1 / len(todo)),
                       "estimated": True, "avg_buy_c": b.get("avg_buy_c")}
        if days:
            self.save_amber_days(days)
        return {"filled": len(days), "known": len(known)}

    def imported_days(self, since=""):
        with self.lock:
            rows = self.db.execute("SELECT day, pv, load, ev, charge, discharge, import, export FROM imported_days "
                                   "WHERE day>? ORDER BY day", (since,)).fetchall()
        return {r[0]: dict(zip(("pv", "load", "ev", "charge", "discharge", "import", "export"), r[1:])) for r in rows}

    # ---- fuel prices (for the car's fuel equivalent)
    def save_fuel_price(self, day, fuel, price_c, stations, source):
        """Add a reading to the day's average for that fuel."""
        with self.lock:
            self.db.execute("INSERT INTO fuel_prices VALUES (?,?,?,1,?,?) ON CONFLICT(day, fuel) DO UPDATE SET "
                            "sum_c=sum_c+excluded.sum_c, n=n+1, stations=excluded.stations, source=excluded.source",
                            (day, fuel, price_c, stations, source))
            self.db.commit()

    def fuel_price(self, day, fuel):
        with self.lock:
            r = self.db.execute("SELECT sum_c/n FROM fuel_prices WHERE day=? AND fuel=?", (day, fuel)).fetchone()
        return r[0] if r else None

    def fuel_price_nearest(self, day, fuel):
        """The recorded price closest in time (before, else after) to a day."""
        with self.lock:
            r = (self.db.execute("SELECT sum_c/n FROM fuel_prices WHERE day<? AND fuel=? ORDER BY day DESC LIMIT 1",
                                 (day, fuel)).fetchone() or
                 self.db.execute("SELECT sum_c/n FROM fuel_prices WHERE day>? AND fuel=? ORDER BY day LIMIT 1",
                                 (day, fuel)).fetchone())
        return r[0] if r else None

    def avg_sell_c(self, t0, t1):
        """Average export price you'd have earned (c/kWh, positive = paid) between two times."""
        with self.lock:
            r = self.db.execute("SELECT AVG(-sell_c) FROM samples WHERE ts>=? AND ts<=? AND sell_c IS NOT NULL",
                                (int(t0), int(t1))).fetchone()
        return r[0] if r and r[0] is not None else None

    def day_avg_buy_c(self, day):
        with self.lock:
            r = self.db.execute("SELECT AVG(buy_c), COUNT(buy_c) FROM samples WHERE day=? AND buy_c IS NOT NULL",
                                (day,)).fetchone()
            if r and r[1] and r[1] > 600:
                return r[0]
            a = self.db.execute("SELECT avg_buy_c FROM amber_days WHERE day=?", (day,)).fetchone()
        return a[0] if a and a[0] is not None else None

    def imported_day(self, day):
        with self.lock:
            r = self.db.execute("SELECT load, ev, import, export FROM imported_days WHERE day=?", (day,)).fetchone()
        return {"load": r[0] or 0, "ev": r[1] or 0, "import": r[2] or 0, "export": r[3] or 0} if r else None

    def first_day(self):
        with self.lock:
            a = self.db.execute("SELECT MIN(day) FROM imported_days").fetchone()[0]
            b = self.db.execute("SELECT MIN(day) FROM samples").fetchone()[0]
        return min(x for x in (a, b) if x) if (a or b) else None

    def recorded_minutes(self, day):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM samples WHERE day=? AND total_import_kwh IS NOT NULL",
                                   (day,)).fetchone()[0]

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()

    def save_strings(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO string_samples VALUES (?,?,?,?)", rows)
            self.db.commit()

    def strings(self, hours):
        since = int(time.time() - hours * 3600)
        with self.lock:
            return self.db.execute(
                "SELECT ts, n, v, a FROM string_samples WHERE ts>=? ORDER BY ts, n", (since,)).fetchall()

    def string_history(self, hours):
        """Per-string power, voltage and current, averaged into ~480 points over the range."""
        since = int(time.time() - hours * 3600)
        bucket = max(60, int(hours * 3600 / 480) // 60 * 60)
        with self.lock:
            rows = self.db.execute(
                "SELECT (ts/?)*? AS b, n, AVG(v*a), AVG(v), AVG(a) FROM string_samples WHERE ts>=? "
                "GROUP BY b, n ORDER BY b", (bucket, bucket, since)).fetchall()
        times = sorted({r[0] for r in rows})
        idx = {t: i for i, t in enumerate(times)}
        strings = {}
        for b, n, w, v, a in rows:
            s = strings.setdefault(n, {"w": [None] * len(times), "v": [None] * len(times), "a": [None] * len(times)})
            i = idx[b]
            s["w"][i], s["v"][i], s["a"][i] = round(w), round(v, 1), round(a, 2)
        return {"t": times, "bucket_seconds": bucket, "strings": strings}

    def string_day(self, day):
        """Energy, peak power and peak time per string for one local day."""
        start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())
        with self.lock:
            rows = self.db.execute(
                "SELECT n, SUM(v*a)/60000.0, MAX(v*a), MAX(v) FROM string_samples WHERE ts>=? AND ts<? GROUP BY n",
                (start, start + 86400)).fetchall()
            peaks = {n: t for n, t in self.db.execute(
                "SELECT s.n, s.ts FROM string_samples s JOIN (SELECT n, MAX(v*a) AS m FROM string_samples "
                "WHERE ts>=? AND ts<? GROUP BY n) p ON s.n=p.n AND s.v*s.a=p.m WHERE s.ts>=? AND s.ts<?",
                (start, start + 86400, start, start + 86400)).fetchall()}
        return [{"string": n, "kwh": round(kwh or 0, 2), "peak_w": round(pw or 0), "peak_at": peaks.get(n),
                 "max_v": round(mv or 0, 1)} for n, kwh, pw, mv in rows]

    # ---- AC charger
    EV_COLS = ["start", "end", "start_kwh", "kwh", "grid_kwh", "cost", "peak_w", "charging_s", "states", "priced"]

    def open_ev_session(self):
        with self.lock:
            r = self.db.execute(f"SELECT {','.join(self.EV_COLS)} FROM ev_sessions WHERE end IS NULL "
                                "ORDER BY start DESC LIMIT 1").fetchone()
        return dict(zip(self.EV_COLS, r)) if r else None

    def save_ev_session(self, s):
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO ev_sessions ({','.join(self.EV_COLS)}) VALUES "
                            f"({','.join('?' * len(self.EV_COLS))})", [s.get(c) for c in self.EV_COLS])
            self.db.commit()

    def ev_log(self, ts, state, code, power, set_current):
        with self.lock:
            self.db.execute("INSERT INTO ev_log VALUES (?,?,?,?,?)", (int(ts), state, code, power, set_current))
            self.db.commit()

    def last_ev_state(self):
        with self.lock:
            return self.db.execute("SELECT ts, state FROM ev_log ORDER BY ts DESC LIMIT 1").fetchone()

    def ev_sessions(self, days):
        since = int(time.time() - days * 86400)
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(self.EV_COLS)} FROM ev_sessions WHERE start>=? OR end IS NULL "
                                   "ORDER BY start DESC", (since,)).fetchall()
        out = []
        for r in rows:
            s = dict(zip(self.EV_COLS, r))
            s.pop("start_kwh", None)
            s["cost"] = round(s["cost"], 2) if s.pop("priced") else None
            s["kwh"], s["grid_kwh"] = round(s["kwh"] or 0, 2), round(s["grid_kwh"] or 0, 2)
            s["peak_w"] = round(s["peak_w"] or 0)
            out.append(s)
        return out

    def ev_states(self, days):
        since = int(time.time() - days * 86400)
        with self.lock:
            return self.db.execute("SELECT ts, state, code, power, set_current FROM ev_log WHERE ts>=? ORDER BY ts",
                                   (since,)).fetchall()

    def ev_day(self, day):
        with self.lock:
            r = self.db.execute("SELECT SUM(ev)/60000.0, COUNT(ev) FROM samples WHERE day=?", (day,)).fetchone()
        return round(r[0] or 0, 2) if r and r[1] else None

    def hourly_pv(self, since, until):
        """{hour start: average PV W} for whole hours with readings for most of the hour."""
        with self.lock:
            rows = self.db.execute("SELECT (ts/3600)*3600 AS h, AVG(pv), COUNT(*) FROM samples WHERE ts>=? AND ts<? "
                                   "GROUP BY h", (int(since), int(until))).fetchall()
        return {h: w for h, w, n in rows if n >= 45 and w is not None}

    def hourly_avgs(self, since, until):
        """{hour start: (average PV W, average home W without the car)} for well-covered hours."""
        with self.lock:
            rows = self.db.execute(
                "SELECT (ts/3600)*3600 AS h, AVG(pv), AVG(MAX(0, load - COALESCE(ev, 0))), COUNT(*) FROM samples "
                "WHERE ts>=? AND ts<? GROUP BY h", (int(since), int(until))).fetchall()
        return {h: (pv, home) for h, pv, home, n in rows if n >= 45}

    def save_aemo(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO aemo_prices VALUES (?,?,?,?)", rows)
            self.db.commit()

    def aemo_history(self, region, hours, bucket=300):
        since = int(time.time() - hours * 3600)
        with self.lock:
            rows = self.db.execute(
                "SELECT (start/?)*? AS b, AVG(rrp), AVG(demand) FROM aemo_prices WHERE region=? AND start>=? "
                "GROUP BY b ORDER BY b", (bucket, bucket, region, since)).fetchall()
        return [{"t": b, "rrp": round(r, 2), "price_c": round(r / 10, 2),
                 "demand_mw": round(d) if d is not None else None} for b, r, d in rows]

    # ---- money: hourly grid flows, battery trades, cost without solar
    def hourly_flows(self, day):
        """[(hour start, import kWh, export kWh, home + car kWh)] for a local day, from the meter counters."""
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, total_import_kwh, total_export_kwh, total_load_kwh FROM samples WHERE day=? "
                "AND total_import_kwh IS NOT NULL ORDER BY ts", (day,)).fetchall()
        hours, prev = {}, None
        for ts, imp, exp, load in rows:
            if prev and ts - prev[0] <= 6 * 3600:
                h = hours.setdefault(ts // 3600 * 3600, [0.0, 0.0, 0.0])
                for i, (a, b) in enumerate(((imp, prev[1]), (exp, prev[2]), (load, prev[3]))):
                    d = (a or 0) - (b or 0)
                    if 0 <= d <= 50:  # skip counter resets and gaps
                        h[i] += d
            prev = (ts, imp, exp, load)
        return [(h, *v) for h, v in sorted(hours.items())]

    def trade_rows(self, day):
        with self.lock:
            return self.db.execute("SELECT ts, battery, grid, buy_c, sell_c FROM samples WHERE day=? ORDER BY ts",
                                   (day,)).fetchall()

    def no_solar_cost(self, day):
        """$ the home and car would have cost from the grid at the retailer's price each minute
        (or, for imported days, at the day's average price)."""
        # each hour's home and car energy (from the meter counters, so gaps in the readings don't
        # lose energy) at that hour's average buy price
        flows = self.hourly_flows(day)
        with self.lock:
            prices = dict(self.db.execute("SELECT (ts/3600)*3600, AVG(buy_c) FROM samples WHERE day=? AND buy_c IS NOT NULL "
                                          "GROUP BY 1", (day,)).fetchall())
        if flows and len(prices) >= 20:  # prices for nearly every hour
            avg = sum(prices.values()) / len(prices)
            return sum(load * prices.get(h, avg) for h, imp, exp, load in flows) / 100
        # otherwise (an imported day): the day's home and car use at that day's average buy price
        with self.lock:
            imp = self.db.execute("SELECT load, ev FROM imported_days WHERE day=?", (day,)).fetchone()
            a = self.db.execute("SELECT avg_buy_c FROM amber_days WHERE day=?", (day,)).fetchone()
        if imp and a and a[0] is not None:
            return ((imp[0] or 0) + (imp[1] or 0)) * a[0] / 100
        return None

    def cost_for_day(self, day):
        """Estimated $ from our own minute readings x the Amber price in force at the time."""
        with self.lock:
            r = self.db.execute(
                "SELECT SUM(MAX(grid,0)/60000.0*buy_c), SUM(MAX(-grid,0)/60000.0*sell_c), COUNT(buy_c) "
                "FROM samples WHERE day=? AND buy_c IS NOT NULL", (day,)).fetchone()
        if not r or not r[2]:
            return None
        bought, sold = (r[0] or 0) / 100, (r[1] or 0) / 100  # sold is negative when paid
        return {"bought": round(bought, 2), "earned": round(-sold, 2), "net": round(bought + sold, 2)}

    def price_history(self, hours):
        since = int(time.time() - hours * 3600)
        with self.lock:
            rows = self.db.execute(
                "SELECT (start/1800)*1800 AS b, channel, AVG(per_kwh) FROM prices WHERE start>=? "
                "GROUP BY b, channel ORDER BY b", (since,)).fetchall()
        out = {}
        for b, ch, v in rows:
            row = out.setdefault(b, {"t": b, "buy": None, "sell": None})
            if ch == "general":
                row["buy"] = round(v, 2)
            elif ch == "feedIn":
                row["sell"] = round(-v, 2)
        return [out[k] for k in sorted(out)]

    def event(self, kind, message):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (int(time.time()), kind, message))
            self.db.commit()

    def events(self, limit=30):
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, kind, message FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]

    def prune(self):
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.db.commit()

    def day_start_counters(self, day):
        with self.lock:
            row = self.db.execute(
                f"SELECT {','.join(COUNTERS)} FROM samples WHERE day=? AND total_pv_kwh IS NOT NULL "
                "ORDER BY ts LIMIT 1", (day,)).fetchone()
            prev = self.db.execute(
                f"SELECT {','.join(COUNTERS)} FROM samples WHERE day<? AND total_pv_kwh IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1", (day,)).fetchone()
        # Prefer the last reading of the previous day (no gap at midnight)
        return dict(zip(COUNTERS, prev or row)) if (prev or row) else None

    def history(self, hours):
        since = int(time.time() - hours * 3600)
        bucket = max(60, int(hours * 3600 / 480) // 60 * 60)
        with self.lock:
            rows = self.db.execute(
                "SELECT (ts/?)*? AS b, AVG(pv), AVG(MAX(0, load - COALESCE(ev, 0))), AVG(battery), AVG(grid), "
                "AVG(soc), AVG(ev) FROM samples WHERE ts>=? GROUP BY b ORDER BY b", (bucket, bucket, since)).fetchall()
        keys = ["t", "pv", "load", "battery", "grid", "soc", "ev"]
        out = {k: [] for k in keys}
        for r in rows:
            for k, v in zip(keys, r):
                out[k].append(round(v, 1) if isinstance(v, float) else v)
        out["bucket_seconds"] = bucket
        if not any(v is not None for v in out["ev"]):
            del out["ev"]  # no charger: keep the chart to the four series
        return out

    def daily(self, days):
        since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        cols = ",".join(f"MIN({c}),MAX({c})" for c in COUNTERS)
        with self.lock:
            rows = self.db.execute(
                f"SELECT day, {cols}, MAX(soc), MIN(soc), SUM(ev)/60000.0, MIN(ts), MAX(ts) FROM samples WHERE day>? "
                "AND total_pv_kwh IS NOT NULL GROUP BY day ORDER BY day", (since,)).fetchall()
        out, prev_max = [], None
        names = ["pv", "load", "import", "export", "charge", "discharge"]
        for r in rows:
            entry = {"day": r[0]}
            mins, maxs = r[1:13:2], r[2:13:2]
            for i, n in enumerate(names):
                start = prev_max[i] if prev_max and prev_max[i] is not None else mins[i]
                entry[n] = round(max(0.0, (maxs[i] or 0) - (start or 0)), 2)
            entry["soc_max"], entry["soc_min"] = round(r[13] or 0, 1), round(r[14] or 0, 1)
            if r[15] is not None:  # the plant's load total includes the car; split it out
                entry["ev"] = round(min(r[15], entry["load"]), 2)
                entry["load"] = round(entry["load"] - entry["ev"], 2)
            # complete: totals come from the meter's running counters, so a reading near the start of
            # the day (or the previous day's last one) and near its end is all it takes
            d0 = datetime.fromisoformat(r[0]).replace(tzinfo=TZ)
            d1 = (d0 + timedelta(days=1, hours=2)).replace(hour=0)
            entry["complete"] = r[17] >= d1.timestamp() - 1800 and (r[16] <= d0.timestamp() + 1800 or prev_max is not None)
            prev_max = maxs
            out.append(entry)
        # days from a mySigen export fill in what the collector didn't record (before it ran, or
        # a day it only saw part of); the collector's own complete days always win
        imported = self.imported_days(since)
        if imported:
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            by_day = {e["day"]: e for e in out}
            for day, v in imported.items():
                own = by_day.get(day)
                if own and (own["complete"] or day == today):
                    continue
                e = {"day": day, "imported": True, "soc_max": None, "soc_min": None}
                for k in ("pv", "load", "import", "export", "charge", "discharge"):
                    e[k] = round(v[k] or 0.0, 2)
                if v.get("ev"):
                    e["ev"] = round(v["ev"], 2)
                by_day[day] = e
            out = [by_day[d] for d in sorted(by_day)]
        for e in out:
            e.pop("complete", None)
        with self.lock:
            billed = {r[0]: r for r in self.db.execute(
                "SELECT day, import_cost, export_cost, estimated FROM amber_days WHERE day>?", (since,))}
        for e in out:
            b = billed.get(e["day"])
            if b:  # the retailer's meter-based figures win once available
                e["cost"] = round((b[1] + b[2]) / 100, 2)
                e["bought"], e["earned"] = round(b[1] / 100, 2), round(-b[2] / 100, 2)
                e["cost_source"] = "retailer-estimated" if b[3] else "retailer-billed"
            elif not e.get("imported"):  # an imported day has no minute readings worth pricing
                c = self.cost_for_day(e["day"])
                if c:
                    e["cost"], e["cost_source"] = c["net"], "calculated"
                    e["bought"], e["earned"] = c["bought"], c["earned"]
        return out

    # ---- long-term trends and monthly summaries
    def save_daily_stats(self, day, acc):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO daily_stats VALUES (?,?,?,?,?,?)",
                                [(day, k, v[0], v[1], v[2], v[3]) for k, v in acc.items()])
            self.db.commit()

    def daily_stats_for(self, day):
        with self.lock:
            return {k: [mn, mx, sm, n] for k, mn, mx, sm, n in self.db.execute(
                "SELECT key, min, max, sum, n FROM daily_stats WHERE day=?", (day,))}

    def daily_stats(self, since_day):
        out = {}
        with self.lock:
            for day, k, mn, mx, sm, n in self.db.execute(
                    "SELECT day, key, min, max, sum, n FROM daily_stats WHERE day>=? ORDER BY day", (since_day,)):
                out.setdefault(day, {})[k] = {"min": mn, "max": mx, "avg": sm / n if n else None}
        return out

    def string_daily(self, since_ts):
        """{local day: {string: kWh}} from the per-minute string readings."""
        with self.lock:
            rows = self.db.execute("SELECT (ts/3600)*3600 AS h, n, SUM(v*a)/60000.0 FROM string_samples WHERE ts>=? "
                                   "GROUP BY h, n", (int(since_ts),)).fetchall()
        out = {}
        for h, n, kwh in rows:
            d = out.setdefault(local_day(h), {})
            d[n] = d.get(n, 0.0) + max(0.0, kwh or 0.0)
        return out

    def ev_by_month(self):
        with self.lock:
            rows = self.db.execute("SELECT start, kwh, grid_kwh, cost, priced FROM ev_sessions").fetchall()
        out = {}
        for start, kwh, gk, cost, priced in rows:
            m = local_day(start)[:7]
            e = out.setdefault(m, {"sessions": 0, "kwh": 0.0, "grid_kwh": 0.0, "cost": 0.0, "priced": False})
            e["sessions"] += 1; e["kwh"] += kwh or 0; e["grid_kwh"] += gk or 0
            if priced:
                e["cost"] += cost or 0; e["priced"] = True
        return out

    def monthly(self, months=13):
        days = self.daily(min(800, months * 31 + 31))
        ev = self.ev_by_month()
        out = {}
        for d in days:
            m = out.setdefault(d["day"][:7], {"month": d["day"][:7], "days": 0, "pv": 0.0, "load": 0.0, "ev": 0.0,
                                              "import": 0.0, "export": 0.0, "charge": 0.0, "discharge": 0.0,
                                              "bought": 0.0, "earned": 0.0, "priced_days": 0, "best_day": None,
                                              "sources": set()})
            m["days"] += 1
            for k in ("pv", "load", "ev", "import", "export", "charge", "discharge"):
                m[k] += d.get(k) or 0.0
            if d.get("bought") is not None:
                m["bought"] += d["bought"]; m["earned"] += d["earned"]; m["priced_days"] += 1
                m["sources"].add(d.get("cost_source"))
            if not m["best_day"] or d["pv"] > m["best_day"]["pv"]:
                m["best_day"] = {"day": d["day"], "pv": d["pv"]}
        res = []
        for key in sorted(out)[-months:]:
            m = out[key]
            used = m["load"] + m["ev"]
            m["self_sufficiency"] = round(max(0, min(100, (1 - m["import"] / used) * 100)), 1) if used > 0.5 else None
            m["self_consumption"] = round(max(0, min(100, (1 - m["export"] / m["pv"]) * 100)), 1) if m["pv"] > 0.5 else None
            priced = m["priced_days"] > 0
            m["net"] = round(m["bought"] - m["earned"], 2) if priced else None
            avg_buy = m["bought"] / m["import"] if priced and m["import"] > 1 else None
            m["avg_buy_c"] = round(avg_buy * 100, 1) if avg_buy else None
            # what the home and car would have cost from the grid without solar and battery, minus what it did cost
            m["saved"] = round((used - m["import"]) * avg_buy + m["earned"], 2) if avg_buy else None
            m["cost_source"] = ("calculated" if m["sources"] == {"calculated"} else "retailer" if m["sources"]
                                and "calculated" not in m["sources"] else "mixed") if priced else None
            del m["sources"]
            e = ev.get(key)
            m["car"] = {"sessions": e["sessions"], "kwh": round(e["kwh"], 1), "grid_kwh": round(e["grid_kwh"], 1),
                        "cost": round(e["cost"], 2) if e["priced"] else None} if e else None
            for k in ("pv", "load", "ev", "import", "export", "charge", "discharge", "bought", "earned"):
                m[k] = round(m[k], 2 if k in ("bought", "earned") else 1)
            if not priced:
                m["bought"] = m["earned"] = None
            res.append(m)
        return res


# ---------------------------------------------------------------- poller
class Collector:
    def __init__(self, store, host, port, retailer=None, aemo=None, weather=None):
        self.store = store
        self.weather = weather
        self.outlook = Outlook(store, TZ, log=log)
        self.outlook_summary = None
        self.retailer = retailer
        self.aemo = aemo
        self.client = ModbusTCPClient(host, port)
        self.plant = DeviceReader(self.client, PLANT_UNIT, PLANT)
        self.inverter = DeviceReader(self.client, INVERTER_UNIT, INVERTER) if INVERTER_UNIT else None
        # Strings beyond PV4 are only read once the inverter says it has them (register 31025)
        self.extra_strings = {f"pv{n}_{k}" for n in range(5, MAX_PV_STRINGS + 1) for k in ("voltage", "current")}
        if self.inverter:
            self.inverter.unsupported |= self.extra_strings
        self.string_count = None
        self.charger = ChargerWatch(self.client, store, CHARGER_SETTING, {0, PLANT_UNIT, INVERTER_UNIT}, log=log)
        self.state = {"status": "connecting", "mode": "simulator" if SIMULATE else "live",
                      "target": f"{host}:{port}", "updated": None, "error": None}
        self.cond = threading.Condition()
        self.version = 0
        self.minute_bucket, self.minute_acc = None, []
        self.last_alarm_set = set()
        self.fail_count = 0
        self.first_seen = {}
        self.last_ignored = {}
        self.ins_level, self.ins_streak = 0, 0
        self.day_key, self.day_acc = None, {}
        self.rec_lock = threading.Lock()
        self.stopping = threading.Event()
        self.raw = {"plant": {}, "inverter": {}}

    def snapshot(self):
        with self.cond:
            return json.loads(json.dumps(self.state))

    def run(self):
        while True:
            started = time.time()
            try:
                self.poll()
                self.fail_count = 0
            except Exception as e:  # keep running whatever happens
                self.fail_count += 1
                msg = f"{type(e).__name__}: {e}"
                kind = "connection" if isinstance(e, (OSError, ModbusError)) else "internal"
                if kind == "internal":
                    traceback.print_exc()
                if self.state.get("status") != "error":
                    log("Modbus read failed:", msg)
                    self.store.event("connection", f"Lost connection to inverter ({msg})")
                with self.cond:
                    self.state.update(status="error", error=msg, error_kind=kind)
                    self.version += 1
                    self.cond.notify_all()
                self.client.close()
            delay = POLL if self.fail_count == 0 else min(60, POLL * (2 ** min(self.fail_count, 4)))
            time.sleep(max(0.5, delay - (time.time() - started)))

    def poll(self):
        p = self.plant.read()
        if "pv_power" not in p:
            raise RuntimeError("plant registers not readable - check the plant unit id (usually 247)")
        inv = {}
        if self.inverter:
            try:
                inv = self.inverter.read()
                count = int(inv.get("pv_string_count") or 0)
                if 0 < count <= MAX_PV_STRINGS and count != self.string_count:
                    self.string_count = count
                    wanted = {f"pv{n}_{k}" for n in range(5, count + 1) for k in ("voltage", "current")}
                    self.inverter.unsupported -= wanted
                    self.inverter.unsupported |= self.extra_strings - wanted
                    if count > 4:
                        inv = self.inverter.read()  # pick up the extra strings straight away
                    log(f"Inverter reports {count} PV string{'s' if count != 1 else ''}")
            except Exception as e:  # inverter unit id may be wrong - plant data still useful
                inv = {"_error": str(e)}
        buy = self.retailer.prices_now()[0] if self.retailer else None
        ev = self.charger.read(time.time(), p.get("grid_power"), buy, p)
        self.raw = {"plant": p, "inverter": inv, "charger": self.charger.debug(), "read_at": time.time(),
                    "unsupported": {"plant": sorted(self.plant.unsupported),
                                    "inverter": sorted(self.inverter.unsupported) if self.inverter else []}}
        now = time.time()
        was = self.state.get("status")
        self.state_from(p, inv, now, ev)
        if was == "error":
            self.store.event("connection", "Connection to inverter restored")
            log("Connection restored")
        self.record(p, now, inv)
        self._track_day(p, inv, now)
        if now - self.outlook.computed >= 300:
            try:
                res = self.outlook.compute(self.state, self.weather.snapshot() if self.weather else None,
                                           self.retailer.snapshot() if self.retailer else None, now)
                self.outlook_summary = {k: v for k, v in res.items() if k != "projection"}
            except Exception as e:  # the outlook is a nice-to-have; never stop the readings for it
                self.outlook.computed = now
                log("Battery outlook failed:", f"{type(e).__name__}: {e}")
                traceback.print_exc()

    def state_from(self, p, inv, now, ev=None):
        day = local_day(now)
        base = self.store.day_start_counters(day)
        cur = {c: p.get(c) for c in COUNTERS}
        if base is None:  # nothing stored for today yet: count from first reading this session
            base = self.first_seen.setdefault(day, dict(cur))

        def today(c):
            if base and base.get(c) is not None and cur.get(c) is not None:
                return round(max(0.0, cur[c] - base[c]), 2)
            return None

        pv_today = p.get("daily_pv_kwh") if p.get("daily_pv_kwh") is not None else today("total_pv_kwh")
        load_today = p.get("daily_load_kwh") if p.get("daily_load_kwh") is not None else today("total_load_kwh")
        imp, exp = today("total_import_kwh"), today("total_export_kwh")
        chg = inv.get("daily_charge_kwh", today("total_charge_kwh"))
        dis = inv.get("daily_discharge_kwh", today("total_discharge_kwh"))
        cost = self.store.cost_for_day(day)
        ev_w = self.charger.power
        ev_today = self.store.ev_day(day) if ev_w is not None or (ev and ev.get("unit")) else None
        if ev_today is not None and load_today is not None:
            ev_today = min(ev_today, load_today)
        home_w = p.get("load_power")
        if self.charger.source == "plant" and p.get("general_load_power") is not None:
            home_w = max(0.0, p["general_load_power"])  # the plant's own figure without the car
        elif ev_w is not None and home_w is not None:
            home_w = max(0.0, home_w - ev_w)  # the plant's total load includes the car
        self_suff = None
        if load_today and imp is not None and load_today > 0.1:
            self_suff = round(max(0.0, min(100.0, (1 - imp / load_today) * 100)), 1)

        alarms, ignored = decode_alarms(p)
        if ignored != self.last_ignored:
            if ignored:
                log("Ignoring alarm registers with undefined bits set (likely not supported by this firmware): " +
                    ", ".join(f"{k}=0x{v:04X}" for k, v in ignored.items()))
            self.last_ignored = ignored
        keys = {(a["register"], a["bit"]) for a in alarms}
        for a in alarms:
            if (a["register"], a["bit"]) not in self.last_alarm_set:
                self.store.event("alarm", f"{a['source']}: {a['message']}")
        for k in self.last_alarm_set - keys:
            self.store.event("alarm-cleared", f"Alarm cleared ({k[0]} bit {k[1]})")
        self.last_alarm_set = keys

        # Insulation resistance (PV array to earth). Healthy systems typically read 0.5-0.8 MΩ;
        # the inverter protects itself below about 0.37 MΩ (its "ISO protection value").
        # Two levels: "low" (below INSULATION_WARN, worth watching) and "fault" (below
        # INSULATION_MIN, at or past the shutdown point). A level only changes after a few readings
        # in a row, so one odd sample (morning dew) doesn't flap. 0 and 65.535 (0xFFFF) mean
        # "not measured", e.g. overnight on some firmware.
        iso = inv.get("insulation_resistance")
        measured = iso is not None and 0 < iso < 65
        if measured:
            level = 2 if iso < INSULATION_MIN else 1 if iso < INSULATION_WARN else 0
            if level != self.ins_level:
                self.ins_streak += 1
                if self.ins_streak >= INSULATION_CONFIRM:
                    was, self.ins_level, self.ins_streak = self.ins_level, level, 0
                    if level == 2:
                        msg = (f"Insulation resistance {iso:.3f} MΩ, below {INSULATION_MIN:g} MΩ: "
                               "the inverter may shut down solar to protect itself")
                        log(msg); self.store.event("alarm", msg)
                    elif level == 1:
                        msg = f"Insulation resistance {iso:.3f} MΩ, below the usual {INSULATION_WARN:g} MΩ"
                        if was == 2:
                            msg += f" (back above {INSULATION_MIN:g} MΩ)"
                        log(msg); self.store.event("alarm" if was == 0 else "alarm-cleared", msg)
                    else:
                        self.store.event("alarm-cleared", f"Insulation resistance back to normal: {iso:.3f} MΩ")
            else:
                self.ins_streak = 0
        val = f"{iso:.3f} MΩ" if measured else "reading unavailable"
        if self.ins_level == 2:
            alarms = alarms + [{"source": "Dashboard check", "severity": "fault",
                                "message": f"Insulation resistance too low: {val}, below {INSULATION_MIN:g} MΩ",
                                "register": "insulation_resistance", "bit": None}]
        elif self.ins_level == 1:
            alarms = alarms + [{"source": "Dashboard check", "severity": "warning",
                                "message": f"Insulation resistance lower than usual: {val}",
                                "register": "insulation_resistance", "bit": None}]

        strings = []
        n = int(inv.get("pv_string_count") or 0) or 4
        for i in range(1, min(n, MAX_PV_STRINGS) + 1):
            v, a = inv.get(f"pv{i}_voltage"), inv.get(f"pv{i}_current")
            if v is not None and a is not None:
                strings.append({"n": i, "v": round(v, 1), "a": round(a, 2), "w": round(v * a)})

        r = lambda x, d=0: None if x is None else round(x, d)
        cell = lambda x: None if x is None or x >= 65 else round(x, 3)  # 65.535 = 0xFFFF: not reported
        state = {
            "status": "ok", "error": None, "updated": now, "version": VERSION,
            "mode": "simulator" if SIMULATE else "live", "target": self.state["target"],
            "power": {"pv": r(p.get("pv_power")), "load": r(home_w),
                      "battery": r(p.get("battery_power")), "grid": r(p.get("grid_power")), "ev": r(ev_w)},
            "battery": {
                "soc": r(p.get("battery_soc"), 1), "soh": r(p.get("battery_soh"), 1),
                "rated_kwh": r(p.get("battery_rated_kwh"), 1),
                "available_kwh": r(p.get("battery_dischargeable_kwh"), 2),
                "chargeable_kwh": r(p.get("battery_chargeable_kwh"), 2),
                "max_charge_w": r(p.get("battery_max_charge_power")),
                "max_discharge_w": r(p.get("battery_max_discharge_power")),
                "cutoff_low": r(p.get("discharge_cutoff_soc"), 1),
                "cutoff_high": r(p.get("charge_cutoff_soc"), 1),
                "cell_temp_avg": r(inv.get("battery_avg_cell_temp", p.get("battery_avg_cell_temp")), 1),
                "temp_max": r(inv.get("battery_max_temp"), 1), "temp_min": r(inv.get("battery_min_temp"), 1),
                "cell_v_avg": cell(inv.get("battery_avg_cell_voltage")),
                "cell_v_max": cell(inv.get("battery_max_cell_voltage")),
                "cell_v_min": cell(inv.get("battery_min_cell_voltage")),
                "packs": inv.get("pack_count"),
            },
            "today": {"pv": r(pv_today, 2), "import": imp, "export": exp, "ev": ev_today,
                      "load": r(load_today - (ev_today or 0) if load_today is not None else None, 2),
                      "charge": r(chg, 2), "discharge": r(dis, 2), "self_sufficiency": self_suff, "cost": cost,
                      "yesterday_pv": r(p.get("yesterday_pv_kwh"), 2)},
            "system": {
                "running_state": RUNNING_STATE.get(int(p.get("running_state", -1)), "Unknown"),
                "running_code": p.get("running_state"),
                "ems_mode": EMS_WORK_MODE.get(int(p.get("ems_work_mode", -1)), "Unknown"),
                "grid_status": ON_OFF_GRID.get(int(p.get("on_off_grid", -1)), "Unknown"),
                "grid_meter": "Connected" if p.get("grid_sensor_status") == 1 else "Not connected",
                "model": inv.get("model"), "serial": inv.get("serial"), "firmware": inv.get("firmware"),
                "grid_frequency": r(inv.get("grid_frequency"), 2), "grid_voltage": r(inv.get("phase_a_voltage"), 1),
                "grid_current": r(inv.get("phase_a_current"), 1), "pcs_temp": r(inv.get("pcs_temp"), 1),
                "insulation_mohm": r(iso, 3) if measured else None, "insulation_min_mohm": INSULATION_MIN,
                "insulation_warn_mohm": INSULATION_WARN, "insulation_level": self.ins_level,
                "inverter_error": inv.get("_error"), "strings": strings,
                "max_active_power": r(p.get("max_active_power")),
            },
            "alarms": alarms + [a for a in (ev or {}).get("alarms", [])],
            "ev": ev or {"enabled": False, "status": "off"},
            "alarm_registers_ignored": {k: f"0x{v:04X}" for k, v in ignored.items()},
            "events": self.store.events(12),
            "price": self.retailer.summary() if self.retailer else {"enabled": False, "status": "off"},
            "aemo": self.aemo.summary() if self.aemo else {"enabled": False, "status": "off"},
            "weather": self.weather.summary() if self.weather else {"enabled": False, "status": "off"},
            "outlook": self.outlook_summary or {"enabled": False},
        }
        with self.cond:
            state["target"] = self.state["target"]
            self.state = state
            self.version += 1
            self.cond.notify_all()

    def _track_day(self, p, inv, now):
        """Daily min/max/average of slow-moving health figures, for the Trends panel."""
        day = local_day(now)
        if day != self.day_key:
            if self.day_key and self.day_acc:
                self.store.save_daily_stats(self.day_key, self.day_acc)
            self.day_key, self.day_acc = day, self.store.daily_stats_for(day)  # carry on after a restart
        iso = inv.get("insulation_resistance")
        vals = {"insulation": iso if iso is not None and 0 < iso < 65 else None,
                "soh": p.get("battery_soh"), "soc": p.get("battery_soc"),
                "battery_temp": inv.get("battery_max_temp", p.get("battery_avg_cell_temp")),
                "inverter_temp": inv.get("pcs_temp"), "grid_voltage": inv.get("phase_a_voltage")}
        for k, v in vals.items():
            if v is None:
                continue
            a = self.day_acc.get(k)
            if a is None:
                self.day_acc[k] = [v, v, v, 1]
            else:
                a[0], a[1], a[2], a[3] = min(a[0], v), max(a[1], v), a[2] + v, a[3] + 1
        if now - getattr(self, "_day_saved", 0) >= 300:
            self.store.save_daily_stats(day, self.day_acc)
            self._day_saved = now

    def flush_minute(self):
        """Write the readings collected so far for the current minute (also used on shutdown)."""
        if self.minute_bucket is None or not self.minute_acc:
            return
        acc = self.minute_acc
        srows = []
        for n in sorted({k for a in acc for k in a.get("strings", {})}):  # per-string minute averages
            vals = [a["strings"][n] for a in acc if n in a.get("strings", {})]
            srows.append((self.minute_bucket, n, sum(v for v, _ in vals) / len(vals),
                          sum(c for _, c in vals) / len(vals)))
        if srows:
            self.store.save_strings(srows)
        avg = lambda k: sum(a[k] for a in acc) / len(acc)
        last = acc[-1]
        buy, sell = self.retailer.prices_now() if self.retailer else (None, None)
        evs = [a["ev"] for a in acc if a.get("ev") is not None]
        self.store.insert([(self.minute_bucket, local_day(self.minute_bucket),
                            avg("pv"), avg("load"), avg("battery"), avg("grid"), last["soc"],
                            *[last[c] for c in COUNTERS], buy, sell, sum(evs) / len(evs) if evs else None)])
        self.minute_acc = []

    def record(self, p, now, inv=None):
        with self.rec_lock:
            if not self.stopping.is_set():
                self._record(p, now, inv)

    def _record(self, p, now, inv=None):
        minute = int(now // 60) * 60
        if self.minute_bucket is not None and minute != self.minute_bucket and self.minute_acc:
            self.flush_minute()
            if minute % 86400 == 0:
                self.store.prune()
        self.minute_bucket = minute
        self.minute_acc.append({"pv": p.get("pv_power", 0), "load": p.get("load_power", 0),
                                "battery": p.get("battery_power", 0), "grid": p.get("grid_power", 0),
                                "soc": p.get("battery_soc"), **{c: p.get(c) for c in COUNTERS},
                                "strings": self._strings_now(inv), "ev": self.charger.power})

    def _strings_now(self, inv):
        inv = inv or {}
        count = min(int(inv.get("pv_string_count") or 0) or 4, MAX_PV_STRINGS)
        out = {}
        for n in range(1, count + 1):
            v, a = inv.get(f"pv{n}_voltage"), inv.get(f"pv{n}_current")
            if v is not None and a is not None:
                out[n] = (v, a)
        return out


# ---------------------------------------------------------------- simulator wiring
def start_simulator(store):
    import simulator
    plant = simulator.SimPlant()
    now = datetime.now(TZ).replace(tzinfo=None)
    start = (now - timedelta(days=DEMO_DAYS if store.empty() else 0)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    plant.t = start
    fill = store.empty() and DEMO_DAYS > 0
    t, rows, prices, srows = start, [], [], []
    from charger import SessionTracker
    tracker = SessionTracker(store, log) if fill and simulator.EV_ON else None
    while t < now:
        t += timedelta(minutes=1)
        plant.step(t)
        ts = int(t.replace(tzinfo=TZ).timestamp())
        if fill:
            tot = plant.totals
            buy, feed, spot = (None, None, None) if (AMBER_KEY or LV_KEY) else Amber.sim_price(ts)
            rows.append((ts, t.strftime("%Y-%m-%d"), plant.p["pv"], plant.p["load"], plant.p["battery"],
                         plant.p["grid"], plant.soc, tot["pv"], tot["load"], tot["import"], tot["export"],
                         tot["charge"], tot["discharge"], buy, feed, plant.p["ev"] if tracker else None))
            if tracker:
                tracker.update(ts, plant.charger_values(), plant.p["grid"], buy, quiet=True)
            for n, (v, a) in enumerate(plant.string_values(), 1):
                srows.append((ts, n, v, a))
            if buy is not None and ts % 1800 == 0:
                prices.append((ts, "general", ts + 1800, buy, spot, None, Amber._descriptor(buy), None))
                prices.append((ts, "feedIn", ts + 1800, feed, spot, None, None, None))
    if prices:
        store.save_prices(prices)
    if srows:
        store.save_strings(srows)
    if rows:
        store.insert(rows)
        log(f"Pre-filled {DEMO_DAYS} days of simulated history ({len(rows)} samples)")
        # daily health figures for the Trends panel
        socs = {}
        for r in rows:
            socs.setdefault(r[1], []).append(r[6])
        for i, (day, s) in enumerate(sorted(socs.items())[:-1]):
            rnd = lambda k: simulator._rand(day, k)
            iso = 0.6 + 0.12 * rnd("iso") - (0.1 if rnd("cloud") > 0.6 else 0)
            soh = 100 - 0.45 * i / max(1, DEMO_DAYS)
            store.save_daily_stats(day, {
                "insulation": [iso, iso + 0.15, (iso + 0.08) * 1, 1], "soh": [soh, soh, soh, 1],
                "soc": [min(s), max(s), sum(s) / len(s), 1],
                "battery_temp": [21 + 3 * rnd("bt"), 26 + 5 * rnd("bt2"), 24 + 3 * rnd("bt3"), 1],
                "inverter_temp": [24 + 4 * rnd("it"), 42 + 14 * (1 - rnd("cloud")), 33 + 6 * rnd("it2"), 1],
                "grid_voltage": [236 + 3 * rnd("gv"), 248 + 4 * rnd("gv2"), 242 + 2 * rnd("gv3"), 1]})
    srv = simulator.Server(("127.0.0.1", 0), simulator.Handler)
    if fill and tracker:
        tracker.save()
    srv.plant, srv.inverter_unit = plant, INVERTER_UNIT or 1
    srv.alarms = simulator.parse_alarms(ENV("SIM_ALARMS"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "127.0.0.1", srv.server_address[1]


# ---------------------------------------------------------------- HTTP
class Api(BaseHTTPRequestHandler):
    server_version = "SigenDash/" + VERSION
    collector = None
    store = None
    retailer = None
    aemo = None
    weather = None

    def log_message(self, fmt, *args):
        pass

    def _headers(self, code, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._headers(code, "application/json", {"Content-Length": str(len(body))})
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._headers(204, "text/plain", {"Access-Control-Allow-Methods": "GET, OPTIONS",
                                          "Access-Control-Allow-Headers": "*"})

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/live":
                return self._json(self.collector.snapshot())
            if url.path == "/api/history":
                hours = min(24 * 31, max(1, float(q.get("hours", ["24"])[0])))
                return self._json(self.store.history(hours))
            if url.path == "/api/daily":
                days = min(800, max(1, int(q.get("days", ["30"])[0])))
                return self._json(self.store.daily(days))
            if url.path == "/api/aemo":
                if not self.aemo:
                    return self._json({"enabled": False, "status": "off"})
                return self._json(self.aemo.snapshot(min(48, max(1, float(q.get("hours", ["24"])[0])))))
            if url.path == "/api/prices":
                a = self.retailer.snapshot() if self.retailer else {"enabled": False, "status": "off"}
                a["history"] = self.store.price_history(min(48, max(1, float(q.get("hours", ["12"])[0]))))
                return self._json(a)
            if url.path == "/api/string-history":
                hours = min(24 * 31, max(1, float(q.get("hours", ["24"])[0])))
                h = self.store.string_history(hours)
                h["today"] = self.store.string_day(datetime.now(TZ).strftime("%Y-%m-%d"))
                return self._json(h)
            if url.path == "/api/strings":
                hours = min(24 * 31, max(1, float(q.get("hours", ["48"])[0])))
                rows = self.store.strings(hours)
                if q.get("format", [""])[0] == "csv":
                    lines = ["timestamp_utc,local_time,string,voltage_v,current_a,power_w"]
                    for ts, n, v, a in rows:
                        lt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")
                        lines.append(f"{ts},{lt},PV{n},{v:.1f},{a:.2f},{v * a:.0f}")
                    body = ("\n".join(lines) + "\n").encode()
                    self._headers(200, "text/csv", {"Content-Length": str(len(body)),
                                                    "Content-Disposition": 'attachment; filename="pv-strings.csv"'})
                    self.wfile.write(body)
                    return
                return self._json([{"t": ts, "string": n, "v": round(v, 1), "a": round(a, 2)} for ts, n, v, a in rows])
            if url.path in ("/eink.png", "/api/eink.png"):
                import eink
                if not eink.AVAILABLE:
                    return self._json({"error": "The e-ink image needs Pillow (included in the Docker image)."}, 501)
                w = min(3000, max(300, int(q.get("w", ["1404"])[0])))
                h = min(4000, max(300, int(q.get("h", ["1872"])[0])))
                if q.get("away", [""])[0] == "1":
                    body = eink.render_away(width=w, height=h, version=VERSION, title=(q.get("title", [""])[0] or None),
                                            rm_owner=q.get("rm", [""])[0] == "1")
                    self._headers(200, "image/png", {"Content-Length": str(len(body))})
                    self.wfile.write(body)
                    return
                body = eink.render(self.collector.snapshot(), self.store.history(24), tz=TZ, width=w, height=h,
                                   version=VERSION, prices=self.retailer.snapshot() if self.retailer else None,
                                   aemo=self.aemo.snapshot(3) if self.aemo else None,
                                   title=(q.get("title", [""])[0] or None), owner=(q.get("owner", [""])[0] or None),
                                   phone=(q.get("phone", [""])[0] or None),
                                   rm_owner=q.get("rm", [""])[0] == "1")
                self._headers(200, "image/png", {"Content-Length": str(len(body))})
                self.wfile.write(body)
                return
            if url.path == "/remarkable/install.sh":
                # the installer, with this collector's address filled in (as the tablet reached it)
                host = self.headers.get("Host") or f"127.0.0.1:{HTTP_PORT}"
                body = (WEB_DIR / "remarkable" / "install.sh").read_text().replace("__BASE__", f"http://{host}").encode()
                self._headers(200, "text/x-shellscript; charset=utf-8", {"Content-Length": str(len(body))})
                self.wfile.write(body)
                return
            if url.path == "/api/trends":
                days = min(800, max(7, int(float(q.get("days", ["365"])[0]))))
                since = datetime.now(TZ) - timedelta(days=days)
                stats = self.store.daily_stats(since.strftime("%Y-%m-%d"))
                strings = self.store.string_daily(since.timestamp())
                daily = {d["day"]: d for d in self.store.daily(days)}
                all_days = sorted(set(stats) | set(strings) | set(daily))
                ids = sorted({n for d in strings.values() for n in d})
                r1 = lambda v: None if v is None else round(v, 3)
                return self._json({
                    "days": all_days,
                    "strings": {n: [round(strings.get(d, {}).get(n, 0.0), 2) if d in strings else None for d in all_days]
                                for n in ids},
                    "pv": [daily[d]["pv"] if d in daily else None for d in all_days],
                    "stats": {k: {f: [r1((stats.get(d, {}).get(k) or {}).get(f)) for d in all_days]
                                  for f in ("min", "avg", "max")}
                              for k in ("insulation", "soh", "battery_temp", "inverter_temp", "grid_voltage", "soc")},
                    "insulation_limits": {"warn": INSULATION_WARN, "min": INSULATION_MIN},
                })
            if url.path == "/api/monthly":
                months = min(36, max(1, int(q.get("months", ["13"])[0])))
                rows = self.store.monthly(months)
                if q.get("format", [""])[0] == "csv":
                    f = lambda v: "" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
                    lines = ["month,days,solar_kwh,home_kwh,car_kwh,bought_kwh,sold_kwh,battery_charged_kwh,"
                             "battery_discharged_kwh,self_sufficiency_pct,self_consumption_pct,bought_dollars,"
                             "earned_dollars,net_dollars,saved_dollars,avg_buy_c_per_kwh,car_sessions,car_cost_dollars"]
                    for m in rows:
                        car = m["car"] or {}
                        lines.append(",".join(f(x) for x in (m["month"], m["days"], m["pv"], m["load"], m["ev"], m["import"],
                                     m["export"], m["charge"], m["discharge"], m["self_sufficiency"], m["self_consumption"],
                                     m["bought"], m["earned"], m["net"], m["saved"], m["avg_buy_c"], car.get("sessions"),
                                     car.get("cost"))))
                    return self._csv("home-energy-monthly.csv", lines)
                return self._json(rows)
            if url.path == "/api/money":
                return self._json(self.money.summary())
            if url.path == "/api/outlook":
                return self._json(self.collector.outlook.result or {"enabled": False, "reason": "not worked out yet"})
            if url.path == "/api/weather":
                return self._json(self.weather.snapshot() if self.weather else {"enabled": False, "status": "off"})
            if url.path in ("/api/ev", "/api/ev-sessions", "/api/ev-log"):
                return self._ev(url.path, q)
            if url.path == "/api/debug":
                raw = json.loads(json.dumps(self.collector.raw, default=str))
                for dev in ("plant", "inverter"):
                    for k, v in list((raw.get(dev) or {}).items()):
                        if k.startswith("alarm") and isinstance(v, (int, float)):
                            raw[dev][k] = f"0x{int(v):04X}"
                raw["version"] = VERSION
                return self._json(raw)
            if url.path == "/api/health":
                s = self.collector.snapshot()
                return self._json({"ok": s.get("status") == "ok", "status": s.get("status"),
                                   "updated": s.get("updated"), "version": VERSION})
            if url.path == "/api/stream":
                return self._stream()
            return self._static(url.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def _csv(self, name, lines):
        body = ("\n".join(lines) + "\n").encode()
        self._headers(200, "text/csv", {"Content-Length": str(len(body)),
                                        "Content-Disposition": f'attachment; filename="{name}"'})
        self.wfile.write(body)

    def _ev(self, path, q):
        from registers import CHARGER_STATE
        days = min(800, max(1, float(q.get("days", ["30"])[0])))
        csv = q.get("format", [""])[0] == "csv"
        lt = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        if path == "/api/ev-log":
            rows = self.store.ev_states(days)
            if csv:
                return self._csv("ev-charger-states.csv", ["timestamp_utc,local_time,state,state_code,meaning,power_w,offered_current_a"] + [
                    f"{ts},{lt(ts)},{code},{st},\"{CHARGER_STATE.get(st, ('', ''))[1]}\",{p or 0:.0f},{'' if a is None else f'{a:g}'}"
                    for ts, st, code, p, a in rows])
            return self._json([{"t": ts, "state_code": st, "state": code, "power": p, "set_current": a}
                               for ts, st, code, p, a in rows])
        sessions = self.store.ev_sessions(days)
        if self.fuel.enabled:
            for x in sessions:
                x["fuel"] = self.fuel.session(x)
        if path == "/api/ev-sessions" and csv:
            fcols = ",km,fuel_litres,fuel_price_c_per_l,fuel_price_from,fuel_cost_dollars,solar_kwh,solar_at_export_dollars," \
                    "charging_cost_dollars,saved_dollars" if self.fuel.enabled else ""
            lines = ["plugged_in,unplugged,minutes_plugged_in,minutes_charging,kwh,kwh_from_grid,"
                     "grid_cost_dollars,peak_kw,states_seen" + fcols]
            v = lambda n, f="{:.2f}": "" if n is None else f.format(n)
            for x in sessions:
                mins = round(((x["end"] or time.time()) - x["start"]) / 60)
                cost = "" if x["cost"] is None else f"{x['cost']:.2f}"
                row = (f"{lt(x['start'])},{lt(x['end'])},{mins},{round((x['charging_s'] or 0) / 60)},"
                       f"{x['kwh']:.2f},{x['grid_kwh']:.2f},{cost},{x['peak_w'] / 1000:.2f},"
                       f"{(x['states'] or '').replace(',', ' ')}")
                if self.fuel.enabled:
                    f = x.get("fuel") or {}
                    row += (f",{v(f.get('km'), '{}')},{v(f.get('litres'), '{}')},{v(f.get('price_c'), '{}')},{f.get('price_from') or ''},"
                            f"{v(f.get('fuel_cost'))},{v(f.get('solar_kwh'))},{v(f.get('solar_cost'))},{v(f.get('charge_cost'))},{v(f.get('saved'))}")
                lines.append(row)
            return self._csv("ev-charging-sessions.csv", lines)
        out = {"sessions": sessions, "live": self.collector.charger.summary(), "fuel": self.fuel.summary(sessions)}
        if path == "/api/ev":
            out["log"] = [{"t": ts, "state_code": st, "state": code, "power": p}
                          for ts, st, code, p, _a in self.store.ev_states(min(days, 2))]
        return self._json(out)

    def _stream(self):
        self._headers(200, "text/event-stream", {"Connection": "keep-alive"})
        c = self.collector
        seen = -1
        while True:
            with c.cond:
                if c.version == seen:
                    c.cond.wait(timeout=20)
                if c.version == seen:
                    payload = b": keepalive\n\n"
                else:
                    seen = c.version
                    payload = b"data: " + json.dumps(c.state, separators=(",", ":")).encode() + b"\n\n"
            self.wfile.write(payload)
            self.wfile.flush()

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR or not target.is_file():
            return self._json({"error": "not found"}, 404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._headers(200, ctype, {"Content-Length": str(len(body))})
        self.wfile.write(body)


def make_retailer(store):
    """Pick the electricity retailer price feed from the settings."""
    choice = PRICE_PROVIDER
    if choice == "auto":
        choice = "localvolts" if LV_KEY else "amber" if (AMBER_KEY or SIMULATE) else "none"
    if choice == "localvolts":
        if not (LV_KEY and LV_PARTNER):
            log("LocalVolts selected but LOCALVOLTS_API_KEY / LOCALVOLTS_PARTNER_ID are missing; prices off")
            return None
        return LocalVolts(store, LV_KEY, LV_PARTNER, LV_NMI, tz=TZ, log=log)
    if choice == "amber":
        return Amber(store, AMBER_KEY or None, AMBER_SITE, simulate=SIMULATE, log=log)
    return None


def main():
    if not SIMULATE and not HOST:
        sys.exit("Set SIGEN_HOST to your inverter's IP address, or SIGEN_SIMULATE=1 to use the simulator.")
    store = Store(DATA_DIR / ("simulator.db" if SIMULATE else "sigen.db"))
    host, port = (start_simulator(store) if SIMULATE else (HOST, PORT))
    import_folder(store, DATA_DIR, log=log)  # past days from a mySigen export, if any were dropped in
    (DATA_DIR / "import").mkdir(exist_ok=True)
    retailer = make_retailer(store)
    if retailer and RETAILER_SINCE:
        retailer.since = RETAILER_SINCE  # no costs to fetch from before you joined
    if retailer:
        threading.Thread(target=retailer.run, daemon=True).start()
    aemo = Aemo(store, AEMO_REGION, enabled=AEMO_ENABLED, simulate=AEMO_SIMULATE, log=log)
    threading.Thread(target=aemo.run, daemon=True).start()
    weather = Weather(store, SITE_LAT, SITE_LON, SITE_ADDRESS, SOLAR_PLANES, SOLAR_KWP,
                      enabled=WEATHER_ENABLED, simulate=SIMULATE, cache_dir=DATA_DIR, tz=TZ, log=log)
    if weather.enabled:
        threading.Thread(target=weather.run, daemon=True).start()
    elif WEATHER_ENABLED:
        log("Weather off: set SITE_LAT and SITE_LON (or SITE_ADDRESS) to see weather and a solar forecast")
    collector = Collector(store, host, port, retailer, aemo, weather)
    if SIMULATE:
        collector.state["target"] = "built-in simulator"
    threading.Thread(target=collector.run, daemon=True).start()
    rname = (retailer.summary() or {}).get("provider") if retailer else None
    money = Money(store, TZ, DATA_DIR, preset=COMPARE_PLANS, system_cost=SYSTEM_COST, installed=SYSTEM_INSTALLED,
                  supply_c_day=RETAILER_SUPPLY_C_DAY, fee_monthly=RETAILER_FEE_MONTHLY, retailer_name=rname,
                  retailer_since=RETAILER_SINCE, log=log)
    Api.collector, Api.store, Api.retailer, Api.aemo, Api.weather = collector, store, retailer, aemo, weather
    Api.money = money
    fuel = Fuel(store, TZ, FUEL_SETTINGS, locate=weather.location, simulate=SIMULATE, log=log)
    Api.fuel = money.fuel = fuel
    if fuel.enabled:
        threading.Thread(target=fuel.run, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Api)
    httpd.daemon_threads = True
    if SETTINGS_PATH:
        log(f"Settings read from {SETTINGS_PATH}")
    log(f"SigenStor dashboard v{VERSION} on http://0.0.0.0:{HTTP_PORT}  "
        f"(reading {'simulator' if SIMULATE else f'{host}:{port}'}, plant unit {PLANT_UNIT}, "
        f"inverter unit {INVERTER_UNIT or 'off'}, every {POLL:g}s; prices: "
        f"{(retailer.state['provider'] + ' ' + retailer.state['mode']) if retailer else 'off'}; "
        f"AEMO: {AEMO_REGION if AEMO_ENABLED else 'off'}{' (simulated)' if AEMO_SIMULATE else ''})")

    def stop(signum, _frame):
        # Inside a container this process is PID 1, which ignores SIGTERM unless it
        # installs a handler, so `docker stop` would otherwise wait 10 s and kill it.
        log(f"Stopping ({signal.Signals(signum).name})")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        try:
            with collector.rec_lock:
                collector.stopping.set()
                collector.flush_minute()
                collector.charger.close()
            store.close()
        except Exception as e:  # never block shutdown
            log("Error while saving on shutdown:", e)
        log("Stopped cleanly")


if __name__ == "__main__":
    main()

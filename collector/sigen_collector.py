"""SigenStor local dashboard collector.

Polls the SigenStor over Modbus TCP (read-only), keeps minute-level history in SQLite,
and serves the dashboard + a small JSON API for the desktop widgets.

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
from amber import Amber  # noqa: E402
from localvolts import LocalVolts  # noqa: E402
from modbus import DeviceReader, ModbusError, ModbusTCPClient  # noqa: E402
from registers import (EMS_WORK_MODE, INVERTER, ON_OFF_GRID, PLANT,  # noqa: E402
                       RUNNING_STATE, decode_alarms)

VERSION = "1.0.0"
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
            CREATE TABLE IF NOT EXISTS amber_days(
              day TEXT PRIMARY KEY, import_kwh REAL, import_cost REAL, export_kwh REAL,
              export_cost REAL, estimated INTEGER);
            """)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
            for c in ("buy_c", "sell_c"):  # Amber price (c/kWh, Amber sign) in force that minute
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
                "total_import_kwh, total_export_kwh, total_charge_kwh, total_discharge_kwh, buy_c, sell_c) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def save_prices(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def save_amber_days(self, days):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO amber_days VALUES (?,?,?,?,?,?)", [
                (d, v["import_kwh"], v["import_cost"], v["export_kwh"], v["export_cost"], int(v["estimated"]))
                for d, v in days.items()])
            self.db.commit()

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
                "SELECT (ts/?)*? AS b, AVG(pv), AVG(load), AVG(battery), AVG(grid), AVG(soc) "
                "FROM samples WHERE ts>=? GROUP BY b ORDER BY b", (bucket, bucket, since)).fetchall()
        keys = ["t", "pv", "load", "battery", "grid", "soc"]
        out = {k: [] for k in keys}
        for r in rows:
            for k, v in zip(keys, r):
                out[k].append(round(v, 1) if isinstance(v, float) else v)
        out["bucket_seconds"] = bucket
        return out

    def daily(self, days):
        since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        cols = ",".join(f"MIN({c}),MAX({c})" for c in COUNTERS)
        with self.lock:
            rows = self.db.execute(
                f"SELECT day, {cols}, MAX(soc), MIN(soc) FROM samples WHERE day>? "
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
            prev_max = maxs
            out.append(entry)
        with self.lock:
            billed = {r[0]: r for r in self.db.execute(
                "SELECT day, import_cost, export_cost, estimated FROM amber_days WHERE day>?", (since,))}
        for e in out:
            b = billed.get(e["day"])
            if b:  # the retailer's meter-based figures win once available
                e["cost"] = round((b[1] + b[2]) / 100, 2)
                e["cost_source"] = "retailer-estimated" if b[3] else "retailer-billed"
            else:
                c = self.cost_for_day(e["day"])
                if c:
                    e["cost"], e["cost_source"] = c["net"], "calculated"
        return out


# ---------------------------------------------------------------- poller
class Collector:
    def __init__(self, store, host, port, retailer=None, aemo=None):
        self.store = store
        self.retailer = retailer
        self.aemo = aemo
        self.client = ModbusTCPClient(host, port)
        self.plant = DeviceReader(self.client, PLANT_UNIT, PLANT)
        self.inverter = DeviceReader(self.client, INVERTER_UNIT, INVERTER) if INVERTER_UNIT else None
        self.state = {"status": "connecting", "mode": "simulator" if SIMULATE else "live",
                      "target": f"{host}:{port}", "updated": None, "error": None}
        self.cond = threading.Condition()
        self.version = 0
        self.minute_bucket, self.minute_acc = None, []
        self.last_alarm_set = set()
        self.fail_count = 0
        self.first_seen = {}
        self.last_ignored = {}
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
            except Exception as e:  # inverter unit id may be wrong - plant data still useful
                inv = {"_error": str(e)}
        self.raw = {"plant": p, "inverter": inv, "read_at": time.time(),
                    "unsupported": {"plant": sorted(self.plant.unsupported),
                                    "inverter": sorted(self.inverter.unsupported) if self.inverter else []}}
        now = time.time()
        was = self.state.get("status")
        self.state_from(p, inv, now)
        if was == "error":
            self.store.event("connection", "Connection to inverter restored")
            log("Connection restored")
        self.record(p, now, inv)

    def state_from(self, p, inv, now):
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

        strings = []
        n = int(inv.get("pv_string_count") or 0) or 4
        for i in range(1, min(n, 4) + 1):
            v, a = inv.get(f"pv{i}_voltage"), inv.get(f"pv{i}_current")
            if v is not None and a is not None:
                strings.append({"n": i, "v": round(v, 1), "a": round(a, 2), "w": round(v * a)})

        r = lambda x, d=0: None if x is None else round(x, d)
        state = {
            "status": "ok", "error": None, "updated": now, "version": VERSION,
            "mode": "simulator" if SIMULATE else "live", "target": self.state["target"],
            "power": {"pv": r(p.get("pv_power")), "load": r(p.get("load_power")),
                      "battery": r(p.get("battery_power")), "grid": r(p.get("grid_power"))},
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
                "cell_v_avg": r(inv.get("battery_avg_cell_voltage"), 3),
                "cell_v_max": r(inv.get("battery_max_cell_voltage"), 3),
                "cell_v_min": r(inv.get("battery_min_cell_voltage"), 3),
                "packs": inv.get("pack_count"),
            },
            "today": {"pv": r(pv_today, 2), "load": r(load_today, 2), "import": imp, "export": exp,
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
                "insulation_mohm": r(inv.get("insulation_resistance"), 2),
                "inverter_error": inv.get("_error"), "strings": strings,
                "max_active_power": r(p.get("max_active_power")),
            },
            "alarms": alarms,
            "alarm_registers_ignored": {k: f"0x{v:04X}" for k, v in ignored.items()},
            "events": self.store.events(12),
            "price": self.retailer.summary() if self.retailer else {"enabled": False, "status": "off"},
            "aemo": self.aemo.summary() if self.aemo else {"enabled": False, "status": "off"},
        }
        with self.cond:
            state["target"] = self.state["target"]
            self.state = state
            self.version += 1
            self.cond.notify_all()

    def flush_minute(self):
        """Write the readings collected so far for the current minute (also used on shutdown)."""
        if self.minute_bucket is None or not self.minute_acc:
            return
        acc = self.minute_acc
        srows = []
        for n in range(1, 5):  # per-string minute averages (for orientation analysis)
            vs = [a[f"pv{n}_v"] for a in acc if a.get(f"pv{n}_v") is not None]
            cs = [a[f"pv{n}_a"] for a in acc if a.get(f"pv{n}_a") is not None]
            if vs and cs:
                srows.append((self.minute_bucket, n, sum(vs) / len(vs), sum(cs) / len(cs)))
        if srows:
            self.store.save_strings(srows)
        avg = lambda k: sum(a[k] for a in acc) / len(acc)
        last = acc[-1]
        buy, sell = self.retailer.prices_now() if self.retailer else (None, None)
        self.store.insert([(self.minute_bucket, local_day(self.minute_bucket),
                            avg("pv"), avg("load"), avg("battery"), avg("grid"), last["soc"],
                            *[last[c] for c in COUNTERS], buy, sell)])
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
                                **{f"pv{n}_{k}": (inv or {}).get(f"pv{n}_{f}") for n in range(1, 5)
                                   for k, f in (("v", "voltage"), ("a", "current"))}})


# ---------------------------------------------------------------- simulator wiring
def start_simulator(store):
    import simulator
    plant = simulator.SimPlant()
    now = datetime.now(TZ).replace(tzinfo=None)
    start = (now - timedelta(days=DEMO_DAYS if store.empty() else 0)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    plant.t = start
    fill = store.empty() and DEMO_DAYS > 0
    t, rows, prices = start, [], []
    while t < now:
        t += timedelta(minutes=1)
        plant.step(t)
        ts = int(t.replace(tzinfo=TZ).timestamp())
        if fill:
            tot = plant.totals
            buy, feed, spot = (None, None, None) if (AMBER_KEY or LV_KEY) else Amber.sim_price(ts)
            rows.append((ts, t.strftime("%Y-%m-%d"), plant.p["pv"], plant.p["load"], plant.p["battery"],
                         plant.p["grid"], plant.soc, tot["pv"], tot["load"], tot["import"], tot["export"],
                         tot["charge"], tot["discharge"], buy, feed))
            if buy is not None and ts % 1800 == 0:
                prices.append((ts, "general", ts + 1800, buy, spot, None, Amber._descriptor(buy), None))
                prices.append((ts, "feedIn", ts + 1800, feed, spot, None, None, None))
    if prices:
        store.save_prices(prices)
    if rows:
        store.insert(rows)
        log(f"Pre-filled {DEMO_DAYS} days of simulated history ({len(rows)} samples)")
    srv = simulator.Server(("127.0.0.1", 0), simulator.Handler)
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
    retailer = make_retailer(store)
    if retailer:
        threading.Thread(target=retailer.run, daemon=True).start()
    aemo = Aemo(store, AEMO_REGION, enabled=AEMO_ENABLED, simulate=AEMO_SIMULATE, log=log)
    threading.Thread(target=aemo.run, daemon=True).start()
    collector = Collector(store, host, port, retailer, aemo)
    if SIMULATE:
        collector.state["target"] = "built-in simulator"
    threading.Thread(target=collector.run, daemon=True).start()
    Api.collector, Api.store, Api.retailer, Api.aemo = collector, store, retailer, aemo
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
            store.close()
        except Exception as e:  # never block shutdown
            log("Error while saving on shutdown:", e)
        log("Stopped cleanly")


if __name__ == "__main__":
    main()

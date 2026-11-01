"""Storage: the SQLite database behind the collector (minute readings, strings, prices, retailer
costs, bills, car sessions, events, daily stats) and the queries the dashboard and API use.

Split out of sigen_collector.py. Call configure() with the local time zone and how many days of
minute history to keep before using Store.
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Sydney")
RETENTION_DAYS = 800
COUNTERS = ["total_pv_kwh", "total_load_kwh", "total_import_kwh", "total_export_kwh",
            "total_charge_kwh", "total_discharge_kwh"]


def configure(tz, retention_days):
    global TZ, RETENTION_DAYS
    TZ, RETENTION_DAYS = tz, retention_days


def log(*a):
    print(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def local_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
            if "source" not in acols:  # "api" or "csv" (an imported Amber usage report, which the API doesn't replace)
                self.db.execute("ALTER TABLE amber_days ADD COLUMN source TEXT")
            # retailer bills: each one sets its period's costs exactly (see daily())
            self.db.execute("CREATE TABLE IF NOT EXISTS bills(start TEXT, end TEXT, energy_cost REAL, export_credit REAL, "
                            "avg_buy_c REAL, source TEXT, PRIMARY KEY(start, end))")
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
            # buy_c/sell_c: retailer price (c/kWh, Amber sign) in force that minute; ev: AC charger W
            for c in ("buy_c", "sell_c", "ev"):
                if c not in cols:
                    self.db.execute(f"ALTER TABLE samples ADD COLUMN {c} REAL")
            # own_kwh/own_value/own_priced: car energy not from the grid (solar or battery), measured
            # each poll, and what it would have earned exported at that moment's price ($, never < 0)
            ecols = {r[1] for r in self.db.execute("PRAGMA table_info(ev_sessions)")}
            for c, t in (("own_kwh", "REAL"), ("own_value", "REAL"), ("own_priced", "INTEGER"), ("last_rise", "INTEGER")):
                if c not in ecols:
                    self.db.execute(f"ALTER TABLE ev_sessions ADD COLUMN {c} {t}")
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

    def save_csv_prices(self, intervals):
        """5-minute buy prices from an Amber usage report (channel "csv-general"; never the live feed's rows)."""
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO prices (start, channel, end, per_kwh) VALUES (?, 'csv-general', ?, ?)",
                                [(t, t + 300, p) for t, p in intervals])
            self.db.commit()

    def home_profile(self, days=30):
        """Share of a day's home use in each local hour, from the collector's own recent days ({hour: share})."""
        now = time.time()
        rows = self.hourly_avgs(now - days * 86400, now)
        by = {}
        for h, (_pv, home) in rows.items():
            if home is not None:
                by.setdefault(datetime.fromtimestamp(h, TZ).hour, []).append(home)
        if len(by) < 24:
            return None
        avg = {hr: sum(v) / len(v) for hr, v in by.items()}
        tot = sum(avg.values())
        return {hr: v / tot for hr, v in avg.items()} if tot > 0 else None

    def save_amber_days(self, days, source="api"):
        """Amber's daily costs. A day from an imported usage report (source "csv", every read actual)
        isn't replaced by the API's figures for it."""
        with self.lock:
            self.db.executemany(
                "INSERT INTO amber_days (day, import_kwh, import_cost, export_kwh, export_cost, estimated, avg_buy_c, source) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET import_kwh=excluded.import_kwh, "
                "import_cost=excluded.import_cost, export_kwh=excluded.export_kwh, export_cost=excluded.export_cost, "
                "estimated=excluded.estimated, avg_buy_c=excluded.avg_buy_c, source=excluded.source "
                "WHERE excluded.source = 'csv' OR amber_days.source IS NOT 'csv' OR amber_days.estimated = 1", [
                    (d, v["import_kwh"], v["import_cost"], v["export_kwh"], v["export_cost"], int(v["estimated"]),
                     v.get("avg_buy_c"), source) for d, v in days.items()])
            self.db.commit()

    def save_bill(self, b, source=""):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO bills VALUES (?,?,?,?,?,?)",
                            (b["start"], b["end"], b["energy_cost"], b["export_credit"], b.get("avg_buy_c"), source))
            self.db.commit()

    def bills(self):
        with self.lock:
            return [dict(zip(("start", "end", "energy_cost", "export_credit", "avg_buy_c", "source"), r))
                    for r in self.db.execute("SELECT start, end, energy_cost, export_credit, avg_buy_c, source FROM bills "
                                             "ORDER BY start")]

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
    EV_COLS = ["start", "end", "start_kwh", "kwh", "grid_kwh", "cost", "peak_w", "charging_s", "states", "priced",
               "own_kwh", "own_value", "own_priced", "last_rise"]

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
            if s.get("own_kwh") is not None:
                s["own_kwh"], s["own_value"] = round(s["own_kwh"], 3), round(s["own_value"] or 0, 4)
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
        # otherwise (an imported day): the day's home and car use, spread over the hours like the house
        # usually uses it, at each hour's price from an Amber usage report; or at the day's average price
        with self.lock:
            imp = self.db.execute("SELECT load, ev FROM imported_days WHERE day=?", (day,)).fetchone()
            a = self.db.execute("SELECT avg_buy_c FROM amber_days WHERE day=?", (day,)).fetchone()
            d0 = datetime.fromisoformat(day).replace(tzinfo=TZ)
            hp = dict(self.db.execute("SELECT (start/3600)*3600, AVG(per_kwh) FROM prices WHERE channel='csv-general' "
                                      "AND start>=? AND start<? GROUP BY 1", (int(d0.timestamp()),
                                                                             int((d0 + timedelta(days=1)).timestamp()))).fetchall())
        if not imp:
            return None
        kwh = (imp[0] or 0) + (imp[1] or 0)
        prof = self.home_profile() if len(hp) >= 20 else None
        if prof:
            avg = sum(hp.values()) / len(hp)
            return sum(kwh * prof.get(datetime.fromtimestamp(h, TZ).hour, 0) * hp.get(h, avg)
                       for h in {int((d0 + timedelta(hours=i)).timestamp()) // 3600 * 3600 for i in range(24)}) / 100
        if a and a[0] is not None:
            return kwh * a[0] / 100
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
        """Daily totals and costs for the last `days` days. A bill sets its whole period's costs: each
        day's bought and earned amounts are scaled so the period adds up to the bill exactly (the
        range is widened to whole bills first, so a bill cut by the window is still scaled right)."""
        since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        bills = [b for b in self.bills() if b["end"] > since]
        wide = min([since] + [(datetime.fromisoformat(b["start"]) - timedelta(days=1)).strftime("%Y-%m-%d") for b in bills])
        out = self._daily_since(wide)
        for b in bills:
            self._apply_bill_to(out, b)
        return [e for e in out if e["day"] > since]

    @staticmethod
    def _apply_bill_to(out, b):
        period = [e for e in out if b["start"] <= e["day"] <= b["end"] and e.get("bought") is not None]
        if not period:
            return
        for key, target, kwh in (("bought", b["energy_cost"], "import"), ("earned", b["export_credit"], "export")):
            have = sum(e[key] for e in period)
            if have > 0:
                weights = [e[key] / have for e in period]
            else:  # nothing recorded: spread by energy instead
                k = sum(e.get(kwh) or 0 for e in period)
                weights = [((e.get(kwh) or 0) / k) if k else 1 / len(period) for e in period]
            vals = [round(target * w, 2) for w in weights]
            vals[-1] = round(vals[-1] + target - sum(vals), 2)  # rounding left over: the period still adds up
            for e, v in zip(period, vals):
                e[key] = v
        for e in period:
            e["cost"], e["cost_source"] = round(e["bought"] - e["earned"], 2), "bill"

    def _daily_since(self, since):
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

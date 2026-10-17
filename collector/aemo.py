"""AEMO National Electricity Market data (no account needed).

Uses the public feeds behind AEMO's NEM data dashboard
(https://visualisations.aemo.com.au/aemo/apps/api/report/...). AEMO doesn't
document these as a supported API, so every part is fetched independently and
a failure in one never breaks the others or the rest of the collector.

  5MIN              POST {"timeScale":["30MIN"]}  5-min actual prices + 30-min forecast (pre-dispatch)
  ELEC_NEM_SUMMARY  GET                            current price, status, demand, interchange, interconnectors
  FUEL              POST {"type":["CURRENT"]}      current large-scale generation by fuel type

Times are NEM time (AEST, UTC+10, no daylight saving). Prices are $/MWh (RRP);
divide by 10 for c/kWh.
"""
import json
import math
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://visualisations.aemo.com.au/aemo/apps/api/report/"
NEM_TZ = timezone(timedelta(hours=10))
REGIONS = ("NSW1", "QLD1", "VIC1", "SA1", "TAS1")
RENEWABLE = {"solar", "wind", "hydro", "biomass"}


class AemoError(Exception):
    pass


def _nem_ts(s):
    """'2026-10-17T16:35:00' in NEM time -> epoch seconds."""
    return datetime.fromisoformat(s.replace(" ", "T")[:19]).replace(tzinfo=NEM_TZ).timestamp()


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _request(name, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + name, data=data, method="POST" if body is not None else "GET", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
        "User-Agent": "sigen-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AemoError(f"AEMO {name} returned {e.code}")
    except urllib.error.URLError as e:
        raise AemoError(f"Can't reach AEMO: {e.reason}")
    except ValueError:
        raise AemoError(f"AEMO {name} returned something that isn't JSON")


class Aemo:
    def __init__(self, store, region="NSW1", enabled=True, simulate=False, log=print):
        self.store, self.log = store, log
        self.region = region if region in REGIONS else "NSW1"
        self.enabled, self.simulate = enabled, simulate
        self.lock = threading.Lock()
        self.errors = {}
        self.state = {"enabled": enabled, "mode": "simulator" if simulate else "live",
                      "status": "starting" if enabled else "off", "region": self.region, "error": None,
                      "current": None, "forecast": [], "history": [], "fuel": [], "interconnectors": [],
                      "renewables_pct": None, "updated": None}

    def snapshot(self, hours=24):
        with self.lock:
            s = json.loads(json.dumps(self.state))
        s["history"] = self.store.aemo_history(self.region, hours)
        return s

    def summary(self):
        with self.lock:
            c = self.state.get("current") or {}
            return {"enabled": self.enabled, "status": self.state["status"], "region": self.region,
                    "mode": self.state["mode"], "rrp": c.get("rrp"), "price_c": c.get("price_c"),
                    "price_status": c.get("price_status"), "demand_mw": c.get("demand_mw"),
                    "renewables_pct": self.state.get("renewables_pct"), "at": c.get("at"),
                    "apc": c.get("apc"), "suspended": c.get("suspended")}

    def run(self):
        if not self.enabled:
            return
        while True:
            try:
                self.poll()
            except Exception as e:  # never let AEMO take the collector down
                with self.lock:
                    self.state.update(status="error", error=str(e))
            # AEMO publishes each 5-minute interval shortly after it ends
            now = time.time()
            time.sleep(max(30, 300 - now % 300 + 45))

    # -------------------------------------------------------------- polling
    def poll(self):
        if self.simulate:
            return self._poll_sim()
        upd, errs = {}, {}
        for name, fn in (("prices", self._prices), ("summary", self._summary), ("fuel", self._fuel)):
            try:
                upd.update(fn())
            except Exception as e:
                errs[name] = str(e)
        if errs and errs != self.errors:
            self.log("AEMO:", "; ".join(dict.fromkeys(errs.values())))
        self.errors = errs
        with self.lock:
            cur = dict(self.state.get("current") or {})
            cur.update(upd.pop("current", {}))
            self.state.update(upd)
            self.state["current"] = cur or None
            self.state["status"] = "ok" if len(errs) < 3 else "error"
            self.state["error"] = "; ".join(dict.fromkeys(errs.values())) or None
            self.state["updated"] = time.time()

    def _prices(self):
        data = _request("5MIN", {"timeScale": ["30MIN"]})
        rows = [r for r in data.get("5MIN", []) if r.get("REGIONID") == self.region]
        actual, forecast = [], []
        for r in rows:
            t, rrp = _nem_ts(r["SETTLEMENTDATE"]), _num(r.get("RRP"))
            if rrp is None:
                continue
            if r.get("PERIODTYPE") == "ACTUAL":
                actual.append((int(t - 300), self.region, rrp, _num(r.get("TOTALDEMAND"))))
            elif r.get("PERIODTYPE") == "FORECAST":
                forecast.append({"t": t - 1800, "end": t, "rrp": rrp, "price_c": round(rrp / 10, 2),
                                 "demand_mw": _num(r.get("TOTALDEMAND"))})
        if actual:
            self.store.save_aemo(actual)
        out = {"forecast": sorted(forecast, key=lambda f: f["t"])}
        if actual:
            last = max(actual)
            out["current"] = {"rrp": last[2], "price_c": round(last[2] / 10, 2), "at": last[0] + 300,
                              "demand_mw": last[3]}
        return out

    def _summary(self):
        data = _request("ELEC_NEM_SUMMARY")
        row = next((r for r in data.get("ELEC_NEM_SUMMARY", []) if r.get("REGIONID") == self.region), None)
        if not row:
            raise AemoError(f"no ELEC_NEM_SUMMARY row for {self.region}")
        flows = row.get("INTERCONNECTORFLOWS") or "[]"
        if isinstance(flows, str):
            try:
                flows = json.loads(flows)
            except ValueError:
                flows = []
        ics = [{"name": f.get("name"), "mw": _num(f.get("value")), "export_limit": _num(f.get("exportlimit")),
                "import_limit": _num(f.get("importlimit"))} for f in flows if isinstance(f, dict)]
        rrp = _num(row.get("PRICE"))
        cur = {"price_status": row.get("PRICE_STATUS"), "demand_mw": _num(row.get("TOTALDEMAND")),
               "net_interchange_mw": _num(row.get("NETINTERCHANGE")),
               "scheduled_gen_mw": _num(row.get("SCHEDULEDGENERATION")),
               "semischeduled_gen_mw": _num(row.get("SEMISCHEDULEDGENERATION")),
               "apc": str(row.get("APCFLAG")) == "1", "suspended": str(row.get("MARKETSUSPENDEDFLAG")) == "1"}
        if rrp is not None:
            cur.update(rrp=rrp, price_c=round(rrp / 10, 2))
            if row.get("SETTLEMENTDATE"):
                cur["at"] = _nem_ts(row["SETTLEMENTDATE"])
        return {"current": cur, "interconnectors": ics}

    def _fuel(self):
        data = _request("FUEL", {"type": ["CURRENT"]})
        rows = [r for r in data.get("FUEL_CURRENT", []) if r.get("STATE") == self.region]
        mix = {}
        for r in rows:
            mw = _num(r.get("SUPPLY"))
            if mw is not None and mw > 0:
                mix[r.get("FUEL_TYPE") or "Other"] = mix.get(r.get("FUEL_TYPE") or "Other", 0) + mw
        return self._mix(mix)

    @staticmethod
    def _mix(mix):
        total = sum(mix.values())
        if total <= 0:
            return {"fuel": [], "renewables_pct": None}
        fuel = [{"fuel": k, "mw": round(v), "pct": round(v / total * 100, 1),
                 "renewable": k.lower() in RENEWABLE} for k, v in sorted(mix.items(), key=lambda kv: -kv[1])]
        ren = sum(f["mw"] for f in fuel if f["renewable"])
        return {"fuel": fuel, "renewables_pct": round(ren / total * 100, 1)}

    # -------------------------------------------------------------- simulator
    def _poll_sim(self):
        from amber import Amber  # same synthetic NEM price shape as the Amber simulator
        now = time.time()
        t = now - now % 300
        rrp = lambda ts: round(Amber.sim_price(ts)[2] * 10, 2)
        hist = [(int(t - i * 300), self.region, rrp(t - i * 300),
                 7200 + 1800 * math.sin((t - i * 300) / 13000)) for i in range(0, 288)]
        self.store.save_aemo(hist)
        f0 = t - t % 1800 + 1800
        forecast = [{"t": f0 + i * 1800, "end": f0 + (i + 1) * 1800, "rrp": rrp(f0 + i * 1800),
                     "price_c": round(rrp(f0 + i * 1800) / 10, 2), "demand_mw": None} for i in range(40)]
        h = datetime.fromtimestamp(now, NEM_TZ).hour + 1
        sun = max(0.0, math.sin(math.pi * (h - 6) / 13)) if 6 <= h <= 19 else 0.0
        mix = {"Black coal": 5200 - 1800 * sun, "Solar": 3200 * sun, "Wind": 900 + 300 * random.random(),
               "Hydro": 600 + 400 * (1 - sun), "Gas": 250, "Battery": 120 * (1 - sun)}
        m = self._mix(mix)
        cur = {"rrp": hist[0][2], "price_c": round(hist[0][2] / 10, 2), "at": hist[0][0] + 300,
               "price_status": "FIRM", "demand_mw": round(hist[0][3]), "net_interchange_mw": -640,
               "scheduled_gen_mw": 6900, "semischeduled_gen_mw": 2100, "apc": False, "suspended": False}
        ics = [{"name": "N-Q-MNSP1", "mw": -48, "export_limit": 115, "import_limit": -122},
               {"name": "NSW1-QLD1", "mw": -512, "export_limit": 700, "import_limit": -1200},
               {"name": "VIC1-NSW1", "mw": 820, "export_limit": 1400, "import_limit": -1000}]
        with self.lock:
            self.state.update(status="ok", error=None, current=cur, forecast=forecast, interconnectors=ics,
                              updated=now, **m)

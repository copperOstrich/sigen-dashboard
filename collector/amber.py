"""Amber Electric integration (https://api.amber.com.au/v1, read-only).

Prices are in c/kWh including GST. For the feedIn channel Amber reports the price
from the *bill's* point of view: a negative feed-in perKwh means you are paid to export.
We keep Amber's sign for maths (cost = kWh x perKwh) and flip it only for display
("sell price").

Without AMBER_API_KEY nothing is called. In simulator mode a fake NSW price curve is
generated so the dashboard can be previewed.
"""
import json
import math
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.amber.com.au/v1"


class AmberError(Exception):
    pass


class AmberClient:
    def __init__(self, key):
        self.key = key

    def get(self, path, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        req = urllib.request.Request(f"{API}{path}{'?' + q if q else ''}", headers={
            "Authorization": f"Bearer {self.key}", "Accept": "application/json",
            "User-Agent": "sigen-dashboard/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "ignore")
            if e.code == 401:
                raise AmberError("Amber rejected the API key (401). Create a new one at app.amber.com.au/developers.")
            if e.code == 429:
                raise AmberError("Amber rate limit reached; slowing down.")
            raise AmberError(f"Amber API {e.code}: {body}")
        except urllib.error.URLError as e:
            raise AmberError(f"Can't reach Amber: {e.reason}")


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _slim(iv):
    if not iv:
        return None
    adv = iv.get("advancedPrice") or {}
    return {
        "start": _ts(iv["startTime"]), "end": _ts(iv["endTime"]), "type": iv.get("type"),
        "per_kwh": iv.get("perKwh"), "spot": iv.get("spotPerKwh"), "renewables": iv.get("renewables"),
        "descriptor": iv.get("descriptor"), "spike": iv.get("spikeStatus"), "estimate": iv.get("estimate"),
        "period": (iv.get("tariffInformation") or {}).get("period"),
        "predicted": adv.get("predicted"), "low": adv.get("low"), "high": adv.get("high"),
    }


class Amber:
    """Background poller. Thread-safe snapshot via .snapshot()."""

    def __init__(self, store, key=None, site_id=None, simulate=False, log=print):
        self.store, self.log = store, log
        self.client = AmberClient(key) if key else None
        self.site_id = site_id
        self.simulate = simulate and not key
        self.lock = threading.Lock()
        self.state = {"provider": "Amber", "enabled": bool(self.client) or self.simulate,
                      "mode": "simulator" if self.simulate else "live" if self.client else "off",
                      "status": "starting" if (self.client or self.simulate) else "off",
                      "error": None, "site": None, "current": {}, "forecast": [], "updated": None}
        self.next_forecast = 0
        self.next_usage = 0
        self.price_now = {"general": None, "feedIn": None}

    # -------------------------------------------------------------- public
    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def prices_now(self):
        """(buy c/kWh, feed-in c/kWh in Amber's sign) for cost accounting."""
        with self.lock:
            return self.price_now.get("general"), self.price_now.get("feedIn")

    def summary(self):
        """Compact block embedded in /api/live for the widget."""
        with self.lock:
            cur = self.state.get("current") or {}
            g, f = cur.get("general"), cur.get("feedIn")
            if not g:
                return {"provider": "Amber", "enabled": self.state["enabled"], "status": self.state["status"]}
            return {"provider": "Amber", "enabled": True, "status": self.state["status"], "mode": self.state["mode"],
                    "buy": g["per_kwh"], "sell": -f["per_kwh"] if f and f.get("per_kwh") is not None else None,
                    "descriptor": g["descriptor"], "spike": g["spike"], "renewables": g["renewables"],
                    "estimate": g.get("estimate"), "ends": g["end"], "period": g.get("period")}

    def run(self):
        if not (self.client or self.simulate):
            return
        delay = 60
        while True:
            try:
                self.poll()
                delay = 60
            except Exception as e:
                msg = str(e)
                if self.state.get("error") != msg:
                    self.log("Amber:", msg)
                with self.lock:
                    self.state.update(status="error", error=msg)
                delay = min(600, delay * 2)
            time.sleep(delay)

    # -------------------------------------------------------------- polling
    def poll(self):
        if self.simulate:
            return self._poll_sim()
        if not self.site_id or not self.state.get("site"):
            sites = self.client.get("/sites")
            active = [s for s in sites if s.get("status") == "active"] or sites
            if self.site_id:
                active = [s for s in sites if s["id"] == self.site_id] or active
            if not active:
                raise AmberError("No Amber sites on this account.")
            s = active[0]
            self.site_id = s["id"]
            with self.lock:
                self.state["site"] = {"id": s["id"], "network": s.get("network"),
                                      "interval_minutes": s.get("intervalLength"),
                                      "nmi": "…" + s.get("nmi", "")[-4:],
                                      "channels": [c["type"] for c in s.get("channels", [])]}
            self.log(f"Amber site {s['id']} ({s.get('network')}, {s.get('intervalLength')}-min pricing)")

        now = time.time()
        sid = self.site_id
        cur = self.client.get(f"/sites/{sid}/prices/current", previous=1, next=0)
        current = {}
        for iv in cur:
            if iv.get("type") == "CurrentInterval":
                current[iv["channelType"]] = _slim(iv)
        rows = [(_slim(iv), iv["channelType"]) for iv in cur]
        self.store.save_prices([(r["start"], ch, r["end"], r["per_kwh"], r["spot"], r["renewables"],
                                 r["descriptor"], r["spike"]) for r, ch in rows])
        forecast = self.state.get("forecast") or []
        if now >= self.next_forecast:
            fc = self.client.get(f"/sites/{sid}/prices/current", previous=0, next=48, resolution=30)
            forecast = self._merge_forecast(fc)
            self.next_forecast = now + 300
        if now >= self.next_usage:
            self._sync_usage()
            self.next_usage = now + 6 * 3600
        self._publish(current, forecast)

    def _merge_forecast(self, intervals):
        by = {}
        for iv in intervals:
            s = _slim(iv)
            row = by.setdefault(s["start"], {"t": s["start"], "end": s["end"], "buy": None, "sell": None,
                                             "descriptor": None, "spike": None, "renewables": None,
                                             "type": s["type"], "buy_high": None, "buy_low": None})
            if iv["channelType"] == "general":
                row.update(buy=s["per_kwh"], descriptor=s["descriptor"], spike=s["spike"],
                           renewables=s["renewables"], buy_high=s["high"], buy_low=s["low"])
                if s["predicted"] is not None:
                    row["buy"] = s["predicted"]
            elif iv["channelType"] == "feedIn":
                row["sell"] = -s["per_kwh"] if s["per_kwh"] is not None else None
        return [by[k] for k in sorted(by)]

    def _sync_usage(self):
        """Billing-grade usage & cost from the meter (lags ~1 day). Last 35 days, 7-day windows."""
        end = date.today()
        start = end - timedelta(days=35)
        days = {}
        while start < end:
            stop = min(end, start + timedelta(days=6))
            data = self.client.get(f"/sites/{self.site_id}/usage", startDate=start.isoformat(), endDate=stop.isoformat())
            for u in data:
                d = days.setdefault(u["date"], {"import_kwh": 0.0, "import_cost": 0.0, "export_kwh": 0.0,
                                                "export_cost": 0.0, "estimated": False})
                if u["channelType"] in ("general", "controlledLoad"):
                    d["import_kwh"] += u["kwh"]; d["import_cost"] += u["cost"]
                elif u["channelType"] == "feedIn":
                    d["export_kwh"] += abs(u["kwh"]); d["export_cost"] += u["cost"]
                if u.get("quality") == "estimated":
                    d["estimated"] = True
            start = stop + timedelta(days=1)
        self.store.save_amber_days(days)

    def _publish(self, current, forecast):
        with self.lock:
            self.state.update(status="ok", error=None, current=current, forecast=forecast, updated=time.time())
            g, f = current.get("general"), current.get("feedIn")
            self.price_now = {"general": g["per_kwh"] if g else None, "feedIn": f["per_kwh"] if f else None}

    # -------------------------------------------------------------- simulator
    @staticmethod
    def sim_price(ts):
        """Plausible NSW Amber prices (c/kWh) for a timestamp: (buy, feedIn in Amber sign)."""
        lt = datetime.fromtimestamp(ts, timezone(timedelta(hours=11)))
        h = lt.hour + lt.minute / 60
        rnd = random.Random(int(ts // 1800))
        day = random.Random(lt.date().toordinal())
        solar_dip = math.exp(-((h - 12.5) / 2.6) ** 2)
        evening = math.exp(-((h - 18.5) / 1.6) ** 2)
        spot = 9 - 13 * solar_dip + 22 * evening + rnd.uniform(-2, 2)
        if day.random() > 0.6 and 17.5 <= h < 19:  # some evenings spike
            spot += 60 + 80 * day.random()
        buy = max(4.0, 17 + spot * 1.1 + (6 if 15 <= h < 21 else 0))
        feed = -(spot * 0.95)  # Amber sign: negative = you're paid
        return round(buy, 2), round(feed, 2), round(spot, 2)

    @staticmethod
    def descriptor_for(buy):
        return Amber._descriptor(buy)

    @staticmethod
    def _descriptor(buy):
        return ("extremelyLow" if buy < 8 else "veryLow" if buy < 14 else "low" if buy < 22
                else "neutral" if buy < 32 else "high" if buy < 60 else "spike")

    def _poll_sim(self):
        now = time.time()
        start = now - now % 300
        buy, feed, spot = self.sim_price(start)
        h = datetime.fromtimestamp(now).hour
        ren = max(5, min(95, 30 + 45 * math.exp(-((h - 12.5) / 3) ** 2)))
        mk = lambda p: {"start": start, "end": start + 300, "type": "CurrentInterval", "per_kwh": p, "spot": spot,
                        "renewables": round(ren, 1), "descriptor": self._descriptor(buy),
                        "spike": "spike" if buy >= 60 else "potential" if buy >= 45 else "none",
                        "estimate": True, "period": None, "predicted": None, "low": None, "high": None}
        current = {"general": mk(buy), "feedIn": mk(feed)}
        self.store.save_prices([(start, "general", start + 300, buy, spot, ren, self._descriptor(buy), current["general"]["spike"]),
                                (start, "feedIn", start + 300, feed, spot, ren, None, None)])
        fstart = now - now % 1800
        forecast = []
        for i in range(48):
            t = fstart + i * 1800
            b, f, _ = self.sim_price(t)
            forecast.append({"t": t, "end": t + 1800, "buy": b, "sell": -f, "descriptor": self._descriptor(b),
                             "spike": "spike" if b >= 60 else "none", "renewables": None,
                             "type": "CurrentInterval" if i == 0 else "ForecastInterval",
                             "buy_low": round(b * 0.85, 1) if i else None, "buy_high": round(b * 1.25, 1) if i else None})
        with self.lock:
            self.state["site"] = {"id": "simulated", "network": "Example network", "interval_minutes": 5,
                                  "nmi": "…0000", "channels": ["general", "feedIn"]}
        self._publish(current, forecast)

"""LocalVolts integration (https://api.localvolts.com/v1, read-only).

Needs an API key and Partner ID from localvolts.com under My Profile -> API Key.
The NMI is discovered automatically unless LOCALVOLTS_NMI is set.

LocalVolts publishes one record per 5-minute interval:
  quality "Exp"  the interval in progress (expected price)
  quality "Fcst" forecast intervals (out to ~24 h)
  anything else  settled/actual intervals, with metered costs
Prices used here:
  costsFlexUp     c/kWh  what one more kWh imported costs you now (incl. network and losses)
  earningsFlexUp  c/kWh  what one more kWh exported earns you now
Metered totals per interval: costsAll / earningsAll (cents), importsAll / exportsAll (kWh).

Internally prices are stored with Amber's sign convention so cost maths is shared:
buy = costsFlexUp, feed-in = -earningsFlexUp (negative = you are paid).
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from amber import Amber

API = "https://api.localvolts.com/v1/customer/interval"
FMT = "%Y-%m-%dT%H:%M:%SZ"


class LocalVoltsError(Exception):
    pass


def _ts(iso):
    iso = iso.strip().replace(" ", "T")
    if not iso.endswith("Z") and "+" not in iso[10:]:
        iso += "Z"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class LocalVoltsClient:
    def __init__(self, key, partner):
        self.key, self.partner = key, partner

    def intervals(self, nmi, start, end):
        url = (f"{API}?NMI={nmi}&from={start.strftime(FMT)}&to={end.strftime(FMT)}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"apikey {self.key}", "partner": str(self.partner),
            "Accept": "application/json", "User-Agent": "sigen-dashboard/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "ignore").strip()
            if e.code in (401, 403):
                raise LocalVoltsError(f"LocalVolts rejected the API key or Partner ID ({e.code}): {body}")
            raise LocalVoltsError(f"LocalVolts API {e.code}: {body}")
        except urllib.error.URLError as e:
            raise LocalVoltsError(f"Can't reach LocalVolts: {e.reason}")
        if not isinstance(data, list):
            raise LocalVoltsError(f"Unexpected LocalVolts response: {str(data)[:120]}")
        return data


class LocalVolts:
    """Same public interface as Amber: snapshot(), summary(), prices_now(), run()."""

    def __init__(self, store, key, partner, nmi=None, tz=None, log=print):
        self.store, self.log, self.tz = store, log, tz
        self.client = LocalVoltsClient(key, partner)
        self.nmi = nmi
        self.lock = threading.Lock()
        self.state = {"provider": "LocalVolts", "enabled": True, "mode": "live", "status": "starting",
                      "error": None, "site": None, "current": {}, "forecast": [], "updated": None}
        self.price_now = {"general": None, "feedIn": None}
        self.next_usage = 0
        self.history_done = False

    # -------------------------------------------------------------- public
    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def prices_now(self):
        with self.lock:
            return self.price_now.get("general"), self.price_now.get("feedIn")

    def summary(self):
        with self.lock:
            g = (self.state.get("current") or {}).get("general")
            f = (self.state.get("current") or {}).get("feedIn")
            if not g:
                return {"provider": "LocalVolts", "enabled": True, "status": self.state["status"]}
            return {"provider": "LocalVolts", "enabled": True, "status": self.state["status"], "mode": "live",
                    "buy": g["per_kwh"], "sell": -f["per_kwh"] if f and f.get("per_kwh") is not None else None,
                    "descriptor": g["descriptor"], "spike": g["spike"], "renewables": None,
                    "estimate": True, "ends": g["end"], "period": None}

    def run(self):
        failures = 0
        while True:
            try:
                wait = self.poll()
                failures = 0
            except Exception as e:
                failures += 1
                msg = str(e)
                if self.state.get("error") != msg:
                    self.log("LocalVolts:", msg)
                with self.lock:
                    self.state.update(status="error", error=msg)
                wait = min(300, 15 * 2 ** min(failures, 4))
            time.sleep(wait)

    # -------------------------------------------------------------- polling
    def _discover(self):
        now = datetime.now(timezone.utc)
        data = self.client.intervals("*", now, now + timedelta(minutes=5))
        nmis = sorted({d["NMI"] for d in data if d.get("NMI")})
        if not nmis:
            raise LocalVoltsError("No NMIs found for this LocalVolts account.")
        self.nmi = nmis[0]
        self.log(f"LocalVolts NMI …{self.nmi[-4:]}" + (f" (of {len(nmis)}; set LOCALVOLTS_NMI to choose)" if len(nmis) > 1 else ""))

    def poll(self):
        """Fetch the current interval and 24 h forecast. Returns seconds until the next poll."""
        if not self.nmi:
            self._discover()
        now = datetime.now(timezone.utc)
        data = self.client.intervals(self.nmi, now - timedelta(minutes=5), now + timedelta(hours=24))
        cur, fc = None, []
        for d in data:
            q = str(d.get("quality") or "").lower()
            end = _ts(d["intervalEnd"])
            dur = (_num(d.get("intervalDuration")) or 5) * 60
            row = {"start": end - dur, "end": end, "buy": _num(d.get("costsFlexUp")),
                   "sell": _num(d.get("earningsFlexUp"))}
            if q == "exp" and row["end"] > now.timestamp() - 60:
                cur = row
            elif q == "fcst":
                fc.append(row)
        with self.lock:
            self.state["site"] = {"id": "…" + self.nmi[-4:], "network": None, "interval_minutes": 5,
                                  "nmi": "…" + self.nmi[-4:], "channels": ["general", "feedIn"]}
        if cur:
            buy, sell = cur["buy"], cur["sell"]
            desc = Amber.descriptor_for(buy) if buy is not None else None
            spike = "spike" if desc == "spike" else "potential" if desc == "high" and buy >= 45 else "none"
            mk = lambda p: {"start": cur["start"], "end": cur["end"], "type": "CurrentInterval", "per_kwh": p,
                            "spot": None, "renewables": None, "descriptor": desc, "spike": spike,
                            "estimate": True, "period": None, "predicted": None, "low": None, "high": None}
            current = {"general": mk(buy), "feedIn": mk(-sell if sell is not None else None)}
            self.store.save_prices([(int(cur["start"]), "general", int(cur["end"]), buy, None, None, desc, spike),
                                    (int(cur["start"]), "feedIn", int(cur["end"]),
                                     -sell if sell is not None else None, None, None, None, None)])
        else:
            current = self.state.get("current") or {}
        forecast = self._half_hours(fc)
        with self.lock:
            self.state.update(status="ok" if cur else "waiting", error=None, current=current,
                              forecast=forecast, updated=time.time())
            if cur:
                self.price_now = {"general": cur["buy"], "feedIn": -cur["sell"] if cur["sell"] is not None else None}
        if time.time() >= self.next_usage:
            self._sync_usage()
            self.next_usage = time.time() + 3 * 3600
        # Next interval's price appears ~15-40 s after its boundary
        if cur:
            return max(5, cur["end"] - time.time() + 20)
        return 15

    @staticmethod
    def _half_hours(rows):
        """Average 5-minute forecast intervals into 30-minute steps for the chart."""
        by = {}
        for r in rows:
            b = int(r["start"] // 1800 * 1800)
            by.setdefault(b, []).append(r)
        out = []
        for b in sorted(by):
            g = by[b]
            avg = lambda k: (sum(x[k] for x in g if x[k] is not None) / max(1, sum(1 for x in g if x[k] is not None))
                             if any(x[k] is not None for x in g) else None)
            buy = avg("buy")
            out.append({"t": b, "end": b + 1800, "buy": round(buy, 2) if buy is not None else None,
                        "sell": round(avg("sell"), 2) if avg("sell") is not None else None,
                        "descriptor": Amber.descriptor_for(buy) if buy is not None else None,
                        "spike": "spike" if buy is not None and buy >= 60 else "none",
                        "renewables": None, "type": "ForecastInterval", "buy_low": None, "buy_high": None})
        return out

    def _sync_usage(self):
        """Metered cost per local day: 35 days on first run, then the last 2 days."""
        tz = self.tz or timezone.utc
        today = datetime.now(tz).date()
        span = 2 if self.history_done else 35
        days = {}
        for i in range(span, -1, -1):
            d = today - timedelta(days=i)
            start = datetime(d.year, d.month, d.day, tzinfo=tz)
            data = self.client.intervals(self.nmi, start.astimezone(timezone.utc),
                                         (start + timedelta(days=1)).astimezone(timezone.utc))
            agg = {"import_kwh": 0.0, "import_cost": 0.0, "export_kwh": 0.0, "export_cost": 0.0,
                   "estimated": d == today, "n": 0}
            for r in data:
                if str(r.get("quality") or "").lower() == "fcst":
                    continue
                agg["n"] += 1
                agg["import_kwh"] += _num(r.get("importsAll")) or 0
                agg["export_kwh"] += _num(r.get("exportsAll")) or 0
                agg["import_cost"] += _num(r.get("costsAll")) or 0      # cents
                agg["export_cost"] -= _num(r.get("earningsAll")) or 0   # cents, negative = paid
                if str(r.get("quality") or "").lower() == "exp":
                    agg["estimated"] = True
            if agg.pop("n"):
                days[d.isoformat()] = agg
            if span > 2:
                time.sleep(1.5)  # be gentle with the API during the first backfill
        if days:
            self.store.save_amber_days(days)
        self.history_done = True

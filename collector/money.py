"""Money: what other electricity plans would have cost, solar and battery payback, and
battery trades with the grid (for example Amber SmartShift).

Plan comparison
  Your recorded grid imports and exports, hour by hour, priced on each comparison plan
  (daily supply charge, usage rate, feed-in tariff; flat or time of use), against what you
  actually paid. It uses the same energy flows for every plan: on a different plan the
  battery might be run differently, so treat the result as a guide, not a quote.

Plans come from DATA_DIR/plans.json if present, otherwise from a bundled preset chosen with
COMPARE_PLANS (see collector/plans/). A plan looks like:

  {"name": "Retailer", "plan": "Standing offer, single rate",
   "supply_c_day": 185.0, "usage_c_kwh": 33.7, "feed_in_c_kwh": 3.0}

or, for time of use, rules checked in order (the last one with no conditions is the default):

  "usage": [{"c_kwh": 46.6, "hours": [16, 20], "days": "weekdays", "months": [11, 12, 1, 2, 3]},
            {"c_kwh": 12.3, "hours": [10, 14]},
            {"c_kwh": 35.7}]

"hours": [start, end) in local time; "days": "weekdays" | "weekends" | "all". Public holidays
are treated as ordinary days. "feed_in" takes the same rules as "usage".

Payback
  SYSTEM_COST against savings: what the home and car would have cost from the grid without
  solar and battery, at your retailer's price each minute, minus what you actually paid for
  energy (supply charges are paid either way, so they cancel out). Without per-minute prices
  it falls back to the first comparison plan's rates. Days before the collector started, back
  to SYSTEM_INSTALLED, are estimated at the average daily saving.

Battery trades
  Minutes where the battery sends power to the grid, or charges from it, beyond what the
  house needs: with self-consumption control the battery does neither, so these are almost
  always the retailer's battery control (Amber SmartShift) or VPP events.
"""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PRESETS = Path(__file__).resolve().parent / "plans"
TRADE_MIN_W = 300      # battery-grid power below this is noise
TRADE_GAP_S = 300      # minutes this far apart still belong to one event
TRADE_MIN_KWH = 0.2    # smaller events are ignored


def _rule_matches(rule, lt):
    h = rule.get("hours")
    if h and not (h[0] <= lt.hour < h[1] if h[0] < h[1] else (lt.hour >= h[0] or lt.hour < h[1])):
        return False
    d = rule.get("days", "all")
    if d == "weekdays" and lt.weekday() >= 5 or d == "weekends" and lt.weekday() < 5:
        return False
    m = rule.get("months")
    return not (m and lt.month not in m)


def rate(rules, flat, lt):
    """c/kWh in force at local time lt."""
    if rules:
        for r in rules:
            if _rule_matches(r, lt):
                return float(r.get("c_kwh") or 0)
        return 0.0
    return float(flat or 0)


class Money:
    def __init__(self, store, tz, data_dir, preset="", system_cost=None, installed="", supply_c_day=None,
                 fee_monthly=None, retailer_name="", log=print):
        self.store, self.tz, self.log = store, tz, log
        self.data_dir, self.preset = Path(data_dir), (preset or "").strip()
        self.system_cost = system_cost
        self.installed = None
        if installed:
            try:
                self.installed = date.fromisoformat(installed.strip())
            except ValueError:
                log(f"SYSTEM_INSTALLED '{installed}' isn't a date like 2024-03-15; ignored")
        self.supply_c_day, self.fee_monthly = supply_c_day, fee_monthly
        self.retailer_name = retailer_name or "Your retailer"
        self._plans, self._plans_key = None, None
        self._day_cache = {}      # (day, plans key) -> {"flows": [...], "plans": {...}, "baseline": $}
        self._trade_cache = {}    # day -> [events]
        self._result, self._result_at = None, 0

    # ---------------------------------------------------------------- plans
    def plans(self):
        custom = self.data_dir / "plans.json"
        src = custom if custom.exists() else (PRESETS / f"{self.preset}.json" if self.preset else None)
        if not src or not src.exists():
            if self.preset and not (PRESETS / f"{self.preset}.json").exists():
                self.log(f"COMPARE_PLANS '{self.preset}' isn't a known preset "
                         f"({', '.join(sorted(p.stem for p in PRESETS.glob('*.json')))})")
                self.preset = ""
            return None
        key = (str(src), src.stat().st_mtime)
        if key != self._plans_key:
            try:
                doc = json.loads(src.read_text())
                plans = [p for p in doc.get("plans", []) if p.get("name")]
                self._plans = {"source": "plans.json" if src == custom else f"preset {self.preset}",
                               "title": doc.get("title"), "as_of": doc.get("as_of"), "note": doc.get("note"),
                               "plans": plans}
            except (OSError, ValueError) as e:
                self.log(f"Plans file {src.name}: {e}")
                self._plans = None
            self._plans_key, self._day_cache = key, {}
        return self._plans


    # ---------------------------------------------------------------- per day
    def _day(self, day, plans, today):
        key = (day, self._plans_key)
        if key in self._day_cache and day != today:
            return self._day_cache[key]
        flows = self.store.hourly_flows(day)
        out = {"plans": {}, "baseline": None, "import": 0.0, "export": 0.0, "load": 0.0}
        for p in plans["plans"]:
            usage = feed = 0.0
            for h, imp, exp, load in flows:
                lt = datetime.fromtimestamp(h, self.tz)
                usage += imp * rate(p.get("usage"), p.get("usage_c_kwh"), lt)
                feed += exp * rate(p.get("feed_in"), p.get("feed_in_c_kwh"), lt)
            supply = float(p.get("supply_c_day") or 0)
            out["plans"][p["name"]] = {"supply": supply / 100, "usage": usage / 100, "feed_in": feed / 100,
                                       "total": (supply + usage - feed) / 100}
        base = plans["plans"][0] if plans["plans"] else None
        if base:  # energy only (fallback for payback when there are no per-minute prices)
            out["baseline"] = sum(load * rate(base.get("usage"), base.get("usage_c_kwh"),
                                              datetime.fromtimestamp(h, self.tz)) for h, imp, exp, load in flows) / 100
        for h, imp, exp, load in flows:
            out["import"] += imp; out["export"] += exp; out["load"] += load
        self._day_cache[key] = out
        return out

    def _fixed_per_day(self):
        """Your own retailer's supply charge and membership fee, per day ($), when set."""
        return ((self.supply_c_day or 0) / 100) + ((self.fee_monthly or 0) * 12 / 365)

    # ---------------------------------------------------------------- everything
    def summary(self, max_age=600):
        if self._result and time.time() - self._result_at < max_age:
            return self._result
        try:
            self._result = self._compute()
        except Exception as e:  # never let this take the API down
            self.log("Money:", e)
            self._result = {"enabled": False, "reason": f"couldn't work this out ({e})"}
        self._result_at = time.time()
        return self._result

    def _compute(self):
        now = datetime.now(self.tz)
        today = now.strftime("%Y-%m-%d")
        days = self.store.daily(800)
        priced = [d for d in days if d.get("bought") is not None]
        plans = self.plans()
        fixed = self._fixed_per_day()
        fixed_known = self.supply_c_day is not None
        out = {"enabled": True, "retailer": self.retailer_name, "fixed_known": fixed_known,
               "supply_c_day": self.supply_c_day, "fee_monthly": self.fee_monthly,
               "trades": self.trades(), "plans": None, "compare": None, "payback": None}
        if not priced:
            out["reason"] = "needs at least a day with prices from your retailer"
            return out

        # actual cost per day. Without your own supply charge, compare energy only (plans without
        # their supply charge too), or the comparison would be lopsided.
        actual = {d["day"]: d["bought"] - d["earned"] + fixed for d in priced}
        energy = {d["day"]: d["bought"] - d["earned"] for d in priced}
        plan_total = (lambda v: v["total"]) if fixed_known else (lambda v: v["total"] - v["supply"])
        complete = [d for d in priced if d["day"] != today]

        # ---- comparison
        if plans and plans["plans"]:
            out["plans"] = {k: plans[k] for k in ("source", "title", "as_of", "note")}
            out["plans"]["list"] = [{"name": p["name"], "plan": p.get("plan"), "source": p.get("source"),
                                     "supply_c_day": p.get("supply_c_day"), "usage_c_kwh": p.get("usage_c_kwh"),
                                     "feed_in_c_kwh": p.get("feed_in_c_kwh"), "tou": bool(p.get("usage"))}
                                    for p in plans["plans"]]
            months = {}
            for d in complete:
                r = self._day(d["day"], plans, today)
                m = months.setdefault(d["day"][:7], {"month": d["day"][:7], "days": 0, "actual": 0.0,
                                                     "plans": {p["name"]: 0.0 for p in plans["plans"]}})
                m["days"] += 1
                m["actual"] += actual[d["day"]]
                for n, v in r["plans"].items():
                    m["plans"][n] += plan_total(v)
            rows = []
            for k in sorted(months)[-13:]:
                m = months[k]
                m["actual"] = round(m["actual"], 2)
                m["plans"] = {n: round(v, 2) for n, v in m["plans"].items()}
                rows.append(m)
            # the last 12 months (or everything recorded, if less)
            since = (now - timedelta(days=365)).strftime("%Y-%m-%d")
            window = [d for d in complete if d["day"] > since]
            tot = {"days": len(window), "actual": round(sum(actual[d["day"]] for d in window), 2),
                   "plans": {p["name"]: 0.0 for p in plans["plans"]}}
            for d in window:
                for n, v in self._day(d["day"], plans, today)["plans"].items():
                    tot["plans"][n] += plan_total(v)
            tot["plans"] = {n: round(v, 2) for n, v in tot["plans"].items()}
            if tot["plans"]:
                best = min(tot["plans"], key=tot["plans"].get)
                tot["cheapest"] = best
                tot["saving_vs_cheapest"] = round(tot["plans"][best] - tot["actual"], 2)
            out["compare"] = {"months": rows, "total": tot, "energy_only": not fixed_known}

        # ---- payback (energy only: supply charges are paid with or without solar)
        if self.system_cost:
            per_day, method = [], set()
            for d in complete:
                b = self.store.no_solar_cost(d["day"])
                if b is not None:
                    method.add("prices")
                elif plans and plans["plans"]:
                    b = self._day(d["day"], plans, today)["baseline"]
                    method.add("plan")
                if b is not None:
                    per_day.append((d["day"], b - energy[d["day"]]))
            if per_day:
                tracked = sum(v for _, v in per_day)
                recent = per_day[-365:]
                avg = sum(v for _, v in recent) / len(recent)
                first = date.fromisoformat(per_day[0][0])
                before_days = max(0, (first - self.installed).days) if self.installed else 0
                before = avg * before_days
                saved = tracked + before
                remaining = self.system_cost - saved
                pay = {"cost": self.system_cost, "method": "plan" if method == {"plan"} else "prices",
                       "baseline_plan": plans["plans"][0]["name"] if "plan" in method else None,
                       "tracked": round(tracked, 2),
                       "tracked_days": len(per_day), "since": per_day[0][0],
                       "estimated_before": round(before, 2), "estimated_days": before_days,
                       "installed": self.installed.isoformat() if self.installed else None,
                       "saved": round(saved, 2), "pct": round(max(0, min(100, saved / self.system_cost * 100)), 1),
                       "per_day": round(avg, 2), "per_year": round(avg * 365, 0),
                       "average_days": len(recent), "paid_back": remaining <= 0}
                if remaining > 0 and avg > 0:
                    pay["years_left"] = round(remaining / avg / 365, 1)
                    pay["payback_date"] = (date.today() + timedelta(days=int(remaining / avg))).isoformat()
                out["payback"] = pay
        return out

    # ---------------------------------------------------------------- battery trades
    def trades(self, days=60):
        now = datetime.now(self.tz)
        today = now.strftime("%Y-%m-%d")
        events = []
        for i in range(days, -1, -1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            if day in self._trade_cache and day != today:
                events += self._trade_cache[day]
                continue
            ev = self._trades_day(day)
            self._trade_cache[day] = ev
            events += ev
        # this month and last month, for totals
        month = now.strftime("%Y-%m")
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        tot = {}
        for e in events:
            m = datetime.fromtimestamp(e["start"], self.tz).strftime("%Y-%m")
            if m in (month, prev):
                t = tot.setdefault(m, {"exported_kwh": 0.0, "export_value": 0.0, "imported_kwh": 0.0,
                                       "import_cost": 0.0, "events": 0, "priced": True})
                t["events"] += 1
                if e["kind"] == "export":
                    t["exported_kwh"] += e["kwh"]; t["export_value"] += e["value"] or 0
                else:
                    t["imported_kwh"] += e["kwh"]; t["import_cost"] += e["value"] or 0
                t["priced"] = t["priced"] and e["value"] is not None
        for t in tot.values():
            for k in ("exported_kwh", "export_value", "imported_kwh", "import_cost"):
                t[k] = round(t[k], 2)
        return {"events": list(reversed(events))[:40], "months": tot, "this_month": month, "last_month": prev}

    def _trades_day(self, day):
        rows = self.store.trade_rows(day)
        events, cur = [], None
        prev_ts = None
        for ts, batt, grid, buy_c, sell_c in rows:
            if batt is None or grid is None:
                continue
            gap = ts - prev_ts if prev_ts else 60
            prev_ts = ts
            dt_h = min(gap, 120) / 3600
            kind = w = None
            if batt < -TRADE_MIN_W and grid < -TRADE_MIN_W:      # discharging while exporting
                kind, w = "export", min(-batt, -grid)
            elif batt > TRADE_MIN_W and grid > TRADE_MIN_W:      # charging while importing
                kind, w = "import", min(batt, grid)
            if cur and (kind != cur["kind"] or ts - cur["last"] > TRADE_GAP_S):
                events.append(cur); cur = None
            if not kind:
                continue
            if not cur:
                cur = {"kind": kind, "start": ts, "last": ts, "kwh": 0.0, "value": 0.0, "unpriced": 0.0, "peak_w": 0}
            kwh = w * dt_h / 1000
            cur["kwh"] += kwh
            cur["last"] = ts
            cur["peak_w"] = max(cur["peak_w"], w)
            price = (-sell_c if kind == "export" else buy_c) if (sell_c if kind == "export" else buy_c) is not None else None
            if price is None:
                cur["unpriced"] += kwh
            else:
                cur["value"] += kwh * price / 100
        if cur:
            events.append(cur)
        out = []
        for e in events:
            if e["kwh"] < TRADE_MIN_KWH:
                continue
            priced = e["unpriced"] < e["kwh"] * 0.5
            out.append({"kind": e["kind"], "start": e["start"], "end": e["last"] + 60, "kwh": round(e["kwh"], 2),
                        "peak_w": round(e["peak_w"]), "value": round(e["value"], 2) if priced else None,
                        "avg_c": round(e["value"] * 100 / e["kwh"], 1) if priced and e["kwh"] else None})
        return out

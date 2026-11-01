"""Solar string health: is any string producing less than it usually does?

Each string's normal share of the solar is learned for every hour of the day from the three
weeks before the days being checked, so a string that is always shaded in the afternoon (a tree,
a hip roof) isn't flagged for it. For the last three days, each string's actual energy is
compared with its usual share of what all the strings made, using only hours with a fair amount of
sun (so dull days don't count). Clouds dim every string together, which shares cancel out.

  low   the last two checked days were both under LOW of usual
  down  the latest day was under DOWN of usual, or right now the string is making next to
        nothing for 20 minutes while the others are producing at an hour it normally contributes

New shade, dirt, a failed panel or optimiser, or a tripped string isolator all show up this way.
"""
import time
from datetime import datetime, timedelta

LOW, DOWN = 0.85, 0.4
BASELINE_DAYS, RECENT_DAYS, MIN_BASELINE_DAYS = 21, 3, 7
REFRESH = 900
DEAD_FOR = 20 * 60


class StringsHealth:
    def __init__(self, store, tz, event=None, log=print):
        self.store, self.tz, self.event, self.log = store, tz, event or (lambda k, m: None), log
        self.computed = 0
        self.result = {"enabled": False, "reason": "not checked yet"}
        self.share = {}            # {n: {hour: share}} usual share for each local hour
        self.dead_since = {}       # {n: ts} string at ~0 while others produce
        self.status_was = {}

    def _hourly(self, since):
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT (ts/3600)*3600 AS h, n, SUM(MAX(v*a, 0))/60000.0 FROM string_samples WHERE ts>=? GROUP BY h, n",
                (int(since),)).fetchall()
        out = {}  # {day: {hour: {n: kWh}}}
        for h, n, kwh in rows:
            lt = datetime.fromtimestamp(h, self.tz)
            out.setdefault(lt.date().isoformat(), {}).setdefault(lt.hour, {})[n] = kwh or 0.0
        return out

    def compute(self, now=None):
        now = now or time.time()
        today = datetime.fromtimestamp(now, self.tz).date()
        first_recent = today - timedelta(days=RECENT_DAYS - 1)
        start = first_recent - timedelta(days=BASELINE_DAYS)
        since = datetime.combine(start, datetime.min.time(), self.tz).timestamp()
        data = self._hourly(since)
        ids = sorted({n for d in data.values() for h in d.values() for n in h})
        base_days = [d for d in sorted(data) if d < first_recent.isoformat()]
        self.computed = now
        if len(ids) < 2:
            self.result = {"enabled": False, "reason": "needs two or more strings"}
            return self.result
        if len(base_days) < MIN_BASELINE_DAYS:
            self.result = {"enabled": False, "reason": f"learning: {len(base_days)} of {MIN_BASELINE_DAYS} days so far",
                           "learning_days": len(base_days)}
            return self.result
        # usual share per hour, from summed energy (sunny days weigh more), and the usual hourly total
        sums, totals = {}, {}
        for d in base_days:
            for hr, per in data[d].items():
                tot = sum(per.values())
                totals.setdefault(hr, []).append(tot)
                for n in ids:
                    s = sums.setdefault(n, {})
                    s[hr] = s.get(hr, 0.0) + per.get(n, 0.0)
        hour_total = {hr: sum(sums[n].get(hr, 0.0) for n in ids) for hr in totals}
        typical = {hr: sorted(v)[len(v) // 2] for hr, v in totals.items()}  # median hourly total
        peak = max(typical.values()) if typical else 0
        self.share = {n: {hr: (sums[n].get(hr, 0.0) / hour_total[hr]) for hr in hour_total if hour_total[hr] > 0}
                      for n in ids}
        days = []
        for i in range(RECENT_DAYS):
            d = (first_recent + timedelta(days=i)).isoformat()
            per_hour = data.get(d, {})
            exp, act = {n: 0.0 for n in ids}, {n: 0.0 for n in ids}
            for hr, per in per_hour.items():
                tot = sum(per.values())
                if hr not in typical or typical[hr] < 0.1 * peak or tot < 0.4 * typical[hr]:
                    continue  # dull hour, or an hour that's barely productive anyway
                for n in ids:
                    exp[n] += self.share[n].get(hr, 0.0) * tot
                    act[n] += per.get(n, 0.0)
            days.append({"day": d, "expected": exp, "actual": act})
        strings = []
        for n in ids:
            checked = [(x["day"], x["actual"][n] / x["expected"][n], x["expected"][n], x["actual"][n])
                       for x in days if x["expected"][n] >= 0.3]
            ratios = [r for _, r, _, _ in checked]
            status = "ok"
            if checked and checked[-1][1] < DOWN and checked[-1][2] >= 0.5:
                status = "down"
            elif len(ratios) >= 2 and all(r < LOW for r in ratios[-2:]):
                status = "low"
            if n in self.dead_since and now - self.dead_since[n] >= DEAD_FOR:
                status = "down"
            exp_kwh, act_kwh = sum(c[2] for c in checked), sum(c[3] for c in checked)
            strings.append({"n": n, "status": status,
                            "ratio": round(act_kwh / exp_kwh, 3) if exp_kwh > 0 else None,
                            "days": [{"day": d, "ratio": round(r, 3), "expected_kwh": round(e, 2), "kwh": round(a, 2)}
                                     for d, r, e, a in checked],
                            "usual_share": round(sum(sums[n].values()) / sum(hour_total.values()), 3)
                            if sum(hour_total.values()) else None})
        self._events(strings)
        self.result = {"enabled": True, "checked_at": int(now), "baseline_days": len(base_days),
                       "low_below": LOW, "down_below": DOWN, "strings": strings}
        return self.result

    def _events(self, strings):
        for s in strings:
            was = self.status_was.get(s["n"])
            if was is not None and was != s["status"]:
                pct = f"{round(s['ratio'] * 100)}% of usual" if s["ratio"] is not None else "much less than usual"
                if s["status"] == "down":
                    self.event("alarm", f"Solar string PV{s['n']} is producing almost nothing ({pct})")
                elif s["status"] == "low":
                    self.event("alarm", f"Solar string PV{s['n']} has been producing less than usual for days ({pct})")
                else:
                    self.event("alarm-cleared", f"Solar string PV{s['n']} back to normal ({pct})")
            self.status_was[s["n"]] = s["status"]

    def watch(self, now, strings):
        """Called every poll with the live strings [{n, w}]: notices a string that has stopped."""
        if not strings or not self.share:
            return
        hr = datetime.fromtimestamp(now, self.tz).hour
        for s in strings:
            others = [o["w"] for o in strings if o["n"] != s["n"]]
            busy = others and max(others) >= 1000
            usual = self.share.get(s["n"], {}).get(hr, 0.0)
            if busy and usual >= 0.08 and s["w"] < 0.02 * max(others):
                was_dead = s["n"] in self.dead_since
                self.dead_since.setdefault(s["n"], now)
                if not was_dead:
                    continue
                if now - self.dead_since[s["n"]] >= DEAD_FOR and self.result.get("enabled"):
                    st = next((x for x in self.result["strings"] if x["n"] == s["n"]), None)
                    if st and st["status"] != "down":
                        self.computed = 0  # recheck now
            else:
                self.dead_since.pop(s["n"], None)

    def alarms(self):
        out = []
        for s in (self.result.get("strings") or []) if self.result.get("enabled") else []:
            if s["status"] == "ok":
                continue
            pct = f"{round(s['ratio'] * 100)}% of its usual output" if s["ratio"] is not None else "less than usual"
            out.append({"source": "Dashboard check", "severity": "fault" if s["status"] == "down" else "warning",
                        "message": (f"Solar string PV{s['n']} is producing almost nothing" if s["status"] == "down"
                                    else f"Solar string PV{s['n']} is producing {pct}"),
                        "register": f"string{s['n']}", "bit": None})
        return out

    def due(self, now):
        return now - self.computed >= REFRESH

"""Solar against the weather, over the seasons: slow changes that a day-to-day check can't see.

For each finished day, the solar made in "clean" hours is compared with what the weather model
(weather.py: Open-Meteo's irradiance on your roof faces, kept hour by hour in pv_model) says the
panels could have made in those same hours. Clean hours have a fair amount of sun by the model
and nothing holding the solar back: the battery not full, exports not pinned at the export limit,
and the feed-in price not negative.

  system    the whole array's yield against the model, as a percentage of its own first month.
            A slow, steady fall is dirt or panel ageing (around half a percent a year is normal);
            a seasonal wobble of a few percent is the model, not the panels
  strings   each string's yield against the others, again against its own first month. The
            weather and the model cancel out, but strings on different roof faces trade share with
            the seasons as the sun's path changes, so these are only flagged against the same weeks
            a year earlier: a string down on last year is new shade (a growing tree), dirt on one
            face, or a failing panel or optimiser

Smoothed over 15 days, as single days are noisy (the model can't see every passing cloud).
Finished days are worked out once and kept in pv_index.
"""
import time
from datetime import datetime, timedelta

WINDOW = 15            # days in each smoothed point
BASE_DAYS = 30         # each line's 100% is its first month of clean days
MIN_HOURS = 3          # clean hours a day needs to count
SUN = 0.3              # an hour counts when the model is at least this share of the day's peak


class PvSeason:
    def __init__(self, store, tz, caps=lambda: {}, log=print):
        self.store, self.tz, self.caps, self.log = store, tz, caps, log
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS pv_index(day TEXT, n INTEGER, kwh REAL, model REAL, hours INTEGER, "
                             "PRIMARY KEY(day, n))")  # n 0 = the whole array (from the plant's solar figure)
            store.db.commit()
        self._at = 0

    def _day_bounds(self, day):
        d = datetime.fromisoformat(day).replace(tzinfo=self.tz)
        return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())

    def analyse_day(self, day):
        """[(day, n, kwh, model, hours)] for one finished day, or None when the model doesn't cover it."""
        a, b = self._day_bounds(day)
        with self.store.lock:
            model = dict(self.store.db.execute("SELECT ts, kw FROM pv_model WHERE ts>=? AND ts<?", (a, b)).fetchall())
            hours = self.store.db.execute(
                "SELECT (ts/3600)*3600 AS h, SUM(MAX(pv,0))/60000.0, MAX(soc), MIN(grid), MAX(sell_c), COUNT(*), "
                "AVG(MAX(-grid,0)), MAX(pv) "
                "FROM samples WHERE ts>=? AND ts<? GROUP BY h", (a, b)).fetchall()
            strings = self.store.db.execute(
                "SELECT (ts/3600)*3600 AS h, n, SUM(MAX(v*a,0))/60000.0 FROM string_samples WHERE ts>=? AND ts<? "
                "GROUP BY h, n", (a, b)).fetchall()
        if len(model) < 20 or not hours:
            return None
        peak = max(model.values()) or 0
        if peak <= 0:
            return None
        caps = self.caps() or {}
        cap, pv_cap = caps.get("export_w"), caps.get("pv_w")
        clean = set()
        for h, pv, soc_max, grid_min, sell_max, n, export_avg, pv_max in hours:
            m = model.get(h)
            if m is None or m < SUN * peak or n < 45:
                continue
            pinned = cap is not None and grid_min is not None and -grid_min >= 0.9 * cap
            full = soc_max is not None and soc_max >= 97 and (export_avg or 0) < 300
            ceiling = pv_cap is not None and pv_max is not None and pv_max >= 0.97 * pv_cap
            if pinned or full or ceiling or (sell_max is not None and _negative_feed_in(sell_max)):
                continue
            clean.add(h)
        if len(clean) < MIN_HOURS:
            return [(day, 0, 0.0, 0.0, len(clean))]  # remembered as "no clean hours", not worked out again
        mod = sum(model[h] for h in clean)
        out = [(day, 0, round(sum(pv for h, pv, *_ in hours if h in clean), 3), round(mod, 3), len(clean))]
        per = {}
        for h, n, kwh in strings:
            if h in clean:
                per[n] = per.get(n, 0.0) + (kwh or 0.0)
        out += [(day, n, round(k, 3), round(mod, 3), len(clean)) for n, k in sorted(per.items())]
        return out

    def update(self, now=None, max_days=120):
        """Work out finished days not done yet (the newest first, a few at a time)."""
        now = now or time.time()
        today = datetime.fromtimestamp(now, self.tz).date()
        with self.store.lock:
            done = {r[0] for r in self.store.db.execute("SELECT DISTINCT day FROM pv_index")}
            first = self.store.db.execute("SELECT MIN(ts) FROM pv_model").fetchone()[0]
            first_s = self.store.db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        if not first or not first_s:
            return 0
        start = datetime.fromtimestamp(max(first, first_s), self.tz).date() + timedelta(days=1)
        n, d = 0, today - timedelta(days=1)
        rows = []
        while d >= start and n < max_days:
            day = d.isoformat()
            if day not in done:
                r = self.analyse_day(day)
                if r:
                    rows += r
                n += 1
            d -= timedelta(days=1)
        if rows:
            with self.store.lock:
                self.store.db.executemany("INSERT OR REPLACE INTO pv_index VALUES (?,?,?,?,?)", rows)
                self.store.db.commit()
        return n

    def series(self, days):
        """{day: {"system": %, "strings": {n: %}}} smoothed, for the days asked for; and a summary."""
        if time.time() - self._at > 1800:
            self._at = time.time()
            try:
                self.update()
            except Exception as e:  # never break the chart for it
                self.log(f"Solar vs weather: {type(e).__name__}: {e}")
        with self.store.lock:
            rows = self.store.db.execute("SELECT day, n, kwh, model FROM pv_index WHERE model > 0 ORDER BY day").fetchall()
        by = {}
        for day, n, kwh, model in rows:
            by.setdefault(day, {})[n] = (kwh, model)
        all_days = sorted(by)
        if not all_days:
            return {}, {"enabled": False, "reason": "builds up from the first full days with the weather model"}
        ids = sorted({n for v in by.values() for n in v if n})

        def smooth(i, f):
            lo = max(0, i - WINDOW + 1)
            num = den = 0.0
            for j in range(lo, i + 1):
                r = f(all_days[j])
                if r:
                    num += r[0]; den += r[1]
            return num / den if den else None

        sys_raw = [smooth(i, lambda d: by[d].get(0)) for i in range(len(all_days))]

        def rel(n):  # string's share of all strings' clean-hour energy, as a ratio to an equal share
            def f(d):
                s = by[d]
                tot = sum(s[k][0] for k in ids if k in s)
                return (s[n][0], tot / len(ids)) if n in s and tot > 0 and all(k in s for k in ids) else None
            return [smooth(i, f) for i in range(len(all_days))]
        str_raw = {n: rel(n) for n in ids} if len(ids) >= 2 else {}

        def base(vals):
            v = [x for x in vals[:BASE_DAYS + WINDOW] if x is not None][WINDOW - 1:] or [x for x in vals if x is not None]
            v = sorted(v)
            return v[len(v) // 2] if v else None
        out = {}
        b_sys = base(sys_raw)
        b_str = {n: base(v) for n, v in str_raw.items()}
        for i, d in enumerate(all_days):
            e = {"system": round(sys_raw[i] / b_sys * 100, 1) if sys_raw[i] and b_sys else None,
                 "strings": {n: round(v[i] / b_str[n] * 100, 1) if v[i] and b_str.get(n) else None for n, v in str_raw.items()}}
            out[d] = e
        last = out[all_days[-1]]
        span = (datetime.fromisoformat(all_days[-1]) - datetime.fromisoformat(all_days[0])).days + 1
        summary = {"enabled": True, "days": len(all_days), "span_days": span, "since": all_days[0],
                   "system": last["system"], "strings": last["strings"],
                   "performance": round(b_sys, 3) if b_sys else None, "ready": len(all_days) >= BASE_DAYS + WINDOW}
        flags = []
        if summary["ready"] and last["system"] is not None and last["system"] < 92:
            flags.append(f"the whole array is {100 - last['system']:.0f}% under its first month against the weather")
        # strings: against the same weeks a year earlier, as their shares move with the seasons
        yoy = {}
        if span >= 365 + WINDOW:
            ix = {d: i for i, d in enumerate(all_days)}
            end = datetime.fromisoformat(all_days[-1])
            for n, v in str_raw.items():
                now_v = [v[ix[d]] for d in all_days[-30:] if v[ix[d]]]
                then = [v[ix[d]] for d in all_days if v[ix[d]] and
                        345 <= (end - datetime.fromisoformat(d)).days <= 380]
                if len(now_v) >= 10 and len(then) >= 10:
                    r = (sum(now_v) / len(now_v)) / (sum(then) / len(then))
                    yoy[n] = round(r * 100, 1)
                    if r < 0.93:
                        flags.append(f"PV{n} is making {100 - r * 100:.0f}% less than at this time last year, "
                                     "compared with the other strings")
        summary["strings_vs_last_year"] = yoy
        summary["flags"] = flags
        return {d: out[d] for d in all_days[-days:]}, summary


def _negative_feed_in(sell_c):
    """Amber's sign: feed-in is reported negative when you're paid, so a positive figure means
    exporting costs you (the price for exports went negative)."""
    return sell_c > 0

"""Solar limits: minutes when the panels could have made more than they did, and why.

For each minute of daylight the solar actually produced is compared with what the weather model
expected for that hour (weather.py, calibrated on your own system). A minute counts as limited when
it fell well short of that AND one of these explains it:

  export    exports were pinned at the export limit (EXPORT_LIMIT_KW, or learned from the
            ceiling exports keep hitting)
  negative  the feed-in price was negative (exporting would have cost you) and nothing was
            exported: the system (or Amber SmartShift) turned the solar down on purpose
  inverter  solar was at the inverter's ceiling (learned the same way)
  full      the battery was full and nothing was being exported (a zero-export or tight limit)

The energy lost is the shortfall against the model, so it is an estimate; a cloud passing at the
same moment can't be told apart, which is why only minutes with a clear explanation count. Without
the weather model the minutes are still counted but no kWh is estimated. Finished days are worked
out once and kept.
"""
import time
from datetime import datetime, timedelta

REASONS = ("export", "negative", "inverter", "full")
LEARN_DAYS = 30


class Limits:
    def __init__(self, store, tz, expected=None, export_limit_kw=None, log=print):
        self.store, self.tz, self.expected, self.log = store, tz, expected or (lambda h: None), log
        self.export_setting = export_limit_kw * 1000 if export_limit_kw else None
        self._rows = 0
        self.caps = {"export_w": self.export_setting, "export_source": "setting" if self.export_setting else None,
                     "pv_w": None, "learned_at": 0}
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS limit_days(day TEXT, reason TEXT, minutes INTEGER, kwh REAL, "
                             "value REAL, modelled INTEGER, PRIMARY KEY(day, reason))")
            store.db.commit()

    # ---------------------------------------------------------------- ceilings
    @staticmethod
    def _ceiling(values, tol, min_minutes=45, floor=500):
        """A level the values keep running into: a high percentile with many minutes close to it."""
        vals = sorted(v for v in values if v and v > floor)
        if len(vals) < min_minutes:
            return None
        top = vals[int(len(vals) * 0.998)]
        near = sum(1 for v in vals if v >= top * (1 - tol))
        return top if near >= min_minutes else None

    def learn(self, now=None):
        now = now or time.time()
        if now - self.caps["learned_at"] < 3600:
            return self.caps
        with self.store.lock:
            rows = self.store.db.execute("SELECT -grid, pv FROM samples WHERE ts>=? AND pv > 300",
                                         (int(now - LEARN_DAYS * 86400),)).fetchall()
        if not self.export_setting:
            cap = self._ceiling([e for e, _ in rows], 0.03)
            self.caps.update(export_w=round(cap) if cap else None, export_source="learned" if cap else None)
        pv = self._ceiling([p for _, p in rows], 0.015, min_minutes=90)
        self.caps.update(pv_w=round(pv) if pv else None, learned_at=now)
        return self.caps

    # ---------------------------------------------------------------- one day
    def _day_bounds(self, day):
        d = datetime.fromisoformat(day).replace(tzinfo=self.tz)
        return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())

    def analyse_day(self, day):
        """{reason: {minutes, kwh, value}} for a local day, plus whether the model was available."""
        t0, t1 = self._day_bounds(day)
        caps = self.caps
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT ts, pv, battery, grid, soc, sell_c FROM samples WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1)).fetchall()
        out = {r: {"minutes": 0, "kwh": 0.0, "value": 0.0} for r in REASONS}
        modelled = False
        for ts, pv, batt, grid, soc, sell in rows:
            if pv is None or grid is None:
                continue
            exp = self.expected(ts // 3600 * 3600)
            if exp is not None:
                modelled = True
            if (exp or 0) < 300 and pv < 300:
                continue  # night, or too little sun to matter
            export = -grid
            reason = None
            if sell is not None and sell > 0 and export < 250:
                reason = "negative"
            elif caps.get("export_w") and export >= caps["export_w"] * 0.96:
                reason = "export"
            elif caps.get("pv_w") and pv >= caps["pv_w"] * 0.97:
                reason = "inverter"
            elif soc is not None and soc >= 97 and abs(grid) < 250 and (batt or 0) < 150:
                reason = "full"
            if reason is None:
                continue
            short = None
            if exp is not None:
                short = exp - pv
                if short < max(250.0, 0.1 * exp):
                    continue  # producing about what the sun allowed
            elif reason in ("inverter", "full"):
                continue  # without the model these can't be told from a sunny peak or a quiet hour
            o = out[reason]
            o["minutes"] += 1
            if short is not None:
                kwh = short / 60000.0
                o["kwh"] += kwh
                if sell is not None:
                    # export, inverter, full: what exporting it would have earned. negative: what it
                    # would have cost to export, so a saving (shown as a negative value)
                    o["value"] += kwh * (-sell) / 100.0
        for r in REASONS:
            out[r]["kwh"] = round(out[r]["kwh"], 3) if modelled else None
            out[r]["value"] = round(out[r]["value"], 3) if modelled else None
        self._rows = len(rows)
        return out, modelled

    # ---------------------------------------------------------------- many days
    def days(self, n=30, now=None):
        now = now or time.time()
        self.learn(now)
        today = datetime.fromtimestamp(now, self.tz).date()
        first = self.store.first_day()
        if not first:
            return []
        with self.store.lock:
            cached = {}
            for day, reason, mins, kwh, value, modelled in self.store.db.execute(
                    "SELECT day, reason, minutes, kwh, value, modelled FROM limit_days WHERE day>=?",
                    ((today - timedelta(days=n)).isoformat(),)):
                cached.setdefault(day, {"modelled": bool(modelled)})[reason] = {"minutes": mins, "kwh": kwh, "value": value}
        out = []
        for i in range(n - 1, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            if first and day < first:
                continue
            if day in cached:
                rec = cached[day]
            else:
                res, modelled = self.analyse_day(day)
                rec = {"modelled": modelled, **res}
                # keep finished days; a day inside the model's reach without it may get it later
                if i > 0 and self._rows and (modelled or i > 13):
                    with self.store.lock:
                        self.store.db.executemany("INSERT OR REPLACE INTO limit_days VALUES (?,?,?,?,?,?)",
                                                  [(day, r, res[r]["minutes"], res[r]["kwh"], res[r]["value"], int(modelled))
                                                   for r in REASONS])
                        self.store.db.commit()
            out.append({"day": day, **rec})
        return out

    def summary(self, n=30, now=None):
        now = now or time.time()
        days = self.days(n, now)
        month = datetime.fromtimestamp(now, self.tz).strftime("%Y-%m")

        def total(rows):
            t = {r: {"minutes": 0, "kwh": 0.0, "value": 0.0} for r in REASONS}
            for d in rows:
                for r in REASONS:
                    x = d.get(r) or {}
                    t[r]["minutes"] += x.get("minutes") or 0
                    t[r]["kwh"] += x.get("kwh") or 0
                    t[r]["value"] += x.get("value") or 0
            for r in REASONS:
                t[r]["kwh"], t[r]["value"] = round(t[r]["kwh"], 1), round(t[r]["value"], 2)
            return t

        return {"caps": {k: v for k, v in self.caps.items() if k != "learned_at"},
                "modelled": any(d.get("modelled") for d in days), "days": days,
                "this_month": total([d for d in days if d["day"].startswith(month)]), "period": total(days)}

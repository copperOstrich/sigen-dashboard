"""Always-on use: what the house draws when nothing much is running.

Each night, the home's use (without the car) between midnight and 5 am is sorted and its 10th
percentile taken: the level the house never really drops below, made of the fridge and freezer,
network gear, standby power, pumps and anything left on. A creeping baseline is easy to miss
because it's spread across every hour of the year; at 100 W it is 876 kWh a year.

  now        median of the last 7 nights (one odd night doesn't move it)
  before     median of the 7 nights four weeks earlier, for the change
  per year   now x 8760 h, priced at your average buy price over the last 30 days
"""
import statistics
import time
from datetime import datetime, timedelta

NIGHT = (0, 5)  # local hours: midnight to 5 am


def baseload(store, tz, days=90, now=None):
    now = now or time.time()
    today = datetime.fromtimestamp(now, tz).date()
    since = datetime.combine(today - timedelta(days=days), datetime.min.time(), tz).timestamp()
    with store.lock:
        rows = store.db.execute("SELECT ts, load, ev FROM samples WHERE ts>=? AND load IS NOT NULL", (int(since),)).fetchall()
        price = store.db.execute(
            "SELECT SUM(MAX(grid, 0) * buy_c) / NULLIF(SUM(MAX(grid, 0)), 0) FROM samples WHERE ts>=? AND buy_c IS NOT NULL",
            (int(now - 30 * 86400),)).fetchone()[0]
        use = store.db.execute(
            "SELECT AVG(MAX(load - COALESCE(ev, 0), 0)) FROM samples WHERE ts>=?", (int(now - 30 * 86400),)).fetchone()[0]
    nights = {}
    for ts, load, ev in rows:
        lt = datetime.fromtimestamp(ts, tz)
        if NIGHT[0] <= lt.hour < NIGHT[1]:
            nights.setdefault(lt.date().isoformat(), []).append(max(0.0, load - (ev or 0)))
    series = []
    for day in sorted(nights):
        vals = sorted(nights[day])
        if len(vals) >= 150:  # at least half the night recorded
            series.append({"day": day, "w": round(vals[int(len(vals) * 0.1)])})
    if len(series) < 3:
        return {"enabled": False, "reason": "needs a few nights of readings", "nights": len(series)}
    last = [x["w"] for x in series[-7:]]
    cut0, cut1 = (today - timedelta(days=35)).isoformat(), (today - timedelta(days=28)).isoformat()
    before = [x["w"] for x in series if cut0 <= x["day"] < cut1]
    now_w = statistics.median(last)
    out = {"enabled": True, "now_w": round(now_w), "nights": series,
           "lowest_w": min(x["w"] for x in series), "highest_w": max(x["w"] for x in series),
           "before_w": round(statistics.median(before)) if len(before) >= 3 else None,
           "kwh_year": round(now_w * 8.76), "price_c": round(price, 1) if price else None,
           "share": round(now_w / use * 100) if use else None}
    out["cost_year"] = round(out["kwh_year"] * price / 100) if price else None
    if out["before_w"] is not None:
        out["change_w"] = out["now_w"] - out["before_w"]
    return out

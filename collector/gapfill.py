"""Filling in the minutes the collector missed (the container restarted, the NAS was off, the network
dropped), from the SigenStor's own counters.

The inverter keeps no history that Modbus can read, but its lifetime counters (solar, home use,
bought, sold, charged, discharged) keep counting while the collector is away. So the first reading
afterwards says exactly how much energy went each way during the gap, and today's counters say
exactly how much of it came after midnight. The energy is exact; how it was spread across the
minutes is estimated:

  solar      shaped by the weather model for those hours (pv_model), or by daylight without one
  home use   shaped by your usual use for each hour of the day
  battery and grid
             charging and selling where solar was more than home use, discharging and buying
             where it was less, each scaled to its exact total
  charge     from the battery's charge before and after, following the energy in and out

Filled minutes are marked est = 1. Daily and monthly totals, money and payback use them (their
totals are exact); the charts mark them as estimated; analyses that need real minute-by-minute
behaviour (solar limits, solar vs weather, trading, always-on use, the usual-use profile) leave
them out. Prices come from the retailer's intervals already saved, where there are any. Gaps
shorter than MIN_GAP (a settings restart) aren't filled; nor are ones longer than MAX_GAP, or
where a counter went backwards (an inverter swapped or reset).
"""
import math
import time
from datetime import datetime, timedelta

MIN_GAP = 5 * 60
MAX_GAP = 31 * 86400
COUNTERS = ["total_pv_kwh", "total_load_kwh", "total_import_kwh", "total_export_kwh",
            "total_charge_kwh", "total_discharge_kwh"]


def _sun(lt):
    """A plain daylight shape (6 am to 7 pm) when there's no weather model."""
    h = lt.hour + lt.minute / 60
    return max(0.0, math.sin(math.pi * (h - 6) / 13)) if 6 <= h <= 19 else 0.0


def _scale(values, total, idx=None):
    """values scaled to add up to total (over idx, or all); spread evenly if they're all zero."""
    idx = list(range(len(values))) if idx is None else idx
    if not idx or total is None:
        return
    s = sum(values[i] for i in idx)
    for i in idx:
        values[i] = (values[i] / s * total) if s > 0 else total / len(idx)


def plan(last, now_counters, now, tz, daily_now=None, model=None, profile=None, prices=None,
         capacity_kwh=None, soc_now=None, soc_min=None):
    """The filled minutes for a gap, or (None, reason).

    last: {"ts", "soc", COUNTERS...} the last reading before the gap; now_counters: COUNTERS now;
    daily_now: today's counters now {"pv", "load", "charge", "discharge"} (for the midnight split);
    model: {hour start: kW} weather model; profile: {hour: share of the day's home use};
    prices: f(ts) -> (buy_c, sell_c) in the retailer's sign."""
    t_last = int(last["ts"])
    gap = now - t_last
    if gap < MIN_GAP:
        return None, "short"
    if gap > MAX_GAP:
        return None, f"too long ({gap / 86400:.0f} days)"
    d = {}
    for c in COUNTERS:
        a, b = last.get(c), now_counters.get(c)
        if a is None or b is None:
            return None, f"no {c} reading"
        if b < a - 0.05:
            return None, f"{c} went backwards (an inverter reset or swap)"
        d[c] = max(0.0, b - a)
    minutes = list(range(t_last + 60, int(now // 60) * 60, 60))
    if not minutes:
        return None, "short"
    n = len(minutes)
    lts = [datetime.fromtimestamp(t, tz) for t in minutes]
    # the minutes after the last midnight in the gap get today's counters exactly
    midnight = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [i for i, t in enumerate(minutes) if t >= midnight]
    before = [i for i, t in enumerate(minutes) if t < midnight]

    def split(values, total, key):
        exact = (daily_now or {}).get(key)
        if before and today and exact is not None and 0 <= exact <= total + 0.05:
            _scale(values, total - min(exact, total), before)
            _scale(values, min(exact, total), today)
        else:
            _scale(values, total)

    pv = [((model or {}).get(t // 3600 * 3600) if model else None) for t in minutes]
    pv = [p if p is not None else _sun(lt) for p, lt in zip(pv, lts)]
    split(pv, d["total_pv_kwh"], "pv")
    load = [((profile or {}).get(lt.hour) or 1 / 24) for lt in lts]
    split(load, d["total_load_kwh"], "load")
    surplus = [max(0.0, a - b) for a, b in zip(pv, load)]
    deficit = [max(0.0, b - a) for a, b in zip(pv, load)]
    chg, exp = surplus[:], surplus[:]
    dis, imp = deficit[:], deficit[:]
    split(chg, d["total_charge_kwh"], "charge")
    split(dis, d["total_discharge_kwh"], "discharge")
    _scale(exp, d["total_export_kwh"])
    _scale(imp, d["total_import_kwh"])

    # the battery's charge: following the energy in and out, bent to end where it is now
    soc0 = last.get("soc")
    socs = [None] * n
    if soc0 is not None:
        end = soc_now if soc_now is not None else soc0
        cum, net = 0.0, []
        for i in range(n):
            cum += chg[i] - dis[i]
            net.append(cum)
        for i in range(n):
            raw = soc0 + (net[i] / capacity_kwh * 100 if capacity_kwh else 0)
            drift = (end - (soc0 + (net[-1] / capacity_kwh * 100 if capacity_kwh else 0))) * (i + 1) / n
            floor = min(v for v in (soc_min if soc_min is not None else 0.0, soc0, end) if v is not None)
            socs[i] = round(max(floor, min(100.0, raw + drift)), 1)  # never below the reserve it keeps

    rows, run = [], {c: last[c] for c in COUNTERS}
    for i, t in enumerate(minutes):
        run["total_pv_kwh"] += pv[i]; run["total_load_kwh"] += load[i]
        run["total_import_kwh"] += imp[i]; run["total_export_kwh"] += exp[i]
        run["total_charge_kwh"] += chg[i]; run["total_discharge_kwh"] += dis[i]
        buy, sell = prices(t) if prices else (None, None)
        k = 60000.0  # kWh in a minute -> W
        rows.append((t, lts[i].strftime("%Y-%m-%d"), pv[i] * k, load[i] * k, (chg[i] - dis[i]) * k,
                     (imp[i] - exp[i]) * k, socs[i], *[run[c] for c in COUNTERS], buy, sell, None, 1))
    summary = {"from": t_last, "to": int(now), "minutes": n, "pv_kwh": round(d["total_pv_kwh"], 2),
               "load_kwh": round(d["total_load_kwh"], 2), "import_kwh": round(d["total_import_kwh"], 2),
               "export_kwh": round(d["total_export_kwh"], 2), "midnight_split": bool(before and today and daily_now)}
    return rows, summary


def describe(s, tz):
    a, b = datetime.fromtimestamp(s["from"], tz), datetime.fromtimestamp(s["to"], tz)
    fmt = lambda x: f"{x.hour % 12 or 12}:{x:%M} {'am' if x.hour < 12 else 'pm'}"
    when = (f"{fmt(a)} to {fmt(b)}" if a.date() == b.date() else f"{a:%a} {fmt(a)} to {b:%a} {fmt(b)}")
    hrs = (s["to"] - s["from"]) / 3600
    span = f"{hrs:.1f} h" if hrs >= 1 else f"{round(hrs * 60)} min"
    return (f"The collector was off {when} ({span}). Filled in from the inverter's counters: "
            f"{s['pv_kwh']:.1f} kWh solar, {s['load_kwh']:.1f} kWh used, {s['import_kwh']:.1f} kWh bought, "
            f"{s['export_kwh']:.1f} kWh sold; the minute-by-minute shape is estimated")

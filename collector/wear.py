"""Battery wear: how hard the battery is working and how its health is holding up.

  cycles       the battery's lifetime discharge counter divided by its rated capacity
               (one cycle = the whole battery's worth of energy out once)
  throughput   lifetime energy in and out; many warranties cap this as well as years
  health       the battery's own state-of-health reading, recorded daily; once it has moved
               enough, a straight-line trend projects where it will be when the warranty ends

Warranty terms differ by product and country; BATTERY_WARRANTY_YEARS, BATTERY_WARRANTY_MWH and
BATTERY_WARRANTY_SOH come from your warranty document.
"""
import time
from datetime import date, datetime, timedelta


def _fit(points):
    """Least-squares slope and intercept of [(x, y)]."""
    n = len(points)
    sx = sum(x for x, _ in points); sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points); sxy = sum(x * y for x, y in points)
    d = n * sxx - sx * sx
    if not d:
        return None, None
    m = (n * sxy - sx * sy) / d
    return m, (sy - m * sx) / n


def battery_wear(store, live, tz, installed=None, warranty_years=10, warranty_mwh=None, warranty_soh=None, now=None):
    now = now or time.time()
    b = (live or {}).get("battery") or {}
    rated = b.get("rated_kwh")
    with store.lock:
        row = store.db.execute("SELECT total_charge_kwh, total_discharge_kwh FROM samples WHERE total_discharge_kwh IS NOT NULL "
                               "ORDER BY ts DESC LIMIT 1").fetchone()
        start = store.db.execute("SELECT day, total_charge_kwh, total_discharge_kwh FROM samples WHERE total_discharge_kwh "
                                 "IS NOT NULL ORDER BY ts LIMIT 1").fetchone()
        first = start[0] if start else None
    if not row or not rated:
        return {"enabled": False, "reason": "no battery readings yet"}
    charged, discharged = row
    today = datetime.fromtimestamp(now, tz).date()
    try:
        since = date.fromisoformat(installed) if installed else None
    except ValueError:
        since = None
    lifetime = since is not None
    if not lifetime:
        # no install date: the battery's counters may include time before recording began, so
        # count only what went through it since then
        since = date.fromisoformat(first) if first else today
        # add up the recorded days (robust to a counter that was reset along the way)
        hist = store.daily(800)
        charged = sum(d.get("charge") or 0 for d in hist)
        discharged = sum(d.get("discharge") or 0 for d in hist)
    days = max(1, (today - since).days)
    cycles = discharged / rated

    # the last 30 days, from the daily totals
    recent = [d for d in store.daily(31) if d.get("discharge") is not None and d["day"] < today.isoformat()][-30:]
    dis30 = [d["discharge"] for d in recent]
    per_day = sum(dis30) / len(dis30) / rated if dis30 else None
    deep = sum(1 for x in dis30 if x >= 0.8 * rated)

    # health trend from the daily averages the Trends panel records
    stats = store.daily_stats((today - timedelta(days=800)).isoformat())
    soh = [(d, v["soh"]["avg"]) for d, v in stats.items() if v.get("soh") and v["soh"]["avg"]]
    temps = [v["battery_temp"] for d, v in stats.items()
             if v.get("battery_temp") and d >= (today - timedelta(days=30)).isoformat()]
    health = {"now": b.get("soh"), "first": round(soh[0][1], 1) if soh else None,
              "first_day": soh[0][0] if soh else None, "days": len(soh), "per_year": None, "at_warranty_end": None}
    end = None
    if warranty_years:
        try:
            end = since.replace(year=since.year + int(warranty_years))
        except ValueError:  # 29 February
            end = since + timedelta(days=round(365.25 * warranty_years))
    if len(soh) >= 30:
        pts = [((date.fromisoformat(d) - since).days / 365.25, v) for d, v in soh]
        slope, icpt = _fit(pts)
        span = pts[-1][0] - pts[0][0]
        moved = max(v for _, v in soh) - min(v for _, v in soh)
        # health is reported in coarse steps and can jump on recalibration: only trust a trend over
        # four months or more that has moved at least a point
        if slope is not None and span >= 120 / 365.25 and moved >= 1.0:
            health["per_year"] = round(slope, 2)
            if end and slope < 0:
                health["at_warranty_end"] = round(icpt + slope * (end - since).days / 365.25, 1)

    out = {"enabled": True, "rated_kwh": rated, "since": since.isoformat(), "days": days, "lifetime": lifetime,
           "charged_mwh": round(charged / 1000, 2), "discharged_mwh": round(discharged / 1000, 2),
           "cycles": round(cycles, 1), "cycles_per_day": round(cycles / days, 2),
           "recent": {"days": len(dis30), "cycles_per_day": round(per_day, 2) if per_day is not None else None,
                      "deep_days": deep},
           "round_trip": round(discharged / charged * 100, 1) if charged > 50 else None,
           "health": health,
           "temps": {"avg": round(sum(t["avg"] for t in temps if t.get("avg") is not None) / len(temps), 1) if temps else None,
                     "max": round(max(t["max"] for t in temps if t.get("max") is not None), 1) if temps else None}}
    if end:
        left = max(0, (end - today).days)
        rate = (per_day if per_day is not None else cycles / days)
        w = {"years": warranty_years, "ends": end.isoformat(), "days_left": left,
             "cycles_at_end": round(cycles + rate * left), "soh_floor": warranty_soh}
        if warranty_mwh:
            # the average since the start once it spans a few months (it evens out the seasons),
            # otherwise the last 30 days
            if days >= 90 or per_day is None:
                mwh_rate, basis = (discharged / 1000) / days, "since"
            else:
                mwh_rate, basis = per_day * rated / 1000, "recent"
            w["mwh"] = warranty_mwh
            w["mwh_used_pct"] = round(discharged / 1000 / warranty_mwh * 100, 1)
            w["mwh_at_end"] = round(discharged / 1000 + mwh_rate * left, 1)
            w["mwh_per_year"] = round(mwh_rate * 365.25, 2)
            w["mwh_basis"] = basis
            if mwh_rate > 0:
                reach = max(0.0, (warranty_mwh - discharged / 1000) / mwh_rate)
                w["mwh_projected"] = (today + timedelta(days=int(reach))).isoformat()
                w["mwh_reached"] = w["mwh_projected"] if reach < left else None
        out["warranty"] = w
    return out

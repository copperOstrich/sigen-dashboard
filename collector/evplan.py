"""Car charging plan: the cheapest times to charge the car with the kWh it needs before you leave.

Uses the battery outlook's 15-minute projection (spare solar that would otherwise be exported) and
the retailer's price forecast. Each step's cost per kWh at the charger's rate: spare solar at the
feed-in price it would have earned, the rest at the buy price. The house battery isn't counted on
(most systems don't charge the car from it). The cheapest steps before the deadline are picked;
one unbroken block is preferred when it costs within 5% (or 10c) of the cheapest split, which
would mean pausing and resuming the charger.
"""
from datetime import datetime, timedelta

STEP = 900


def ev_plan(rows, forecast, kwh, by_ts, kw, now):
    if not rows:
        return {"enabled": False, "reason": "waiting for the battery outlook"}
    price = {}
    for p in forecast or []:
        if p.get("buy") is not None:
            for t in range(int(p["t"]) // STEP * STEP, int(p["end"]), STEP):
                price[t] = (p["buy"], p.get("sell") if p.get("sell") is not None else 0.0)
    steps = []
    for r in rows:
        t = int(r["t"]) // STEP * STEP
        if t < now // STEP * STEP or t >= by_ts:
            continue
        if t not in price:
            continue
        buy, sell = price[t]
        spare = (r.get("grid_out") or 0) * 4  # kW of solar that would be exported
        solar = min(kw, spare)
        c_kwh = (solar * sell + (kw - solar) * buy) / kw
        steps.append({"t": t, "c": c_kwh, "solar_kw": solar, "buy": buy})
    if not steps:
        return {"enabled": False, "reason": "no price forecast before then"}
    per_step = kw * STEP / 3600
    need = kwh

    def take(order):
        got, cost, solar, chosen = 0.0, 0.0, 0.0, []
        for s in order:
            if got >= need - 1e-6:
                break
            k = min(per_step, need - got)
            got += k; cost += k * s["c"] / 100; solar += k * s["solar_kw"] / kw
            chosen.append({**s, "kwh": k})
        return got, cost, solar, chosen

    got, cost, solar, chosen = take(sorted(steps, key=lambda s: (s["c"], s["t"])))
    now_got, now_cost, _s, _c = take(steps)  # straight away, from now
    # one unbroken block is easier to live with: use the cheapest one if it costs within 5% (or 10c)
    n = max(1, -(-int(round(need * 1000)) // int(round(per_step * 1000))))
    block = None
    for i in range(len(steps) - n + 1):
        run = steps[i:i + n]
        if run[-1]["t"] - run[0]["t"] != (n - 1) * STEP:
            continue  # a gap in the forecast
        g, c, so, ch = take(run)
        if g >= got - 1e-6 and (block is None or c < block[1]):
            block = (g, c, so, ch)
    if block and block[1] <= max(cost * 1.05, cost + 0.10):
        got, cost, solar, chosen = block
    chosen.sort(key=lambda s: s["t"])
    windows = []
    for s in chosen:
        if windows and s["t"] == windows[-1]["end"]:
            windows[-1]["end"] = s["t"] + STEP; windows[-1]["kwh"] += s["kwh"]
        else:
            windows.append({"start": s["t"], "end": s["t"] + STEP, "kwh": s["kwh"]})
    for w in windows:
        w["kwh"] = round(w["kwh"], 1)
    return {"enabled": True, "kwh": round(got, 1), "wanted_kwh": kwh, "short_kwh": round(max(0.0, kwh - got), 1),
            "kw": kw, "by": by_ts, "cost": round(cost, 2), "avg_c": round(cost / got * 100, 1) if got else None,
            "solar_kwh": round(solar, 1), "windows": windows,
            "now_cost": round(now_cost, 2) if now_got >= got - 1e-6 else None,
            "saving": round(now_cost - cost, 2) if now_got >= got - 1e-6 else None,
            "until": max(s["t"] for s in steps) + STEP}


def deadline(by, tz, now):
    """'07:30' -> the next time it's 07:30 local (today if still ahead, else tomorrow)."""
    h, m = (int(x) for x in by.split(":"))
    d = datetime.fromtimestamp(now, tz).replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)
    if d.timestamp() <= now + 900:
        d += timedelta(days=1)
    return int(d.timestamp())

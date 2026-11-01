"""Best times to run big appliances, from the battery outlook and the price forecast.

The battery outlook (outlook.py) projects, every 15 minutes to the end of tomorrow, how much solar
will be spare (exported), how full the battery will be and when the house will buy from the grid.
Running an appliance at a given time costs:

  spare solar   what exporting it would have earned (the feed-in price; when that is negative,
                using it instead saves money)
  battery       what the house will pay for it later: the average buy price at the times the
                outlook expects to import (or the feed-in price if it never needs to)
  grid          the buy price at that time

Every start time in the next 24 hours is priced for each appliance in APPLIANCES
("name:kW:hours", comma separated) and the cheapest is suggested. Without a retailer price feed it
picks the time that needs the least grid energy instead. Appliances are priced one at a time, so
two run together would share the same spare solar.
"""
import statistics

DEFAULT = "Dishwasher:1.2:2,Washing machine:0.5:1.5,Dryer:2.5:1.5"
STEP = 900
HORIZON = 24 * 3600


def parse(spec):
    out = []
    for part in (spec if spec is not None else DEFAULT).split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) != 3 or not bits[0]:
            continue
        try:
            kw, hrs = float(bits[1]), float(bits[2])
        except ValueError:
            continue
        if 0 < kw <= 50 and 0 < hrs <= 24:
            out.append({"name": bits[0], "kw": kw, "hours": hrs})
    return out


def appliance_advice(rows, limits, forecast, appliances, now):
    if not rows:
        return {"enabled": False, "reason": "waiting for the battery outlook"}
    if not appliances:
        return {"enabled": False, "reason": "no appliances set"}
    price = {}
    for p in forecast or []:
        if p.get("buy") is None:
            continue
        for t in range(int(p["t"]) // STEP * STEP, int(p["end"]), STEP):
            price[t] = (p["buy"], p.get("sell"))
    priced = bool(price)
    steps = []
    for r in rows:
        t = int(r["t"]) // STEP * STEP
        if t < now // STEP * STEP or t >= now + HORIZON:
            continue
        if priced and t not in price:
            break  # beyond the price forecast
        h = STEP / 3600
        buy, sell = price.get(t, (30.0, 5.0))  # without prices: grid dear, exports cheap, so least grid wins
        steps.append({"t": t, "spare_kw": (r.get("grid_out") or 0) / h, "import_kw": (r.get("grid_in") or 0) / h,
                      "soc": r.get("soc"), "buy": buy, "sell": sell if sell is not None else 0.0})
    if len(steps) < 4:
        return {"enabled": False, "reason": "not enough of a forecast yet"}
    imports = [s["buy"] for s in steps if s["import_kw"] > 0.05]
    sells = [s["sell"] for s in steps]
    battery_c = statistics.mean(imports) if imports else max(0.0, statistics.median(sells))
    reserve = (limits or {}).get("reserve", 5.0)

    def run_cost(i, kw, n):
        cost = grid_kwh = solar_kwh = batt_kwh = 0.0
        h = STEP / 3600
        for s in steps[i:i + n]:
            need = kw
            use = min(need, s["spare_kw"])
            cost += use * h * s["sell"] / 100; solar_kwh += use * h; need -= use
            if need > 0 and s["import_kw"] < 0.05 and (s["soc"] or 0) > reserve + 5:
                cost += need * h * battery_c / 100; batt_kwh += need * h; need = 0
            if need > 0:
                cost += need * h * s["buy"] / 100; grid_kwh += need * h
        return {"cost": round(cost, 2), "grid_kwh": round(grid_kwh, 2), "solar_kwh": round(solar_kwh, 2),
                "battery_kwh": round(batt_kwh, 2)}

    out = []
    for a in appliances:
        n = max(1, round(a["hours"] * 3600 / STEP))
        if n > len(steps):
            continue
        options = [(i, run_cost(i, a["kw"], n)) for i in range(len(steps) - n + 1)]
        key = (lambda o: (o[1]["cost"], o[1]["grid_kwh"])) if priced else (lambda o: (o[1]["grid_kwh"], o[1]["cost"]))
        best_i, best = min(options, key=key)
        worst_i, worst = max(options, key=key)
        now_c = options[0][1]
        total = a["kw"] * n * STEP / 3600
        why = ("mostly spare solar" if best["solar_kwh"] >= 0.6 * total else
               "from the battery" if best["battery_kwh"] >= 0.6 * total else
               "cheapest grid power" if priced else "least from the grid")
        out.append({**a, "kwh": round(total, 2),
                    "best": {"start": steps[best_i]["t"], "end": steps[best_i]["t"] + n * STEP, **best, "why": why},
                    "now": now_c, "worst": {"start": steps[worst_i]["t"], **worst},
                    "saving": round(now_c["cost"] - best["cost"], 2) if priced else None,
                    "run_now": best_i == 0 or (priced and now_c["cost"] - best["cost"] <= 0.05)})
    return {"enabled": True, "priced": priced, "battery_c": round(battery_c, 1),
            "until": steps[-1]["t"] + STEP, "appliances": out}

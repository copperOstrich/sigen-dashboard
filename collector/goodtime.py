"""Is now a good time to use power? One level for the header, from what an extra kWh costs you now.

What an extra kWh costs depends on where it would come from (the same rules as Best times):

  spare solar   the feed-in it would have earned exported (nothing, or a saving, when feed-in is
                negative)
  battery       what the house will pay later for the energy it takes out: the average buy price
                at the times the battery outlook expects to buy (or feed-in, if it never needs to)
  grid          the buy price now

That cost is placed on a scale of five levels:

  free     3c/kWh or less: spare solar, or a negative price
  green    cheap
  normal
  dear
  spike    $1/kWh or more (or your retailer says there's a spike)

"Cheap" and "dear" come from your own prices (GOOD_TIME_SCALE=adaptive, the default: the cheapest
quarter and the dearest tenth of the buy prices in the last fortnight), or from fixed cents
(GOOD_TIME_SCALE=fixed with GOOD_TIME_FIXED="15,35": green under 15c, dear from 35c). With
GOOD_TIME_GREEN=renewables, green also needs the grid at GOOD_TIME_RENEWABLES% renewable or more
(your retailer's figure, or AEMO's for your region); otherwise it shows as normal.
"""
import statistics

LEVELS = ["free", "green", "normal", "dear", "spike"]
LABELS = {"free": "Free power", "green": "Good time", "normal": "Normal", "dear": "Dear", "spike": "Price spike"}
FREE_C, SPIKE_C = 3.0, 100.0
STEP = 900


def parse_fixed(value):
    """'15,35' -> (15.0, 35.0). Raises ValueError."""
    try:
        a, b = (float(x) for x in str(value).split(","))
    except ValueError:
        raise ValueError("Two prices in c/kWh, like 15,35 (green under the first, dear from the second)") from None
    if not (FREE_C < a < b < SPIKE_C):
        raise ValueError(f"Two prices between {FREE_C:g} and {SPIKE_C:g}c, the first lower, like 15,35")
    return a, b


def thresholds(scale, fixed, history):
    """(green below, dear from) in c/kWh."""
    if scale == "fixed":
        return fixed
    buys = sorted(h["buy"] for h in history or [] if h.get("buy") is not None)
    if len(buys) < 48:  # under a day of prices: start from typical figures
        return fixed
    q = lambda p: buys[min(len(buys) - 1, int(p * len(buys)))]
    green, dear = max(FREE_C + 1, q(0.25)), min(SPIKE_C - 1, q(0.90))
    if dear - green < 5:  # flat prices (a flat plan): everything is normal
        green, dear = green - 0.01, green + 5
    return round(green, 1), round(dear, 1)


def level_for(cost_c, green, dear, spike=False, renewables=None, need_renewables=None):
    if spike or cost_c >= SPIKE_C:
        return "spike"
    if cost_c <= FREE_C:
        return "free"
    if cost_c < green:
        if need_renewables is not None and (renewables is None or renewables < need_renewables):
            return "normal"
        return "green"
    return "dear" if cost_c >= dear else "normal"


def good_time(live, outlook_rows, forecast, history, scale="adaptive", fixed=(15.0, 35.0), green_rule="cost",
              renewables_min=50.0, now=None):
    p = (live or {}).get("price") or {}
    power = (live or {}).get("power") or {}
    aemo = (live or {}).get("aemo") or {}
    if p.get("buy") is None:
        return {"enabled": False, "reason": "needs retailer prices"}
    now = now or (live or {}).get("updated") or 0
    green, dear = thresholds(scale, fixed, history)
    ren = p.get("renewables") if p.get("renewables") is not None else aemo.get("renewables_pct")
    need_ren = renewables_min if green_rule == "renewables" else None

    # the battery's energy is worth what the house will pay for it later
    future = [r for r in outlook_rows or [] if r.get("t", 0) >= now]
    price_at = {}
    for f in forecast or []:
        if f.get("buy") is not None:
            for t in range(int(f["t"]) // STEP * STEP, int(f["end"]), STEP):
                price_at[t] = (f["buy"], f.get("sell"), f.get("spike") in ("spike", "potential"))
    imports = [price_at[int(r["t"]) // STEP * STEP][0] for r in future
               if (r.get("grid_in") or 0) > 0.01 and int(r["t"]) // STEP * STEP in price_at]
    sells = [v[1] for v in price_at.values() if v[1] is not None]
    battery_c = statistics.mean(imports) if imports else (max(0.0, statistics.median(sells)) if sells else p["buy"])

    def cost(spare_kw, importing, soc, buy, sell, reserve=10):
        if spare_kw >= 0.5:
            return (sell if sell is not None else 0.0), "spare solar"
        if not importing and soc is not None and soc > reserve:
            return battery_c, "the battery"
        return buy, "the grid"

    grid_w = power.get("grid") or 0
    soc = ((live or {}).get("battery") or {}).get("soc")
    reserve = (((live or {}).get("battery") or {}).get("cutoff_low") or 5) + 5
    c_now, src = cost(max(0.0, -grid_w) / 1000, grid_w > 100, soc, p["buy"], p.get("sell"), reserve)
    lvl = level_for(c_now, green, dear, p.get("spike") == "spike", ren, need_ren)

    # the next stretch (half an hour or more) at free or green, and the next spike, from the forecast
    steps = []
    for r in future:
        t = int(r["t"]) // STEP * STEP
        if t not in price_at:
            continue
        buy, sell, spk = price_at[t]
        c, _ = cost((r.get("grid_out") or 0) / (STEP / 3600), (r.get("grid_in") or 0) > 0.01, r.get("soc"), buy, sell, reserve)
        steps.append((t, level_for(c, green, dear, spk and buy >= SPIKE_C, None, None), c))
    good = ("free", "green")
    # while it's good now: until when; the next good stretch is the one after that
    until, i = None, 0
    if lvl in good:
        while i < len(steps) and (steps[i][0] <= now or steps[i][1] in good):
            i += 1
        until = steps[i][0] if i < len(steps) else None
    nxt = None
    while i < len(steps):
        if steps[i][1] in ("free", "green") and steps[i][0] > now - STEP:
            j = i
            while j + 1 < len(steps) and steps[j + 1][1] in ("free", "green") and steps[j + 1][0] == steps[j][0] + STEP:
                j += 1
            if j - i + 1 >= 2:
                nxt = {"start": steps[i][0], "end": steps[j][0] + STEP,
                       "level": "free" if all(s[1] == "free" for s in steps[i:j + 1]) else "green",
                       "c_kwh": round(min(s[2] for s in steps[i:j + 1]), 1)}
                break
            i = j + 1
        else:
            i += 1
    spike = next(({"start": t, "c_kwh": round(c, 1)} for t, lv, c in steps if lv == "spike" and t > now), None)
    why = {"spare solar": "your spare solar would otherwise be exported",
           "the battery": "the battery is covering the house",
           "the grid": "the house is buying from the grid"}[src]
    return {"enabled": True, "level": lvl, "label": LABELS[lvl], "c_kwh": round(c_now, 1), "source": src, "why": why,
            "renewables": round(ren) if ren is not None else None, "green_below": green, "dear_from": dear,
            "scale": scale, "green_rule": green_rule, "renewables_min": renewables_min if need_ren else None,
            "battery_c": round(battery_c, 1), "good_until": until, "next_good": nxt, "next_spike": spike, "levels": LEVELS}

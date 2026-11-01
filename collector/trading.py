"""Battery trading: how well the battery is traded with the grid (Amber SmartShift or a VPP), from
the collector's minute readings.

  spikes    runs of minutes when the feed-in price was $1/kWh or more. For each: what was exported
            (and how much of it came from the battery), what it earned, and what the battery could
            have exported over the spike at the fastest export seen, from the charge it had above
            its reserve. Caught = at least half of that; otherwise missed, and why: the battery was
            already near its reserve, or it had charge but didn't export it.
  prices    what battery exports averaged per kWh, against each day's best hour of feed-in prices
  reserve   a higher backup reserve, roughly: the extra grid energy bought while the battery would
            have stopped at the higher level (at the buy price then), against what that held-back
            energy could have earned in spikes the battery missed for lack of charge, and the extra
            hours of outage cover at the house's always-on use

Estimates, from minute averages; the retailer's own control isn't visible, only its effects.
"""
import time
from datetime import datetime

SPIKE_C = 100.0        # feed-in at or above this (c/kWh, paid to you) counts as a spike
GAP_S = 600            # spike minutes this close together are one event
TRADE_MIN_W = 300      # battery-to-grid power below this is noise


def trading(store, live, tz, days=30, baseload_w=None, now=None):
    now = now or time.time()
    b = (live or {}).get("battery") or {}
    cap, reserve = b.get("rated_kwh"), b.get("cutoff_low")
    with store.lock:
        rows = store.db.execute(
            "SELECT ts, battery, grid, soc, sell_c, buy_c, load FROM samples WHERE ts>=? AND sell_c IS NOT NULL ORDER BY ts",
            (int(now - days * 86400),)).fetchall()
    if len(rows) < 600 or not cap:
        return {"enabled": False, "reason": "needs a few days of readings with retailer prices"}
    reserve = reserve if reserve is not None else 5.0
    exports = sorted(-g for _ts, _b, g, *_r in rows if g is not None and g < -500)
    # the fastest export seen (a high percentile, so one odd reading doesn't set it; with few exports,
    # the fastest of them; with none, half the battery's capacity an hour)
    max_w = (exports[int(len(exports) * 0.99)] if len(exports) > 50 else exports[-1] if exports
             else (b.get("max_discharge_w") or cap * 500))

    # ---- spikes
    events, cur = [], None
    for ts, batt, grid, soc, sell, buy, load in rows:
        price = -sell  # paid to you, c/kWh
        if price >= SPIKE_C:
            if cur is None or ts - cur["end"] > GAP_S:
                cur = {"start": ts, "end": ts, "minutes": 0, "kwh": 0.0, "batt_kwh": 0.0, "value": 0.0, "peak_c": 0.0,
                       "soc_start": soc, "soc_min": soc, "price_sum": 0.0}
                events.append(cur)
            exp = max(0.0, -(grid or 0)) / 60000
            to_grid = max(0.0, min(-(batt or 0), -(grid or 0))) / 60000 if (batt or 0) < -TRADE_MIN_W else 0.0
            cur.update(end=ts, minutes=cur["minutes"] + 1, kwh=cur["kwh"] + exp, batt_kwh=cur["batt_kwh"] + to_grid,
                       value=cur["value"] + exp * price / 100, peak_c=max(cur["peak_c"], price),
                       soc_min=min(x for x in (cur["soc_min"], soc) if x is not None) if soc is not None else cur["soc_min"],
                       price_sum=cur["price_sum"] + price)
    spikes = []
    for e in events:
        avail = max(0.0, cap * ((e["soc_start"] or 0) - reserve) / 100)
        could = min(max_w * e["minutes"] / 60000, avail + e["kwh"] - e["batt_kwh"])  # battery room + whatever solar exported
        avg_c = e["price_sum"] / e["minutes"]
        caught = could > 0.05 and e["kwh"] >= 0.5 * could
        why = None
        if not caught:
            why = "battery near its reserve" if (e["soc_start"] or 0) <= reserve + 5 else "battery had charge but didn't export"
        spikes.append({"start": e["start"], "end": e["end"] + 60, "minutes": e["minutes"], "peak_c": round(e["peak_c"], 1),
                       "avg_c": round(avg_c, 1), "kwh": round(e["kwh"], 2), "battery_kwh": round(e["batt_kwh"], 2),
                       "earned": round(e["value"], 2), "could_kwh": round(could, 2),
                       "could_earn": round(could * avg_c / 100, 2), "soc_start": e["soc_start"], "caught": caught, "why": why})

    # ---- export prices: battery exports against each day's best hour
    by_day = {}
    for ts, batt, grid, soc, sell, buy, load in rows:
        d = by_day.setdefault(datetime.fromtimestamp(ts, tz).date().isoformat(), {"prices": [], "kwh": 0.0, "value": 0.0})
        d["prices"].append(-sell)
        if (batt or 0) < -TRADE_MIN_W and (grid or 0) < -TRADE_MIN_W:
            kwh = min(-batt, -grid) / 60000
            d["kwh"] += kwh; d["value"] += kwh * -sell / 100
    trade_kwh = sum(d["kwh"] for d in by_day.values())
    trade_value = sum(d["value"] for d in by_day.values())
    best = []
    for d in by_day.values():
        p = d["prices"]
        if len(p) >= 60:
            best.append(max(sum(p[i:i + 60]) / 60 for i in range(0, len(p) - 59, 5)))
    prices = {"battery_kwh": round(trade_kwh, 1), "battery_value": round(trade_value, 2),
              "battery_avg_c": round(trade_value / trade_kwh * 100, 1) if trade_kwh > 0.5 else None,
              "best_hour_avg_c": round(sum(best) / len(best), 1) if best else None, "days": len(by_day)}

    # ---- a higher reserve
    what_if = []
    for extra in (10, 20, 30):
        new = min(100.0, reserve + extra)
        held = cap * (new - reserve) / 100
        cost = grid_kwh = 0.0
        for ts, batt, grid, soc, sell, buy, load in rows:
            # discharging below the new reserve: the house would have bought this instead
            if soc is not None and soc < new and (batt or 0) < -50 and buy is not None:
                kwh = -batt / 60000
                grid_kwh += kwh; cost += kwh * buy / 100
        gain = 0.0
        for s in spikes:
            if not s["caught"] and s["why"] == "battery near its reserve":
                gain += min(held, max_w * s["minutes"] / 60000) * s["avg_c"] / 100
        what_if.append({"reserve": round(new), "held_kwh": round(held, 1), "extra_grid_kwh": round(grid_kwh, 1),
                        "extra_cost": round(cost, 2), "spike_gain": round(gain, 2), "net": round(gain - cost, 2),
                        "outage_hours": round(held * 1000 / baseload_w, 1) if baseload_w else None})
    return {"enabled": True, "days": prices["days"], "reserve": reserve, "capacity_kwh": cap, "max_export_w": round(max_w),
            "spike_c": SPIKE_C, "spikes": spikes[-20:], "caught": sum(1 for s in spikes if s["caught"]),
            "spike_count": len(spikes), "spike_earned": round(sum(s["earned"] for s in spikes), 2),
            "spike_missed_value": round(sum(max(0.0, s["could_earn"] - s["earned"]) for s in spikes), 2),
            "prices": prices, "what_if": what_if}

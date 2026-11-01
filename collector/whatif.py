"""What if: your own recorded year, priced on other plans, with the battery run the way each plan
rewards, and with more battery or more panels.

The recorded hours (solar made, and what the home and car used) are played back through a simple
battery model, hour by hour:

  flat plans       the battery soaks up spare solar and covers the house when the sun isn't enough
  time of use      the same, plus it tops up from the grid in the cheap hours (enough for the dear
                   hours that follow) and holds its charge through cheap hours for the dear ones
  Amber            the same as flat, plus charging from the grid when the price is about zero or
                   negative, and selling down to 20% when the feed-in pays $1/kWh or more

The battery knows the next 24 hours of use and solar (as a forecast would, roughly), so this is a
little better than a real battery would manage. Exports are capped at the export limit; more panels
are capped at the inverter's ceiling. The simulated Amber cost with your system as it is is
compared with what the recorded hours cost at Amber's recorded prices, as a check on the model. The
Amber row itself starts from that recorded cost (the battery as your system actually ran it), and
each change is the simulated difference, so the idealised trading above doesn't flatter it.
"""
import math
import time
from datetime import datetime

from money import dated_on, rate, rates_on

SPIKE_C = 100      # feed-in paying this much (c/kWh) or more: sell
CHEAP_C = 1        # buy price at or under this: charge from the grid
KEEP = 0.2         # share of the battery kept when selling into a spike


def load_hours(store, tz, days=365):
    """[(ts, local datetime, pv kWh, load kWh, buy c, sell c, import kWh, export kWh)] for complete days."""
    since = int(time.time()) - days * 86400
    with store.lock:
        rows = store.db.execute(
            "SELECT (ts/3600)*3600 AS h, SUM(MAX(pv,0))/60000.0, SUM(MAX(load,0))/60000.0, AVG(buy_c), AVG(sell_c), "
            "SUM(MAX(grid,0))/60000.0, SUM(MAX(-grid,0))/60000.0, COUNT(*) FROM samples WHERE ts>=? AND pv IS NOT NULL "
            "AND load IS NOT NULL GROUP BY h ORDER BY h", (since,)).fetchall()
    byday = {}
    for h, pv, load, buy, sell, imp, exp, n in rows:
        if n < 30:
            continue
        k = 60.0 / n  # a few missing minutes: scale up
        lt = datetime.fromtimestamp(h, tz)
        byday.setdefault(lt.date().isoformat(), []).append((h, lt, pv * k, load * k, buy, sell, imp * k, exp * k))
    today = datetime.now(tz).date().isoformat()
    out = []
    for day in sorted(byday):
        if day != today and len(byday[day]) >= 22:
            out += byday[day]
    return out


def simulate(hours, cap, p_ch, p_dis, eta, export_cap, pv_scale, pv_cap, strategy, rates=None):
    """[(import kWh, export kWh)] per hour. cap: usable kWh; p_*: kW; rates: c/kWh per hour (time of use)."""
    n = len(hours)
    e = cap * 0.5
    one = math.sqrt(eta)  # each way
    pv = [min(h[2] * pv_scale, pv_cap) if pv_cap else h[2] * pv_scale for h in hours]
    load = [h[3] for h in hours]
    need = [0.0] * n
    cheap = [False] * n
    if strategy == "tou" and cap > 0:
        for i in range(n):
            ri = rates[i]
            window = range(i + 1, min(n, i + 25))
            if not any(rates[j] * one * one > ri + 1 for j in window):
                continue
            cheap[i] = True
            dear, sun, started = 0.0, 0.0, False
            for j in window:  # the dear stretch after this cheap one, up to the next cheap hour
                if rates[j] * one * one > ri + 1:
                    started = True
                    dear += max(0.0, load[j] - pv[j])
                elif started:
                    break
                # spare solar before and during it will refill the battery anyway
                sun += min(p_ch, max(0.0, pv[j] - load[j])) * one
            need[i] = min(cap, max(0.0, dear / one - sun))
    out = []
    for i in range(n):
        net = pv[i] - load[i]
        imp = exp = 0.0
        if net >= 0:
            c = min(net, p_ch, (cap - e) / one) if cap > 0 else 0.0
            e += c * one
            exp = net - c
        else:
            d = -net
            hold = cheap[i] and e <= need[i]
            dis = 0.0 if hold else min(d, p_dis, max(0.0, (e - (need[i] if cheap[i] else 0.0))) * one)
            e -= dis / one
            imp = d - dis
        room = max(0.0, p_ch - max(0.0, net))
        if strategy == "tou" and cheap[i] and e < need[i]:
            g = min(room, (need[i] - e) / one)
            e += g * one
            imp += g
        elif strategy == "amber" and cap > 0:
            buy, sell = hours[i][4], hours[i][5]
            if buy is not None and buy <= CHEAP_C and e < cap:
                g = min(room, (cap - e) / one)
                e += g * one
                imp += g
            elif sell is not None and -sell >= SPIKE_C and e > cap * KEEP:
                s = min(p_dis, (e - cap * KEEP) * one)
                e -= s / one
                exp += s
        if export_cap is not None:
            exp = min(exp, export_cap)
        out.append((imp, exp))
    return out


def price_plan(hours, flows, plan, tz):
    days = sorted({h[1].date().isoformat() for h in hours})
    usage = feed = 0.0
    for h, (imp, exp) in zip(hours, flows):
        p = rates_on(plan, h[1].date().isoformat())
        usage += imp * rate(p.get("usage"), p.get("usage_c_kwh"), h[1])
        feed += exp * rate(p.get("feed_in"), p.get("feed_in_c_kwh"), h[1])
    supply = sum(float(rates_on(plan, d).get("supply_c_day") or 0) for d in days)
    return (usage - feed + supply) / 100


def price_amber(hours, flows, supply_rates, fee_monthly):
    days = sorted({h[1].date().isoformat() for h in hours})
    # an hour without a recorded price: that hour of day's average
    avg = {}
    for h in hours:
        if h[4] is not None:
            avg.setdefault(h[1].hour, []).append((h[4], h[5] if h[5] is not None else 0))
    avg = {k: (sum(a for a, _ in v) / len(v), sum(b for _, b in v) / len(v)) for k, v in avg.items()}
    total = 0.0
    for h, (imp, exp) in zip(hours, flows):
        buy, sell = h[4], h[5]
        if buy is None:
            buy, sell = avg.get(h[1].hour, (30.0, -5.0))
        total += imp * buy + exp * (sell if sell is not None else 0)
    fixed = sum((dated_on(supply_rates, d) or 0) for d in days) + (fee_monthly or 0) * 100 * 12 / 365 * len(days)
    return (total + fixed) / 100


def what_if(store, tz, live, plans, retailer_name=None, supply_rates=None, fee_monthly=None, kwp=None,
            export_w=None, pv_w=None, round_trip=None, add_kwh=None, add_kw=None, days=365):
    hours = load_hours(store, tz, days)
    if len(hours) < 14 * 24:
        return {"enabled": False, "reason": f"needs two weeks of readings ({len(hours) // 24} days so far)"}
    b = (live or {}).get("battery") or {}
    rated = b.get("rated_kwh") or 0
    reserve = (b.get("cutoff_low") or 5) / 100
    usable = rated * (1 - reserve)
    p_ch = (b.get("max_charge_w") or 10000) / 1000
    p_dis = (b.get("max_discharge_w") or 10000) / 1000
    eta = (round_trip / 100) if round_trip and 75 <= round_trip <= 98 else 0.9
    add_kwh = 8.0 if add_kwh is None else max(0.0, min(200.0, add_kwh))
    add_kw = (round((kwp or 6) * 0.25 * 2) / 2 if add_kw is None else max(0.0, min(100.0, add_kw)))
    exp_cap = export_w / 1000 if export_w else None
    pv_cap = pv_w / 1000 if pv_w else None
    scale = (kwp + add_kw) / kwp if kwp else 1.25
    scen = [("now", "Your system", usable, 1.0),
            ("no_battery", "Without the battery", 0.0, 1.0),
            ("more_battery", f"+{add_kwh:g} kWh battery", usable + add_kwh * (1 - reserve), 1.0),
            ("more_panels", f"+{add_kw:g} kW panels" if kwp else "+25% panels", usable, scale)]
    span = (hours[-1][0] - hours[0][0]) / 86400 + 1
    n_days = len({h[1].date() for h in hours})
    year = 365 / n_days
    rows = []
    ar_name = f"{retailer_name or 'Amber'} (wholesale prices)"
    have_prices = sum(1 for h in hours if h[4] is not None) > len(hours) * 0.5
    check = None
    if have_prices:
        # anchored on what the recorded hours cost at the recorded prices; each change is the
        # simulated difference from the simulated system as it is
        actual = price_amber(hours, [(h[6], h[7]) for h in hours], supply_rates, fee_monthly)
        sims = {}
        for key, _, cap, sc in scen:
            fl = simulate(hours, cap, p_ch, p_dis, eta, exp_cap, sc, pv_cap if sc != 1 else None, "amber")
            sims[key] = price_amber(hours, fl, supply_rates, fee_monthly)
        r = {"name": ar_name, "kind": "amber",
             "costs": {k: round((actual + sims[k] - sims["now"]) * year) for k in sims}}
        rows.append(r)
        check = {"actual": round(actual * year), "simulated": round(sims["now"] * year)}
    for plan in (plans or {}).get("plans", []):
        tou = any(rp.get("usage") for rp in (plan.get("rates") or [plan]))
        rates = [rate(rates_on(plan, h[1].date().isoformat()).get("usage"),
                      rates_on(plan, h[1].date().isoformat()).get("usage_c_kwh"), h[1]) for h in hours] if tou else None
        r = {"name": plan["name"], "kind": "tou" if tou else "flat", "costs": {}}
        for key, _, cap, sc in scen:
            fl = simulate(hours, cap, p_ch, p_dis, eta, exp_cap, sc, pv_cap if sc != 1 else None,
                          "tou" if tou else "solar", rates)
            r["costs"][key] = round(price_plan(hours, fl, plan, tz) * year)
        rows.append(r)
    best = {k: min(rows, key=lambda r: r["costs"][k])["name"] for k, *_ in scen} if rows else {}
    first, last = hours[0][1].date().isoformat(), hours[-1][1].date().isoformat()
    return {"enabled": True, "from": first, "to": last, "days": n_days, "span_days": round(span),
            "full_year": n_days >= 330, "scenarios": [{"key": k, "label": l} for k, l, *_ in scen],
            "plans": rows, "best": best, "check": check,
            "assumptions": {"usable_kwh": round(usable, 1), "charge_kw": p_ch, "discharge_kw": p_dis,
                            "round_trip": round(eta * 100), "export_limit_kw": exp_cap, "inverter_kw": pv_cap,
                            "kwp": kwp, "add_kwh": add_kwh, "add_kw": add_kw}}

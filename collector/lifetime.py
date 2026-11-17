"""Lifetime totals: everything the system has done since it was switched on.

The inverter's own lifetime counters (total solar, home use, bought, sold, battery in and out, car)
give the true totals, including the time before the dashboard was installed. The dashboard's own
days (plus any imported mySigen history) break that down by year; whatever the counters hold that
the recorded days don't is shown as "before records".

CO2 avoided uses the location-based grid emission factor for your state from the National Greenhouse
Accounts (kg CO2-e per kWh of grid electricity), or GRID_CO2_KG_KWH if set: each kWh of solar,
used at home or sold, is a kWh the grid didn't have to make.
"""
from datetime import date, datetime, timedelta

# National Greenhouse Accounts / NGER location-based scope 2 factors, kg CO2-e per kWh. 2025-26 for
# NSW, VIC and QLD; 2024-25 for SA (the latest published). TAS: set GRID_CO2_KG_KWH.
GRID_FACTORS = {"NSW1": 0.64, "VIC1": 0.78, "QLD1": 0.67, "SA1": 0.23}
KEYS = ("pv", "load", "import", "export", "charge", "discharge", "ev")


def _milestone(total):
    """The next round number of kWh worth celebrating."""
    step = 1000 if total < 10000 else 5000 if total < 50000 else 10000
    return (int(total // step) + 1) * step


def lifetime(store, counters, ev_total=None, installed="", region="NSW1", co2_factor=None, today=None):
    """counters: the inverter's latest lifetime totals (store.last_sample()); ev_total: the charger's."""
    today = today or date.today()
    # store.daily counts back from the real clock
    days = store.daily(max(1, (max(today, date.today()) - _first_day(store, today)).days + 2))
    rec = {k: 0.0 for k in KEYS}
    rec.update(bought=0.0, earned=0.0, priced_days=0)
    years, best_day = {}, None
    for d in days:
        y = years.setdefault(d["day"][:4], {"year": d["day"][:4], "days": 0, **{k: 0.0 for k in KEYS},
                                            "bought": 0.0, "earned": 0.0, "priced_days": 0})
        y["days"] += 1
        for k in KEYS:
            v = d.get(k) or 0.0
            y[k] += v
            rec[k] += v
        if d.get("bought") is not None:
            for t in (y, rec):
                t["bought"] += d["bought"]
                t["earned"] += d["earned"]
                t["priced_days"] += 1
        if d.get("pv") and (best_day is None or d["pv"] > best_day["pv"]):
            best_day = {"day": d["day"], "pv": round(d["pv"], 1)}

    c = counters or {}
    life = {"pv": c.get("total_pv_kwh"), "import": c.get("total_import_kwh"), "export": c.get("total_export_kwh"),
            "charge": c.get("total_charge_kwh"), "discharge": c.get("total_discharge_kwh"), "ev": ev_total}
    load_all = c.get("total_load_kwh")  # the inverter's home total includes the car
    car = ev_total if ev_total is not None else rec["ev"]  # no charger counter: the car as recorded
    life["load"] = max(0.0, load_all - car) if load_all is not None else None
    for k in KEYS:  # no counter (or the car counter missing): fall back to what's been recorded
        if life[k] is None or life[k] < rec[k] - 1:
            life[k] = rec[k]
    before = {k: round(max(0.0, life[k] - rec[k]), 1) for k in KEYS}
    has_before = before["pv"] >= 1

    # lifetime solar at the end of each recorded day: the total now, less what came after
    cum, after = [], 0.0
    for d in reversed(days):
        cum.append({"day": d["day"], "pv": round(life["pv"] - after, 1)})
        after += d.get("pv") or 0.0
    cum.reverse()
    if len(cum) > 500:  # keep the chart light: every nth day, always the last
        n = -(-len(cum) // 500)
        cum = cum[::n] + ([cum[-1]] if (len(cum) - 1) % n else [])

    # rate: solar a day over the last year of records (or what there is)
    recent = [d.get("pv") or 0.0 for d in days if d["day"] > (today - timedelta(days=365)).isoformat()]
    per_day = sum(recent) / len(recent) if len(recent) >= 7 else None
    nxt = _milestone(life["pv"])
    eta = (today + timedelta(days=round((nxt - life["pv"]) / per_day))).isoformat() if per_day else None

    used = life["load"] + life["ev"]
    factor = co2_factor if co2_factor is not None else GRID_FACTORS.get(region)
    first = days[0]["day"] if days else None
    out_years = []
    for key in sorted(years):
        y = years[key]
        priced = y["priced_days"] > 0
        y.update({k: round(y[k], 1) for k in KEYS})
        y["bought"] = round(y["bought"], 2) if priced else None
        y["earned"] = round(y["earned"], 2) if priced else None
        y["net"] = round(y["bought"] - y["earned"], 2) if priced else None
        y["self_sufficiency"] = (round(max(0, min(100, (1 - y["import"] / (y["load"] + y["ev"])) * 100)), 1)
                                 if y["load"] + y["ev"] > 1 else None)
        out_years.append(y)
    months = {}
    for d in days:
        months[d["day"][:7]] = months.get(d["day"][:7], 0.0) + (d.get("pv") or 0.0)
    best_month = max(months.items(), key=lambda kv: kv[1]) if months else None
    priced = rec["priced_days"] > 0
    return {
        "since": installed or None,                           # the day it was switched on, if set
        "first_recorded": first, "recorded_days": len(days),
        "total": {k: round(life[k], 1) for k in KEYS},
        "recorded": {**{k: round(rec[k], 1) for k in KEYS},
                     "bought": round(rec["bought"], 2) if priced else None,
                     "earned": round(rec["earned"], 2) if priced else None,
                     "net": round(rec["bought"] - rec["earned"], 2) if priced else None,
                     "priced_days": rec["priced_days"]},
        "before_records": before if has_before else None,
        "years": out_years,
        "cumulative": cum,
        "self_sufficiency": round(max(0, min(100, (1 - life["import"] / used) * 100)), 1) if used > 1 else None,
        "self_consumption": round(max(0, min(100, (1 - life["export"] / life["pv"]) * 100)), 1) if life["pv"] > 1 else None,
        "round_trip": round(life["discharge"] / life["charge"] * 100, 1) if life["charge"] > 10 else None,
        "best_day": best_day,
        "best_month": {"month": best_month[0], "pv": round(best_month[1], 1)} if best_month else None,
        "per_day": round(per_day, 1) if per_day else None,
        "milestone": {"kwh": nxt, "eta": eta},
        "co2": {"factor": factor, "tonnes": round(life["pv"] * factor / 1000, 1),
                "source": "your setting" if co2_factor is not None else "National Greenhouse Accounts"} if factor else None,
    }


def _first_day(store, today):
    with store.lock:
        r = store.db.execute("SELECT MIN(day) FROM samples").fetchone()
        own = r[0] if r else None
    imp = store.imported_days("")
    first = min([d for d in [own, *imp.keys()] if d] or [today.isoformat()])
    return datetime.fromisoformat(first).date()

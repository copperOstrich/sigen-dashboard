"""Targets: a self-sufficiency percentage and a monthly net energy cost to aim for, with how this
month is tracking and which past months made it.

Each target is a number for every month ("90"), or different numbers for groups of months, as
first-last:value pairs ("12-2:90,3-11:70" = 90% from December to February, 70% otherwise). The
cost target is History > Months' Net cost (energy bought less energy sold, before supply
charges): -50 means earning $50 more than you spend.

This month's finish is projected from the month so far plus the remaining days at the last
fortnight's average.
"""
import calendar


def parse(value, lo, hi):
    """'90' -> {1..12: 90.0}; '12-2:90,3-11:70' -> per month. Raises ValueError."""
    v = (value or "").strip()
    if not v:
        return {}
    if ":" not in v:
        x = float(v.replace("$", ""))
        if not lo <= x <= hi:
            raise ValueError(f"from {lo:g} to {hi:g}")
        return {m: x for m in range(1, 13)}
    out = {}
    for part in v.split(","):
        months, x = part.split(":")
        x = float(x.strip().replace("$", ""))
        if not lo <= x <= hi:
            raise ValueError(f"from {lo:g} to {hi:g}")
        a, _, b = months.strip().partition("-")
        a, b = int(a), int(b or a)
        if not (1 <= a <= 12 and 1 <= b <= 12):
            raise ValueError("months are 1 to 12")
        m = a
        while True:
            out[m] = x
            if m == b:
                break
            m = m % 12 + 1
    return out


def check(value, kind):
    """For the Settings page: raises ValueError with a message."""
    try:
        parse(value, *((0, 100) if kind == "self" else (-100000, 100000)))
    except ValueError as e:
        msg = str(e) if str(e).startswith(("from", "months")) else ""
        raise ValueError(("A number" + (" (percent)" if kind == "self" else " (dollars)")
                          + ", or month ranges like 12-2:90,3-11:70") + (f": {msg}" if msg else "")) from None


def progress(store, today, self_target, cost_target):
    """{"self": {...}, "cost": {...}, "months": [...]} for the dashboard, or {"enabled": False}."""
    if not self_target and not cost_target:
        return {"enabled": False}
    months = store.monthly(13)
    cur = today.strftime("%Y-%m")
    recent = [d for d in store.daily(15) if d.get("day") and d["day"] < today.isoformat()][-14:]
    days_in = calendar.monthrange(today.year, today.month)[1]
    left = days_in - today.day + 1  # today isn't finished, so it counts as left (its readings so far are in the month)
    m = next((x for x in months if x["month"] == cur), None)
    out = {"enabled": True, "month": cur, "days_left": left}

    used_day = sum((d.get("load") or 0) + (d.get("ev") or 0) for d in recent) / len(recent) if recent else None
    imp_day = sum(d.get("import") or 0 for d in recent) / len(recent) if recent else None
    costs = [d["cost"] for d in recent if d.get("cost") is not None]
    cost_day = sum(costs) / len(costs) if costs else None

    if self_target:
        t = self_target.get(today.month)
        so_far = m.get("self_sufficiency") if m else None
        proj = None
        if m and used_day is not None:
            # today's partial readings are in the month already: count today as half done
            used = m["load"] + m["ev"] + used_day * (left - 0.5)
            imp = m["import"] + imp_day * (left - 0.5)
            proj = round(max(0, min(100, (1 - imp / used) * 100)), 1) if used > 0.5 else None
        out["self"] = {"target": t, "so_far": so_far, "projected": proj,
                       "on_track": None if t is None or proj is None else proj >= t}
    if cost_target:
        t = cost_target.get(today.month)
        so_far = m.get("net") if m else None
        proj = round(so_far + cost_day * (left - 0.5), 2) if so_far is not None and cost_day is not None else None
        out["cost"] = {"target": t, "so_far": so_far, "projected": proj,
                       "on_track": None if t is None or proj is None else proj <= t}
    hist = []
    for x in months:
        if x["month"] == cur:
            continue
        mo = int(x["month"][5:])
        full = x["days"] >= calendar.monthrange(int(x["month"][:4]), mo)[1] - 1
        e = {"month": x["month"], "full": full}
        if self_target and self_target.get(mo) is not None and x.get("self_sufficiency") is not None:
            e["self"] = {"target": self_target[mo], "value": x["self_sufficiency"], "met": x["self_sufficiency"] >= self_target[mo]}
        if cost_target and cost_target.get(mo) is not None and x.get("net") is not None:
            e["cost"] = {"target": cost_target[mo], "value": x["net"], "met": x["net"] <= cost_target[mo]}
        if "self" in e or "cost" in e:
            hist.append(e)
    out["months"] = hist[-12:]
    return out

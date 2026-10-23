"""Battery outlook: where the battery's charge is heading over the next day and a half.

A simple, explainable model, not a copy of the inverter's own control:
  * home use: your typical use for each hour of the day over the last two weeks (weekdays and
    weekends separately once there's enough of each), without car charging
  * solar: the weather forecast's hourly production if it's on, otherwise a typical recent day
  * the battery covers the home and stores surplus solar (self-consumption), between your
    reserve and charge limit, at its maximum charge and discharge rates, with round-trip losses

It doesn't know about retailer or VPP control (Amber SmartShift exporting at a price spike,
grid charging), so the advice says so where that matters.
"""
import statistics
import time
from datetime import datetime, timedelta

STEP = 900                 # 15-minute steps
EFF_IN, EFF_OUT = 0.96, 0.96
HISTORY_DAYS = 14


class Outlook:
    def __init__(self, store, tz, log=print):
        self.store, self.tz, self.log = store, tz, log
        self.result = None
        self.computed = 0.0

    # ---------------------------------------------------------------- profiles
    def _profiles(self, now):
        """Typical home use and solar for each local hour: {kind: {hour: W}} for weekday/weekend."""
        rows = self.store.hourly_avgs(now - HISTORY_DAYS * 86400, now)
        buckets = {}
        for h, (pv, home) in rows.items():
            lt = datetime.fromtimestamp(h, self.tz)
            kind = "weekend" if lt.weekday() >= 5 else "weekday"
            b = buckets.setdefault(lt.hour, {"weekday": [], "weekend": [], "pv": []})
            if home is not None:
                b[kind].append(home)
            if pv is not None:
                b["pv"].append(pv)
        days_by_kind = {"weekday": set(), "weekend": set()}
        for h in rows:
            lt = datetime.fromtimestamp(h, self.tz)
            days_by_kind["weekend" if lt.weekday() >= 5 else "weekday"].add(lt.date())
        split = len(days_by_kind["weekend"]) >= 2 and len(days_by_kind["weekday"]) >= 3
        home, pv = {"weekday": {}, "weekend": {}}, {}
        for hour, b in buckets.items():
            both = b["weekday"] + b["weekend"]
            for kind in ("weekday", "weekend"):
                vals = b[kind] if split and len(b[kind]) >= 2 else both
                if vals:
                    home[kind][hour] = statistics.median(vals)
            if b["pv"]:
                pv[hour] = statistics.median(b["pv"])
        n_days = len(days_by_kind["weekday"] | days_by_kind["weekend"])
        return home, pv, n_days

    # ---------------------------------------------------------------- model
    def compute(self, live, weather=None, prices=None, now=None):
        now = now or time.time()
        b = (live or {}).get("battery") or {}
        soc, cap = b.get("soc"), b.get("rated_kwh")
        if soc is None or not cap:
            self.result = {"enabled": False, "reason": "no battery data yet"}
            return self.result
        reserve = b.get("cutoff_low") if b.get("cutoff_low") is not None else 5.0
        top = b.get("cutoff_high") if b.get("cutoff_high") is not None else 100.0
        max_in = b.get("max_charge_w") or cap * 500
        max_out = b.get("max_discharge_w") or cap * 500
        # the inverter reports 0 for a full / empty battery; use a sensible rate for the model
        max_in = max_in if max_in > 100 else cap * 500
        max_out = max_out if max_out > 100 else cap * 500

        home_prof, pv_prof, n_days = self._profiles(now)
        if n_days < 2:
            self.result = {"enabled": False, "reason": "needs two days of history"}
            return self.result

        # solar: the weather forecast where we have it, otherwise a typical recent day
        fc = {}
        solar_source = "recent days"
        if weather and weather.get("status") == "ok" and (weather.get("factor") or {}).get("value") is not None:
            for h in weather.get("hourly") or []:
                if h.get("pv_w") is not None:
                    fc[int(h["t"])] = h["pv_w"]
            if fc:
                solar_source = "forecast"

        mid = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (mid + timedelta(days=2)).timestamp()  # to the end of tomorrow
        price_at = {}
        for p in (prices or {}).get("forecast") or []:
            if p.get("buy") is not None:
                for t in range(int(p["t"]), int(p["end"]), STEP):
                    price_at[t // STEP * STEP] = p["buy"]

        e = cap * soc / 100.0
        e_min, e_max = cap * reserve / 100.0, cap * top / 100.0
        t = now
        rows = []
        while t < end:
            lt = datetime.fromtimestamp(t, self.tz)
            hour_start = int(t // 3600 * 3600)
            kind = "weekend" if lt.weekday() >= 5 else "weekday"
            load = home_prof[kind].get(lt.hour, home_prof["weekday"].get(lt.hour, 500.0))
            pv = fc.get(hour_start)
            if pv is None:
                pv = pv_prof.get(lt.hour, 0.0) if solar_source == "recent days" or hour_start not in fc else 0.0
            dt = min(STEP, end - t) / 3600.0
            net = pv - load
            grid_in = grid_out = 0.0
            if net >= 0:
                w = min(net, max_in, max(0.0, (e_max - e) / (EFF_IN * dt) * 1000))
                e += w * EFF_IN * dt / 1000
                grid_out = (net - w) * dt / 1000
            else:
                need = -net
                w = min(need, max_out, max(0.0, (e - e_min) * EFF_OUT / dt * 1000))
                e -= w / EFF_OUT * dt / 1000
                grid_in = (need - w) * dt / 1000
            rows.append({"t": int(t), "soc": round(100 * e / cap, 1), "pv": round(pv), "load": round(load),
                         "grid_in": grid_in, "grid_out": grid_out, "price": price_at.get(int(t) // STEP * STEP)})
            t = (t // STEP + 1) * STEP

        self.result = self._summarise(now, rows, soc, reserve, top, mid.timestamp(), solar_source, n_days, cap, live)
        self.computed = now
        return self.result

    def _summarise(self, now, rows, soc, reserve, top, mid, solar_source, n_days, cap, live):
        tomorrow0, tomorrow1 = mid + 86400, mid + 2 * 86400
        clock = lambda ts: datetime.fromtimestamp(ts, self.tz).strftime("%-I:%M %p")
        when = lambda ts: (clock(ts) + (" tomorrow" if ts >= tomorrow0 else "")) if ts else None

        # tonight: the first stretch without solar surplus after now (or the coming night)
        empty_at = next((r["t"] for r in rows if r["soc"] <= reserve + 0.2 and r["pv"] < r["load"]), None)
        # when solar next covers the home (sunrise-ish), searching after the evening
        evening = next((r["t"] for r in rows if r["pv"] < r["load"] and r["t"] >= now), now)
        cover = next((r["t"] for r in rows if r["t"] > evening + 3600 and r["pv"] >= r["load"]), None)
        night = [r for r in rows if evening <= r["t"] < (cover or rows[-1]["t"])]
        low = min(night or rows, key=lambda r: r["soc"])
        grid_night = sum(r["grid_in"] for r in night)
        priced = [r for r in night if r["grid_in"] > 0 and r["price"] is not None]
        night_cost = sum(r["grid_in"] * r["price"] for r in priced) / 100 if priced else None
        full_at = next((r["t"] for r in rows if r["soc"] >= top - 0.5 and r["t"] > now + 600), None)
        if soc >= top - 0.5:
            full_at = None  # already full
        # "the coming day": the next daylight. After midnight that's today, in the evening tomorrow.
        day0 = tomorrow0
        if cover:
            day0 = datetime.fromtimestamp(cover, self.tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        day_word = "Tomorrow" if day0 >= tomorrow0 else "Today"
        tom = [r for r in rows if day0 <= r["t"] < day0 + 86400 and r["t"] >= now]
        tom_pv = sum(r["pv"] for r in tom) * STEP / 3.6e6
        tom_load = sum(r["load"] for r in tom) * STEP / 3.6e6
        tom_max = max((r["soc"] for r in tom), default=None)
        tom_spare = sum(r["grid_out"] for r in tom)
        tom_full = next((r["t"] for r in tom if r["soc"] >= top - 0.5), None)

        # cheapest hour to buy before solar takes over (only meaningful with retailer prices)
        cheap = None
        window = [r for r in rows if now <= r["t"] < (cover or tomorrow0 + 9 * 3600) and r["price"] is not None]
        if len(window) >= 4:
            best = min(range(len(window) - 3), key=lambda i: sum(x["price"] for x in window[i:i + 4]))
            cheap = {"t": window[best]["t"], "end": window[best + 3]["t"] + STEP,
                     "price": round(sum(x["price"] for x in window[best:best + 4]) / 4, 1)}

        advice = []
        if empty_at and (not cover or empty_at < cover):
            s = (f"At your usual use, the battery reaches its {reserve:g}% reserve about {when(empty_at)}, "
                 f"and the house then needs about {grid_night:.1f} kWh from the grid until the sun takes over")
            s += f" (about ${night_cost:.2f} at the forecast prices)." if night_cost is not None else "."
            advice.append(s)
        elif night:
            advice.append(f"The battery should last the night, getting down to about {low['soc']:.0f}% around "
                          f"{when(low['t'])}.")
        if tom:
            if tom_full:
                s = (f"{day_word}'s solar (about {tom_pv:.0f} kWh) should fill it again by {clock(tom_full)}"
                     + (f", with about {tom_spare:.0f} kWh to spare" if tom_spare >= 1 else ""))
                s += ", so charging from the grid overnight isn't needed." if empty_at or low["soc"] < 50 else "."
                advice.append(s)
            elif tom_max is not None:
                s = (f"{day_word}'s solar (about {tom_pv:.0f} kWh) only gets it to about {tom_max:.0f}%, against about "
                     f"{tom_load:.0f} kWh of home use.")
                if cheap:
                    s += (f" Topping up from the grid when it's cheapest ({clock(cheap['t'])} to {clock(cheap['end'])}, "
                          f"about {cheap['price']:.0f}c/kWh) could pay off.")
                advice.append(s)
        ems = ((live or {}).get("system") or {}).get("ems_mode") or ""
        managed = any(k in ems for k in ("VPP", "Remote", "AI"))

        headline = (f"Lasts until about {when(empty_at)}" if empty_at and (not cover or empty_at < cover)
                    else f"Lasts the night, lowest about {low['soc']:.0f}%" if night else "Charging from solar")
        return {
            "enabled": True, "computed": now, "headline": headline, "advice": advice,
            "managed": managed, "ems_mode": ems, "solar_source": solar_source, "history_days": n_days,
            "empty_at": empty_at if empty_at and (not cover or empty_at < cover) else None,
            "full_at": full_at, "solar_covers_at": cover,
            "lowest": {"t": low["t"], "soc": low["soc"]},
            "night_grid_kwh": round(grid_night, 1), "night_cost": round(night_cost, 2) if night_cost is not None else None,
            "tomorrow": {"label": day_word, "date": day0, "pv_kwh": round(tom_pv, 1), "home_kwh": round(tom_load, 1),
                         "max_soc": tom_max, "full_at": tom_full, "spare_kwh": round(tom_spare, 1)} if tom else None,
            "cheapest": cheap, "reserve": reserve, "top": top, "capacity_kwh": cap,
            "projection": [{"t": r["t"], "soc": r["soc"], "pv": r["pv"], "load": r["load"]} for r in rows[::2]],
        }

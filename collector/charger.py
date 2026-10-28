"""Sigen EVAC AC charger: finding it, reading it, and turning its states into charging sessions.

The charger is read through the inverter's Modbus TCP server, at the charger's own unit id.
That id is set by the installer and isn't shown in the mySigen app, so by default the
collector scans for it: one unit id per poll, so a slow or silent id never holds up the
normal readings. Read-only, like everything else here.
"""
import time
from collections import deque

from modbus import ModbusError, ModbusException, decode, scale
from registers import (CHARGER, CHARGER_ALARM_BITS, CHARGER_SET_CURRENT, CHARGER_STATE, decode_alarms,
                       looks_like_charger, reg_count)

import os
# Seconds to wait for an answer from a unit id that may not exist. The inverter passes requests
# on to a wired charger, which can take a moment, so this is generous.
PROBE_TIMEOUT = float(os.environ.get("SIGEN_CHARGER_PROBE_TIMEOUT") or 3)
RESCAN_AFTER = 12 * 3600     # if nothing was found, look again this often
WAITING_ALERT = 10 * 60      # "power offered but the car isn't taking it" after this long
OFFLINE_AFTER = 12           # consecutive failed reads before the charger counts as offline
# Inferring charging from the plant's figures (charger not on Modbus)
PLANT_ON_W = 300             # car counts as charging above this
PLANT_OFF_AFTER = 10 * 60    # ...and as stopped once below PLANT_IDLE_W for this long
PLANT_IDLE_W = 100
PLANT_COUNTER_S = 360        # charging only while the plant's charger energy counter has risen this recently (it may update coarsely)
BLOCK_START = CHARGER[0][1]
BLOCK_LEN = CHARGER[-1][1] + reg_count(CHARGER[-1][2]) - BLOCK_START


def decode_block(regs):
    out = {}
    for name, addr, dtype, gain, _u in CHARGER:
        off = addr - BLOCK_START
        out[name] = scale(decode(dtype, regs[off:off + reg_count(dtype)]), dtype, gain)
    return out


def duration(seconds):
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


class SessionTracker:
    """Turns a stream of charger readings into plug-in sessions, state log and events.

    Kept apart from the Modbus side so the simulator can replay history through it.
    """

    def __init__(self, store, log=print):
        self.store, self.log = store, log
        last = store.last_ev_state()  # carry on from before a restart without logging a fake change
        self.code, self.since = (last[1], last[0]) if last else (None, None)
        self.session = store.open_ev_session()
        self.last_t = None
        self.alarm_keys = set()
        self.waiting_flagged = False
        self.inferred = False  # True when states come from the plant's figures, not the charger

    def update(self, now, v, grid_w=None, buy_c=None, quiet=False):
        code = int(v.get("state", -1))
        _short, label, plugged = CHARGER_STATE.get(code, ("?", f"Unknown state {code}", False))
        power = max(0.0, float(v.get("power") or 0))
        total = v.get("total_kwh")
        first = self.last_t is None
        dt = 0.0 if first else max(0.0, min(now - self.last_t, 120))
        self.last_t = now
        event = (lambda msg: None) if quiet else (lambda msg: self.store.event("ev", msg))

        if code != self.code:
            prev = None if first else self.code  # no events for the state found at start-up
            self.store.ev_log(now, code, CHARGER_STATE.get(code, ("?",))[0], power, v.get("set_current"))
            if prev is not None:
                was_plugged = CHARGER_STATE.get(prev, (None, None, False))[2]
                if plugged and not was_plugged and not self.inferred:
                    event("Car plugged in")
                if code == 5:
                    event("Charging started")
                elif prev == 5 and plugged:
                    event(f"Charging paused: {label.lower()}")
                if code in (6, 7):
                    event(f"EV charger: {label.lower()}")
            self.code, self.since = code, now
            if code != 3:
                self.waiting_flagged = False

        if plugged and self.session is None:
            self.session = {"start": int(now), "end": None, "start_kwh": total, "kwh": 0.0, "grid_kwh": 0.0,
                            "cost": 0.0, "peak_w": 0.0, "charging_s": 0, "states": "", "priced": 0}
        s = self.session
        if s is not None:
            if s.get("start_kwh") is None and total is not None:
                s["start_kwh"] = total
            inc = power * dt / 3.6e6
            before = s["kwh"]
            if total is not None and s.get("start_kwh") is not None:
                s["kwh"] = max(0.0, total - s["start_kwh"])
            else:
                s["kwh"] += inc
            if s["kwh"] > before:
                s["last_rise"] = int(now)  # when energy last went into the car
            if grid_w is not None:
                from_grid = min(power, max(0.0, grid_w)) * dt / 3.6e6
                s["grid_kwh"] += from_grid
                if buy_c is not None:
                    s["cost"] += from_grid * buy_c / 100
                    s["priced"] = 1
            s["peak_w"] = max(s["peak_w"], power)
            if code == 5:
                s["charging_s"] += int(round(dt))
            short = CHARGER_STATE.get(code, ("?",))[0]
            seen = s["states"].split(",") if s["states"] else []
            if plugged and short not in seen:
                s["states"] = ",".join(seen + [short])
            # Offered power for a while without the car ever charging this session: worth a note
            # (a full car sitting in B2 after charging is normal and isn't flagged).
            if code == 3 and not s["charging_s"] and not self.waiting_flagged and now - self.since >= WAITING_ALERT:
                self.waiting_flagged = True
                event(f"Power offered for {duration(now - self.since)} but the car hasn't started charging")
            if not plugged:
                s["end"] = int(now)
                if self.inferred and s["kwh"] > 0:
                    # worked out from the plant: the session ended when energy stopped going in, not
                    # when that was noticed (or, for a session saved before this was tracked, at the
                    # time its energy takes at its peak power)
                    last = s.get("last_rise")
                    if not last and s.get("peak_w"):
                        last = s["start"] + int(s["kwh"] * 3.6e6 / s["peak_w"])
                    if last:
                        s["end"] = min(s["end"], int(last) + 60)
                        s["charging_s"] = min(s["charging_s"], s["end"] - s["start"])
                self.store.save_ev_session(s)
                if self.inferred:
                    event(f"Charging finished after {duration(s['charging_s'])}: {s['kwh']:.1f} kWh")
                else:
                    event(f"Car unplugged after {duration(s['end'] - s['start'])}: {s['kwh']:.1f} kWh"
                          + ("" if s["charging_s"] else ", it never started charging"))
                self.session = None

        alarms, _ignored = decode_alarms(v, CHARGER_ALARM_BITS)
        keys = {(a["register"], a["bit"]) for a in alarms}
        for a in alarms:
            if (a["register"], a["bit"]) not in self.alarm_keys:
                event(f"EV charger alarm: {a['message']}")
        self.alarm_keys = keys
        return alarms

    def save(self):
        if self.session is not None:
            self.store.save_ev_session(self.session)


class ChargerWatch:
    def __init__(self, client, store, setting, skip_units, log=print):
        self.client, self.store, self.log = client, store, log
        setting = (setting or "auto").strip().lower()
        # auto: scan for the charger, using the plant's figures meanwhile; plant: never scan,
        # just use the plant's figures (charger has no Modbus address); a number: read that unit
        self.mode = ("off" if setting in ("0", "off", "none", "no") else setting if setting in ("auto", "plant")
                     else "fixed")
        self.unit = int(setting) if self.mode == "fixed" else None
        self.skip = set(skip_units)
        self.queue, self.next_scan, self.scanned = [], 0.0, 0
        self.scan_log = {}
        self.status = {"off": "off", "auto": "scanning", "plant": "plant"}.get(self.mode, "connecting")
        self.values, self.alarms, self.fails, self.error = {}, [], 0, None
        self.tracker = SessionTracker(store, log) if self.mode != "off" else None
        self.last_save = 0.0
        self.set_current_ok = True
        self.source = None            # "charger" (read directly) or "plant" (inferred)
        self.plant_ev_w = None
        self.counter = deque()        # (time, plant EV energy counter) for the last few minutes
        self.charging_since_low = None
        self.plant_charging = False
        self.counter_last, self.counter_moved_at = None, None
        self.plant_logged = False

    # ---- discovery ---------------------------------------------------------------
    def _start_scan(self):
        # Low ids first: installers almost always number devices from 1.
        self.queue = [u for u in range(1, 247) if u not in self.skip]
        self.scanned, self.scan_log = 0, {}
        self.status = "scanning"
        self.log("Looking for an AC charger (scanning Modbus unit ids 1-246, one per poll)")

    def _probe(self, unit):
        try:
            regs = self.client.read_input_registers(unit, BLOCK_START, BLOCK_LEN, timeout=PROBE_TIMEOUT)
        except ModbusException as e:
            return f"answered, not a charger (exception {e.code})"
        except (OSError, ModbusError):
            return "no answer"
        v = decode_block(regs)
        if looks_like_charger(v):
            return v
        # Keep what it said, so /api/debug shows why it didn't count as a charger
        return "answered, but not charger data: " + ", ".join(f"{k}={v[k]}" for k in
                                                              ("state", "rated_power", "rated_current", "rated_voltage", "power"))

    def _scan_step(self, now):
        if not self.queue:
            if now < self.next_scan:
                return
            self._start_scan()
        unit = self.queue.pop(0)
        self.scanned += 1
        r = self._probe(unit)
        if isinstance(r, dict):
            self.unit, self.status, self.queue = unit, "ok", []
            self.scan_log[unit] = "AC charger"
            self.log(f"Found an AC charger at Modbus unit {unit} ({r['rated_power'] / 1000:g} kW, "
                     f"{r['rated_current']:g} A). Set SIGEN_CHARGER_UNIT={unit} to skip the scan next time.")
            self.store.event("ev", f"Found the AC charger (Modbus unit {unit})")
            return r
        if r != "no answer":
            self.scan_log[unit] = r
        if not self.queue:
            self.status = "not found"
            self.next_scan = now + RESCAN_AFTER
            answered = ", ".join(f"{u}: {r}" for u, r in self.scan_log.items()) or "none"
            self.log("No AC charger found on unit ids 1-246; will look again in 12 hours. "
                     "If you have one, set SIGEN_CHARGER_UNIT to its id. Ids that answered: " + answered)
        return None

    # ---- reading -----------------------------------------------------------------
    # ---- charging from the plant's own figures -------------------------------------
    def _from_plant(self, now, p):
        """The charger can't be read (no Modbus address), but the plant still reports it.

        Power: total load minus general load (the plant's split of the car from the house),
        or, if that stays at zero, the rise of the plant's AC-charger energy counter.

        The counter is the judge of whether the car is charging at all: the two load figures can
        briefly disagree by a few hundred watts (for example while the battery exports hard), which
        would otherwise look like the car drawing power and keep a finished session open.
        """
        total = p.get("total_evac_kwh")
        if total is None:
            return None
        self.counter.append((now, total))
        while self.counter and now - self.counter[0][0] > 6 * 60:
            self.counter.popleft()
        if self.counter_last is not None and total > self.counter_last:
            self.counter_moved_at = now
        self.counter_last = total
        moved = self.counter_moved_at is not None and now - self.counter_moved_at <= PLANT_COUNTER_S
        diff = None
        if p.get("load_power") is not None and p.get("general_load_power") is not None:
            diff = max(0.0, float(p["load_power"]) - float(p["general_load_power"]))
        w = diff or 0.0
        t0, k0 = self.counter[0]
        if w < 50 and now - t0 >= 120 and total > k0:  # counter moved: 0.01 kWh steps, so average
            w = (total - k0) * 3.6e6 / (now - t0)
        if not moved:
            w = 0.0  # the counter hasn't moved: whatever the load figures say, the car isn't charging
        self.plant_ev_w = w
        if moved and w >= PLANT_ON_W:
            self.plant_charging, self.charging_since_low = True, None
        elif self.plant_charging:
            if not moved:
                self.plant_charging, self.charging_since_low = False, None
            elif w < PLANT_IDLE_W:
                self.charging_since_low = self.charging_since_low or now
                if now - self.charging_since_low >= PLANT_OFF_AFTER:
                    self.plant_charging = False
            else:
                self.charging_since_low = None
        if total <= 0 and not self.plant_charging:
            return None  # the plant has never sent anything to a charger: assume there isn't one
        return {"state": 5 if self.plant_charging else 8, "total_kwh": total, "power": w,
                "alarm1": 0, "alarm2": 0, "alarm3": 0}

    def read(self, now, grid_w=None, buy_c=None, plant=None):
        """Called once per collector poll. Returns the charger's live summary (or None if off)."""
        if self.mode == "off":
            return None
        v = None
        if self.unit is None:
            v = self._scan_step(now) if self.mode == "auto" else None
            if v is None and plant is not None:
                pv = self._from_plant(now, plant)
                if pv is not None:
                    if not self.plant_logged and self.status != "scanning":
                        self.plant_logged = True
                        self.log("Showing car charging from the plant's figures; the charger itself "
                                 "isn't readable over Modbus (it has no Modbus address)")
                    self.source, self.tracker.inferred, self.values = "plant", True, pv
                    # While still searching, a charger may yet turn up with its real states, so
                    # don't start or end sessions from the plant's figures until the search is over.
                    if self.status != "scanning":
                        self.alarms = self.tracker.update(now, pv, grid_w, buy_c)
                    if now - self.last_save >= 60:
                        self.tracker.save()
                        self.last_save = now
                return self.summary(now)
        else:
            try:
                regs = self.client.read_input_registers(self.unit, BLOCK_START, BLOCK_LEN)
                v = decode_block(regs)
                self.fails, self.error = 0, None
                self.status = "ok"
            except (OSError, ModbusError) as e:
                self.fails += 1
                self.error = f"{type(e).__name__}: {e}"
                if self.fails == OFFLINE_AFTER:
                    self.status = "offline"
                    self.log(f"AC charger at unit {self.unit} isn't answering ({self.error})")
                    self.store.event("ev", "Lost contact with the AC charger")
        if v is not None:
            if self.set_current_ok:
                try:
                    regs = self.client.read_holding_registers(self.unit, CHARGER_SET_CURRENT[1], 2,
                                                              timeout=PROBE_TIMEOUT)
                    v["set_current"] = scale(decode("u32", regs), "u32", CHARGER_SET_CURRENT[3])
                except (OSError, ModbusError) as e:
                    self.set_current_ok = False  # firmware doesn't expose it; don't ask again
                    self.log(f"AC charger current setting not readable ({e}); carrying on without it")
            if not self.values or self.source != "charger":
                self.log(f"Reading the AC charger at Modbus unit {self.unit}")
            self.source, self.tracker.inferred = "charger", False
            self.values = v
            self.alarms = self.tracker.update(now, v, grid_w, buy_c)
            if now - self.last_save >= 60:
                self.tracker.save()
                self.last_save = now
        return self.summary(now)

    @property
    def power(self):
        if self.source == "plant" and self.unit is None:
            return self.plant_ev_w
        if self.status != "ok" or not self.values:
            return None
        return max(0.0, float(self.values.get("power") or 0))

    def summary(self, now=None):
        if self.mode == "off":
            return {"enabled": False, "status": "off"}
        now = now or time.time()
        out = {"enabled": True, "status": self.status, "unit": self.unit, "error": self.error,
               "source": self.source if (self.unit is not None or self.source == "plant") else None}
        if self.unit is None:
            out["scan"] = {"checked": self.scanned, "of": self.scanned + len(self.queue)}
            if self.source != "plant" or not self.values:
                return out
        v = self.values
        if not v:
            return out
        code = int(v.get("state", -1))
        short, label, plugged = CHARGER_STATE.get(code, ("?", f"Unknown state {code}", False))
        t = self.tracker
        s = t.session
        out.update({
            "state_code": code, "state": short, "state_label": label, "plugged": plugged,
            "charging": code == 5, "since": t.since,
            "power": self.power, "total_kwh": v.get("total_kwh"),
            "rated_power": v.get("rated_power"), "rated_current": v.get("rated_current"),
            "rated_voltage": v.get("rated_voltage"), "breaker_current": v.get("breaker_current"),
            "set_current": v.get("set_current"), "alarms": self.alarms,
            "session": None if s is None else {
                "start": s["start"], "seconds": int(now - s["start"]), "kwh": round(s["kwh"], 2),
                "grid_kwh": round(s["grid_kwh"], 2), "cost": round(s["cost"], 2) if s["priced"] else None,
                "peak_w": round(s["peak_w"]), "charging_s": s["charging_s"], "states": s["states"]},
        })
        return out

    def debug(self):
        return {"mode": self.mode, "status": self.status, "unit": self.unit, "error": self.error,
                "source": self.source, "plant_ev_w": self.plant_ev_w,
                "scan_checked": self.scanned, "scan_remaining": len(self.queue),
                "scan_answers": {str(k): v for k, v in self.scan_log.items()}, "values": self.values}

    def close(self):
        if self.tracker:
            self.tracker.save()

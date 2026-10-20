"""SigenStor Modbus TCP simulator.

Serves the same input registers as a real SigenStor (plant unit 247 + inverter unit 1)
from a simple physical model of an example home system. Defaults: 10 kWp array,
16 kWh battery, 10 kW inverter, 5 kW export limit, spring day length. Change them
to resemble your own system with the SIM_* settings below.

Use it while Modbus TCP on the real system isn't available:
    python simulator.py --port 5020
then point the collector at it (SIGEN_HOST=127.0.0.1 SIGEN_PORT=5020).

Settings (environment variables):
  SIM_PV_KWP            solar array size, kWp                    default 10
  SIM_BATTERY_KWH       battery capacity, kWh                    default 16
  SIM_INVERTER_KW       inverter rating, kW                      default 10
  SIM_EXPORT_LIMIT_KW   grid export limit, kW                    default 5
  SIM_ALARMS            e.g. "alarm6:1,alarm1:9" sets alarm bits so you can see the alarm UI
"""
import argparse
import hashlib
import math
import os
import socketserver
import struct
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modbus import encode
from registers import INVERTER, PLANT, PLANT_UNIT_ID, reg_count

def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


PV_KWP = _env_float("SIM_PV_KWP", 10)
BATTERY_KWH = _env_float("SIM_BATTERY_KWH", 16)
INVERTER_W = _env_float("SIM_INVERTER_KW", 10) * 1000
EXPORT_LIMIT_W = _env_float("SIM_EXPORT_LIMIT_KW", 5) * 1000
MAX_BATT_W = min(INVERTER_W, BATTERY_KWH * 1000 * 0.5)  # ~0.5C, capped by the inverter
MIN_SOC, MAX_SOC = 5.0, 100.0
LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Australia/Sydney")


def local_now():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def _rand(*key):
    """Deterministic 0..1 value from a key (so backfilled history is stable)."""
    h = hashlib.sha256(repr(key).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _smooth_noise(day_key, minute, scale_min):
    """Smooth 0..1 noise by interpolating random points every `scale_min` minutes."""
    i = int(minute // scale_min)
    f = (minute % scale_min) / scale_min
    a, b = _rand(day_key, scale_min, i), _rand(day_key, scale_min, i + 1)
    f = f * f * (3 - 2 * f)
    return a + (b - a) * f


def solar_w(t: datetime):
    day_key = t.date().isoformat()
    minute = t.hour * 60 + t.minute + t.second / 60
    # A spring day with daylight saving: sunrise ~05:50, sunset ~19:10, solar noon ~12:30
    rise, sset = 5 * 60 + 50, 19 * 60 + 10
    if minute <= rise or minute >= sset:
        return 0.0
    x = (minute - rise) / (sset - rise)
    clear = math.sin(math.pi * x) ** 1.4 * PV_KWP * 1000 * 0.74
    cloudiness = _rand(day_key, "cloud") * 0.75  # 0 = clear day, 0.75 = heavy cloud
    passing = _smooth_noise(day_key, minute, 7) * 0.6 + _smooth_noise(day_key, minute, 31) * 0.4
    factor = 1 - cloudiness * passing
    return max(0.0, clear * factor)


def load_w(t: datetime):
    day_key = t.date().isoformat()
    minute = t.hour * 60 + t.minute + t.second / 60
    h = minute / 60
    w = 420 + 120 * math.sin(minute / 9)  # base + fridge cycling
    if 6.4 <= h <= 8.6:
        w += 1300 * math.sin(math.pi * (h - 6.4) / 2.2)
    if 16.8 <= h <= 21.8:
        w += 2600 * math.sin(math.pi * (h - 16.8) / 5.0)
    if 12 <= h <= 15 and _rand(day_key, "washer") > 0.5:
        w += 900
    # short appliance spikes (kettle, oven, dryer)
    slot = int(minute // 6)
    r = _rand(day_key, "spike", slot)
    if r > 0.93:
        w += 1800 + 1200 * _rand(day_key, "spikew", slot)
    w += 250 * _smooth_noise(day_key, minute, 3)
    return w


class SimPlant:
    def __init__(self, start=None, soc=55.0):
        self.t = start or local_now()
        self.soc = soc
        self.totals = {"pv": 9850.0, "load": 7420.0, "charge": 3110.0, "discharge": 2870.0,
                       "import": 1640.0, "export": 4380.0}
        self.daily = {"pv": 0.0, "load": 0.0, "charge": 0.0, "discharge": 0.0}
        self.yesterday_pv = 61.3
        self.p = {"pv": 0.0, "load": 0.0, "battery": 0.0, "grid": 0.0}
        self.lock = threading.Lock()

    def _dispatch(self, t, pv, load):
        """Self-consumption with an export limit and an Amber-style evening export event."""
        h = t.hour + t.minute / 60
        surplus = pv - load
        if surplus >= 0:
            batt = min(surplus, MAX_BATT_W) if self.soc < MAX_SOC - 0.05 else 0.0
        else:
            batt = max(surplus, -MAX_BATT_W) if self.soc > MIN_SOC + 0.05 else 0.0
        # Price spike export window on some days (what Amber SmartShift does)
        if 18.0 <= h < 18.5 and _rand(t.date().isoformat(), "spike-export") > 0.45 and self.soc > 35:
            batt = -min(MAX_BATT_W, load + EXPORT_LIMIT_W)
        grid = load + batt - pv
        if grid < -EXPORT_LIMIT_W:  # export limited -> curtail PV
            pv -= (-grid - EXPORT_LIMIT_W)
            grid = -EXPORT_LIMIT_W
        return max(pv, 0.0), batt, grid

    def step(self, now=None):
        with self.lock:
            now = now or local_now()
            dt = max(0.0, min((now - self.t).total_seconds(), 600))
            if now.date() != self.t.date():
                self.yesterday_pv = self.daily["pv"]
                self.daily = {k: 0.0 for k in self.daily}
            self.t = now
            pv_raw, load = solar_w(now), load_w(now)
            pv, batt, grid = self._dispatch(now, pv_raw, load)
            kwh = lambda w: max(w, 0) * dt / 3.6e6
            self.totals["pv"] += kwh(pv); self.daily["pv"] += kwh(pv)
            self.totals["load"] += kwh(load); self.daily["load"] += kwh(load)
            self.totals["charge"] += kwh(batt); self.daily["charge"] += kwh(batt)
            self.totals["discharge"] += kwh(-batt); self.daily["discharge"] += kwh(-batt)
            self.totals["import"] += kwh(grid); self.totals["export"] += kwh(-grid)
            self.soc += (batt * dt / 3.6e6) / BATTERY_KWH * 100 * (0.96 if batt > 0 else 1.0)
            self.soc = min(MAX_SOC, max(MIN_SOC, self.soc))
            self.p = {"pv": pv, "load": load, "battery": batt, "grid": grid}

    # ---- register images ---------------------------------------------------
    def plant_values(self, alarms):
        p, s = self.p, self.soc
        v = {
            "system_time": time.time(), "timezone_offset": 660, "ems_work_mode": 6,
            "grid_sensor_status": 1, "grid_power": p["grid"], "on_off_grid": 0,
            "max_active_power": INVERTER_W, "battery_soc": s, "plant_active_power": p["pv"] - p["battery"],
            "pv_power": p["pv"], "battery_power": p["battery"],
            "battery_max_charge_power": MAX_BATT_W if s < 100 else 0,
            "battery_max_discharge_power": MAX_BATT_W if s > MIN_SOC else 0, "running_state": 1,
            "battery_chargeable_kwh": (MAX_SOC - s) / 100 * BATTERY_KWH,
            "battery_dischargeable_kwh": (s - MIN_SOC) / 100 * BATTERY_KWH,
            "battery_rated_kwh": BATTERY_KWH, "charge_cutoff_soc": MAX_SOC,
            "discharge_cutoff_soc": MIN_SOC, "battery_soh": 99.6,
            "total_pv_kwh": self.totals["pv"], "daily_load_kwh": self.daily["load"],
            "total_load_kwh": self.totals["load"], "total_charge_kwh": self.totals["charge"],
            "total_discharge_kwh": self.totals["discharge"], "total_import_kwh": self.totals["import"],
            "total_export_kwh": self.totals["export"], "daily_pv_kwh": self.daily["pv"],
            "yesterday_pv_kwh": self.yesterday_pv, "general_load_power": p["load"],
            "load_power": p["load"], "battery_avg_cell_temp": 24.5 + abs(p["battery"]) / 2500,
        }
        for i in range(1, 8):
            v[f"alarm{i}"] = alarms.get(f"alarm{i}", 0)
        return v

    # Example roof: four strings on three roof faces, so the simulated strings peak at
    # different times of day (like a real east/north/west install).
    #   (name, panels, solar-time offset of the face's peak in hours)
    LAYOUTS = {
        1: [("north", 18, 0.0)],
        2: [("east", 9, -2.6), ("west", 9, 2.6)],
        3: [("east", 8, -2.6), ("north", 10, 0.0), ("west", 8, 2.6)],
        4: [("east", 8, -2.6), ("north", 10, 0.0), ("north", 6, 0.0), ("west", 8, 2.6)],
    }
    _n = max(1, min(36, int(os.environ.get("SIM_PV_STRINGS") or 4)))
    STRINGS = LAYOUTS.get(_n) or [(("east", "north", "west")[i % 3], 8 + (i % 4), (-2.6, 0.0, 2.6)[i % 3])
                                  for i in range(_n)]

    def string_values(self):
        """[(volts, amps)] for PV1..PV4 from the current PV power and time of day."""
        pv = self.p["pv"]
        h = self.t.hour + self.t.minute / 60
        noon = 12.5
        weights = []
        for _name, panels, offset in self.STRINGS:
            x = (h - (noon + offset)) / 7.5  # angle away from this face's best time
            facing = max(0.0, math.cos(math.pi / 2 * min(1.0, abs(x)))) ** 1.3
            weights.append(panels * (0.15 + 0.85 * facing))  # diffuse light still reaches every face
        total = sum(weights) or 1
        out = []
        for (_name, panels, _o), w in zip(self.STRINGS, weights):
            share = pv * w / total
            if pv < 30:
                out.append((0.0, 0.0))
                continue
            vmp = panels * 41.5 * (0.93 + 0.07 * min(1.0, share / (panels * 380)))
            out.append((round(vmp, 1), share / vmp))
        return out

    def inverter_values(self):
        p = self.p
        pv = p["pv"]
        strings = self.string_values()
        v = {
            "model": "SigenStor (simulated)", "serial": "SIM00000000000001",
            "firmware": "SIMULATOR", "rated_power": INVERTER_W,
            "daily_charge_kwh": self.daily["charge"], "daily_discharge_kwh": self.daily["discharge"],
            "running_state": 1, "active_power": pv - p["battery"], "battery_power": p["battery"],
            "battery_soc": self.soc, "battery_soh": 99.6,
            "battery_avg_cell_temp": 24.5 + abs(p["battery"]) / 2500, "battery_avg_cell_voltage": 3.31,
            "battery_max_temp": 26.8 + abs(p["battery"]) / 2000, "battery_min_temp": 23.4,
            "battery_max_cell_voltage": 3.34, "battery_min_cell_voltage": 3.29,
            "grid_frequency": 49.98 + 0.04 * math.sin(time.time() / 13), "pcs_temp": 38 + pv / 1500,
            "phase_a_voltage": 241.3 + 2 * math.sin(time.time() / 29),
            "phase_a_current": (pv - p["battery"]) / 241.3, "pack_count": max(1, round(BATTERY_KWH / 8)), "pv_string_count": len(self.STRINGS),
            "mppt_count": min(4, len(self.STRINGS)), "pv_power": pv, "insulation_resistance": 3.2, "daily_pv_kwh": self.daily["pv"],
        }
        for i, (vv, ii) in enumerate(strings, 1):
            v[f"pv{i}_voltage"], v[f"pv{i}_current"] = vv, ii
        for i in range(1, 6):
            v[f"alarm{i}"] = 0
        return v


def register_image(defs, values):
    regs = {}
    for name, addr, dtype, gain, _u in defs:
        val = values.get(name, 0)
        raw = val if dtype.startswith("str") else float(val) * gain
        for i, r in enumerate(encode(dtype, raw)):
            regs[addr + i] = r
    return regs


def parse_alarms(spec):
    out = {}
    for part in filter(None, (spec or "").split(",")):
        key, bit = part.split(":")
        out[key.strip()] = out.get(key.strip(), 0) | (1 << int(bit))
    return out


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        plant, inverter_unit = self.server.plant, self.server.inverter_unit
        while True:
            try:
                hdr = self._recv(sock, 7)
                if hdr is None:
                    return
                tid, proto, length, unit = struct.unpack(">HHHB", hdr)
                pdu = self._recv(sock, length - 1)
                if pdu is None:
                    return
            except OSError:
                return
            fc = pdu[0]

            def reply(body):
                sock.sendall(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)

            if fc not in (3, 4):
                reply(bytes([fc | 0x80, 1]))
                continue
            addr, count = struct.unpack(">HH", pdu[1:5])
            plant.step()
            if unit == PLANT_UNIT_ID:
                regs = register_image(PLANT, plant.plant_values(self.server.alarms))
            elif unit == inverter_unit:
                regs = register_image(INVERTER, plant.inverter_values())
            else:
                continue  # real devices time out for unknown unit ids
            if any((addr + i) not in regs for i in range(count)):
                reply(bytes([fc | 0x80, 2]))  # illegal data address, like the real device
                continue
            data = b"".join(struct.pack(">H", regs[addr + i]) for i in range(count))
            reply(bytes([fc, len(data)]) + data)

    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("SIM_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SIM_PORT", "5020")))
    ap.add_argument("--inverter-unit", type=int, default=int(os.environ.get("SIM_INVERTER_UNIT", "1")))
    args = ap.parse_args()
    srv = Server((args.host, args.port), Handler)
    srv.plant = SimPlant()
    # Warm up so SOC/day counters reflect the time of day rather than starting at zero
    start = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    srv.plant.t, t = start, start
    while t < local_now():
        t += timedelta(minutes=1)
        srv.plant.step(min(t, local_now()))
    srv.inverter_unit = args.inverter_unit
    srv.alarms = parse_alarms(os.environ.get("SIM_ALARMS"))
    print(f"SigenStor simulator listening on {args.host}:{args.port} "
          f"(plant unit {PLANT_UNIT_ID}, inverter unit {args.inverter_unit})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

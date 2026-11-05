"""The collector loop: reads the inverter every few seconds, builds the live state, records minute
readings and daily health figures, runs the checks (string health, storms), and wires up the
built-in simulator.
"""
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from config import *  # noqa: F401,F403  (settings, log, local_day, ...)
from aemo import Aemo  # noqa: E402
from charger import ChargerWatch  # noqa: E402
from weather import Weather  # noqa: E402
from outlook import Outlook  # noqa: E402
from money import Money  # noqa: E402
from importer import import_folder, reload_bills  # noqa: E402
from fuel import Fuel  # noqa: E402
from backup import Backups  # noqa: E402
from strings_health import StringsHealth  # noqa: E402
import gapfill  # noqa: E402
from limits import Limits  # noqa: E402
from wear import battery_wear  # noqa: E402
from advice import appliance_advice, parse as parse_appliances  # noqa: E402
from mqtt import Mqtt  # noqa: E402
from baseload import baseload  # noqa: E402
from amber import Amber  # noqa: E402
from localvolts import LocalVolts  # noqa: E402
from modbus import DeviceReader, ModbusError, ModbusTCPClient  # noqa: E402
from registers import (EMS_WORK_MODE, INVERTER, MAX_PV_STRINGS, ON_OFF_GRID, PLANT,  # noqa: E402
                       RUNNING_STATE, decode_alarms)


class Collector:
    def __init__(self, store, host, port, retailer=None, aemo=None, weather=None):
        self.store = store
        self.weather = weather
        self.outlook = Outlook(store, TZ, log=log, time_format=TIME_FORMAT)
        self.outlook_summary = None
        self.retailer = retailer
        self.aemo = aemo
        self.client = ModbusTCPClient(host, port)
        self.plant = DeviceReader(self.client, PLANT_UNIT, PLANT)
        self.inverter = DeviceReader(self.client, INVERTER_UNIT, INVERTER) if INVERTER_UNIT else None
        # Strings beyond PV4 are only read once the inverter says it has them (register 31025)
        self.extra_strings = {f"pv{n}_{k}" for n in range(5, MAX_PV_STRINGS + 1) for k in ("voltage", "current")}
        if self.inverter:
            self.inverter.unsupported |= self.extra_strings
        self.string_count = None
        self.charger = ChargerWatch(self.client, store, CHARGER_SETTING, {0, PLANT_UNIT, INVERTER_UNIT}, log=log)
        self.state = {"status": "connecting", "mode": "simulator" if SIMULATE else "live",
                      "target": f"{host}:{port}", "updated": None, "error": None}
        self.cond = threading.Condition()
        self.version = 0
        self.minute_bucket, self.minute_acc = None, []
        self.last_alarm_set = set()
        self.fail_count = 0
        self.first_seen = {}
        self._gap_checked = False
        self.last_ignored = {}
        self.ins_level, self.ins_streak = 0, 0
        self.day_key, self.day_acc = None, {}
        self.rec_lock = threading.Lock()
        self.stopping = threading.Event()
        self.raw = {"plant": {}, "inverter": {}}
        self.health = StringsHealth(store, TZ, event=store.event, log=log)
        self.backups = self.mqtt = self.notifier = None  # set by main()
        self._backup_status, self._backup_at = None, 0
        self.storm, self._storm_logged = None, None
        self.fuel = None  # set by main()

    def snapshot(self):
        with self.cond:
            return json.loads(json.dumps(self.state))

    def run(self):
        while True:
            started = time.time()
            try:
                self.poll()
                self.fail_count = 0
            except Exception as e:  # keep running whatever happens
                self.fail_count += 1
                msg = f"{type(e).__name__}: {e}"
                kind = "connection" if isinstance(e, (OSError, ModbusError)) else "internal"
                if kind == "internal":
                    traceback.print_exc()
                if self.state.get("status") != "error":
                    log("Modbus read failed:", msg)
                    self.store.event("connection", f"Lost connection to inverter ({msg})")
                with self.cond:
                    self.state.update(status="error", error=msg, error_kind=kind)
                    self.version += 1
                    self.cond.notify_all()
                self.client.close()
            delay = POLL if self.fail_count == 0 else min(60, POLL * (2 ** min(self.fail_count, 4)))
            time.sleep(max(0.5, delay - (time.time() - started)))

    def poll(self):
        p = self.plant.read()
        if "pv_power" not in p:
            raise RuntimeError("plant registers not readable - check the plant unit id (usually 247)")
        inv = {}
        if self.inverter:
            try:
                inv = self.inverter.read()
                count = int(inv.get("pv_string_count") or 0)
                if 0 < count <= MAX_PV_STRINGS and count != self.string_count:
                    self.string_count = count
                    wanted = {f"pv{n}_{k}" for n in range(5, count + 1) for k in ("voltage", "current")}
                    self.inverter.unsupported -= wanted
                    self.inverter.unsupported |= self.extra_strings - wanted
                    if count > 4:
                        inv = self.inverter.read()  # pick up the extra strings straight away
                    log(f"Inverter reports {count} PV string{'s' if count != 1 else ''}")
            except Exception as e:  # inverter unit id may be wrong - plant data still useful
                inv = {"_error": str(e)}
        buy, sell = self.retailer.prices_now() if self.retailer else (None, None)
        ev = self.charger.read(time.time(), p.get("grid_power"), buy, p, sell_c=sell)
        self.raw = {"plant": p, "inverter": inv, "charger": self.charger.debug(), "read_at": time.time(),
                    "unsupported": {"plant": sorted(self.plant.unsupported),
                                    "inverter": sorted(self.inverter.unsupported) if self.inverter else []}}
        now = time.time()
        was = self.state.get("status")
        self.state_from(p, inv, now, ev)
        if was == "error":
            self.store.event("connection", "Connection to inverter restored")
            log("Connection restored")
        if not self._gap_checked or was == "error":
            self._gap_checked = True
            self.fill_gap(p, inv or {}, now)
        self.record(p, now, inv)
        self._track_day(p, inv, now)
        try:
            self.health.watch(now, (self.state.get("system") or {}).get("strings") or [])
            if self.health.due(now):
                self.health.compute(now)
        except Exception as e:  # a health check must never stop the readings
            self.health.computed = now
            log("String health check failed:", f"{type(e).__name__}: {e}")
            traceback.print_exc()
        if now - self.outlook.computed >= 300:
            try:
                res = self.outlook.compute(self.state, self.weather.snapshot() if self.weather else None,
                                           self.retailer.snapshot() if self.retailer else None, now,
                                           temps=getattr(self.weather, "temps", None) if self.weather else None)
                self.outlook_summary = {k: v for k, v in res.items() if k != "projection"}
                self.storm = self._storm_check(now)
            except Exception as e:  # the outlook is a nice-to-have; never stop the readings for it
                self.outlook.computed = now
                log("Battery outlook failed:", f"{type(e).__name__}: {e}")
                traceback.print_exc()

    def _feeds(self, now):
        """Each data source: status, last update and any error, for the System panel's Data feeds."""
        def from_state(name, obj, what):
            if obj is None:
                return None
            st = getattr(obj, "state", None) or {}
            if not (st.get("enabled", True)):
                return {"name": name, "status": "off", "what": what}
            return {"name": name, "status": st.get("status") or "?", "updated": st.get("updated"),
                    "error": st.get("error"), "what": what, "mode": st.get("mode")}
        out = [{"name": "Inverter", "status": "ok", "updated": now, "what": "Modbus readings",
                "mode": "simulator" if SIMULATE else "live"}]
        r = from_state((self.retailer.state.get("provider") if self.retailer else None) or "Retailer prices",
                       self.retailer, "prices and costs")
        out += [x for x in (r, from_state("AEMO", self.aemo, "wholesale market"),
                            from_state("Weather", self.weather, "forecast")) if x]
        if self.fuel is not None:
            fs = dict(getattr(self.fuel, "state", {}) or {})
            if getattr(self.fuel, "enabled", False):
                out.append({"name": "Fuel prices", "status": fs.get("status") or "?", "updated": fs.get("updated"),
                            "error": fs.get("error"), "what": getattr(self.fuel, "source", "") or ""})
        if self.mqtt:
            m = self.mqtt.summary()
            out.append({"name": "Home Assistant", "status": "ok" if m["status"] == "connected" else m["status"],
                        "error": m.get("error"), "what": "MQTT"})
        if self.notifier is not None and self.notifier.enabled:
            n = self.notifier.snapshot()
            out.append({"name": "Phone alerts", "status": n["status"], "updated": (n.get("last") or {}).get("at"),
                        "error": n.get("error"), "what": "ntfy" + (f", {len(n['open'])} open" if n.get("open") else "")})
        b = self._backup_status or {}
        if b.get("enabled"):
            last = (b.get("last") or {}).get("ts")
            st = "error" if b.get("error") else "first one soon" if not last else "stale" if now - last > 30 * 3600 else "ok"
            out.append({"name": "Backups", "status": st, "updated": last, "error": b.get("error"), "what": "nightly database copy"})
        try:
            free = shutil.disk_usage(DATA_DIR).free
            out.append({"name": "Disk space", "status": "warn" if free < 1e9 else "ok", "what": f"{free / 1e9:.1f} GB free"})
        except OSError:
            pass
        return out

    def _storm_check(self, now):
        """Thunderstorms forecast in the next 36 hours: when, and how full the battery should be then."""
        w = self.weather.snapshot() if self.weather else None
        if not w or w.get("status") != "ok":
            return None
        hours = sorted((h for h in w.get("hourly") or [] if h.get("icon") == "storm" and now - 3600 <= h["t"] <= now + 36 * 3600),
                       key=lambda h: h["t"])
        if not hours:
            return None
        start = hours[0]["t"]
        end = start + 3600
        for h in hours[1:]:
            if h["t"] > end + 3600:  # a later, separate storm: just the first
                break
            end = h["t"] + 3600
        soc = None
        for r in self.outlook.rows or []:
            if r["t"] <= max(start, now):
                soc = r["soc"]
        live = (self.state.get("battery") or {}).get("soc")
        storm = {"at": int(start), "until": int(end), "desc": hours[0].get("desc") or "Thunderstorms",
                 "soc": soc if start > now else live, "now": start <= now}
        if self._storm_logged != start:
            self._storm_logged = start
            lt = datetime.fromtimestamp(start, TZ)  # (no %-I: Alpine's C library doesn't have it)
            when = f"{lt:%a} {lt.hour % 12 or 12} {'am' if lt.hour < 12 else 'pm'}"
            self.store.event("weather", f"{storm['desc']} forecast from {when}" +
                             (f"; the battery should be about {round(storm['soc'])}% then" if storm["soc"] is not None else ""))
        return storm

    def state_from(self, p, inv, now, ev=None):
        day = local_day(now)
        base = self.store.day_start_counters(day)
        cur = {c: p.get(c) for c in COUNTERS}
        if base is None:  # nothing stored for today yet: count from first reading this session
            base = self.first_seen.setdefault(day, dict(cur))

        def today(c):
            if base and base.get(c) is not None and cur.get(c) is not None:
                return round(max(0.0, cur[c] - base[c]), 2)
            return None

        pv_today = p.get("daily_pv_kwh") if p.get("daily_pv_kwh") is not None else today("total_pv_kwh")
        load_today = p.get("daily_load_kwh") if p.get("daily_load_kwh") is not None else today("total_load_kwh")
        imp, exp = today("total_import_kwh"), today("total_export_kwh")
        chg = inv.get("daily_charge_kwh", today("total_charge_kwh"))
        dis = inv.get("daily_discharge_kwh", today("total_discharge_kwh"))
        cost = self.store.cost_for_day(day)
        ev_w = self.charger.power
        ev_today = self.store.ev_day(day) if ev_w is not None or (ev and ev.get("unit")) else None
        if ev_today is not None and load_today is not None:
            ev_today = min(ev_today, load_today)
        home_w = p.get("load_power")
        if self.charger.source == "plant" and p.get("general_load_power") is not None:
            home_w = max(0.0, p["general_load_power"])  # the plant's own figure without the car
        elif ev_w is not None and home_w is not None:
            home_w = max(0.0, home_w - ev_w)  # the plant's total load includes the car
        self_suff = None
        if load_today and imp is not None and load_today > 0.1:
            self_suff = round(max(0.0, min(100.0, (1 - imp / load_today) * 100)), 1)

        alarms, ignored = decode_alarms(p)
        if ignored != self.last_ignored:
            if ignored:
                log("Ignoring alarm registers with undefined bits set (likely not supported by this firmware): " +
                    ", ".join(f"{k}=0x{v:04X}" for k, v in ignored.items()))
            self.last_ignored = ignored
        keys = {(a["register"], a["bit"]) for a in alarms}
        for a in alarms:
            if (a["register"], a["bit"]) not in self.last_alarm_set:
                self.store.event("alarm", f"{a['source']}: {a['message']}")
        for k in self.last_alarm_set - keys:
            self.store.event("alarm-cleared", f"Alarm cleared ({k[0]} bit {k[1]})")
        self.last_alarm_set = keys

        # Insulation resistance (PV array to earth). Healthy systems typically read 0.5-0.8 MΩ;
        # the inverter protects itself below about 0.37 MΩ (its "ISO protection value").
        # Two levels: "low" (below INSULATION_WARN, worth watching) and "fault" (below
        # INSULATION_MIN, at or past the shutdown point). A level only changes after a few readings
        # in a row, so one odd sample (morning dew) doesn't flap. 0 and 65.535 (0xFFFF) mean
        # "not measured", e.g. overnight on some firmware.
        iso = inv.get("insulation_resistance")
        measured = iso is not None and 0 < iso < 65
        if measured:
            level = 2 if iso < INSULATION_MIN else 1 if iso < INSULATION_WARN else 0
            if level != self.ins_level:
                self.ins_streak += 1
                if self.ins_streak >= INSULATION_CONFIRM:
                    was, self.ins_level, self.ins_streak = self.ins_level, level, 0
                    if level == 2:
                        msg = (f"Insulation resistance {iso:.3f} MΩ, below {INSULATION_MIN:g} MΩ: "
                               "the inverter may shut down solar to protect itself")
                        log(msg); self.store.event("alarm", msg)
                    elif level == 1:
                        msg = f"Insulation resistance {iso:.3f} MΩ, below the usual {INSULATION_WARN:g} MΩ"
                        if was == 2:
                            msg += f" (back above {INSULATION_MIN:g} MΩ)"
                        log(msg); self.store.event("alarm" if was == 0 else "alarm-cleared", msg)
                    else:
                        self.store.event("alarm-cleared", f"Insulation resistance back to normal: {iso:.3f} MΩ")
            else:
                self.ins_streak = 0
        val = f"{iso:.3f} MΩ" if measured else "reading unavailable"
        if self.ins_level == 2:
            alarms = alarms + [{"source": "Dashboard check", "severity": "fault",
                                "message": f"Insulation resistance too low: {val}, below {INSULATION_MIN:g} MΩ",
                                "register": "insulation_resistance", "bit": None}]
        elif self.ins_level == 1:
            alarms = alarms + [{"source": "Dashboard check", "severity": "warning",
                                "message": f"Insulation resistance lower than usual: {val}",
                                "register": "insulation_resistance", "bit": None}]
        alarms = alarms + self.health.alarms()
        if self.backups and now - self._backup_at >= 60:
            self._backup_status, self._backup_at = self.backups.status(), now

        strings = []
        n = int(inv.get("pv_string_count") or 0) or 4
        for i in range(1, min(n, MAX_PV_STRINGS) + 1):
            v, a = inv.get(f"pv{i}_voltage"), inv.get(f"pv{i}_current")
            if v is not None and a is not None:
                strings.append({"n": i, "v": round(v, 1), "a": round(a, 2), "w": round(v * a)})

        r = lambda x, d=0: None if x is None else round(x, d)
        cell = lambda x: None if x is None or x >= 65 else round(x, 3)  # 65.535 = 0xFFFF: not reported
        state = {
            "status": "ok", "error": None, "updated": now, "version": VERSION,
            "mode": "simulator" if SIMULATE else "live", "target": self.state["target"],
            "power": {"pv": r(p.get("pv_power")), "load": r(home_w),
                      "battery": r(p.get("battery_power")), "grid": r(p.get("grid_power")), "ev": r(ev_w)},
            "battery": {
                "soc": r(p.get("battery_soc"), 1), "soh": r(p.get("battery_soh"), 1),
                "rated_kwh": r(p.get("battery_rated_kwh"), 1),
                "available_kwh": r(p.get("battery_dischargeable_kwh"), 2),
                "chargeable_kwh": r(p.get("battery_chargeable_kwh"), 2),
                "max_charge_w": r(p.get("battery_max_charge_power")),
                "max_discharge_w": r(p.get("battery_max_discharge_power")),
                "cutoff_low": r(p.get("discharge_cutoff_soc"), 1),
                "cutoff_high": r(p.get("charge_cutoff_soc"), 1),
                "cell_temp_avg": r(inv.get("battery_avg_cell_temp", p.get("battery_avg_cell_temp")), 1),
                "temp_max": r(inv.get("battery_max_temp"), 1), "temp_min": r(inv.get("battery_min_temp"), 1),
                "cell_v_avg": cell(inv.get("battery_avg_cell_voltage")),
                "cell_v_max": cell(inv.get("battery_max_cell_voltage")),
                "cell_v_min": cell(inv.get("battery_min_cell_voltage")),
                "packs": inv.get("pack_count"),
            },
            "today": {"pv": r(pv_today, 2), "import": imp, "export": exp, "ev": ev_today,
                      "load": r(load_today - (ev_today or 0) if load_today is not None else None, 2),
                      "charge": r(chg, 2), "discharge": r(dis, 2), "self_sufficiency": self_suff, "cost": cost,
                      "yesterday_pv": r(p.get("yesterday_pv_kwh"), 2)},
            "system": {
                "running_state": RUNNING_STATE.get(int(p.get("running_state", -1)), "Unknown"),
                "running_code": p.get("running_state"),
                "ems_mode": EMS_WORK_MODE.get(int(p.get("ems_work_mode", -1)), "Unknown"),
                "grid_status": ON_OFF_GRID.get(int(p.get("on_off_grid", -1)), "Unknown"),
                "grid_meter": "Connected" if p.get("grid_sensor_status") == 1 else "Not connected",
                "model": inv.get("model"), "serial": inv.get("serial"), "firmware": inv.get("firmware"),
                "grid_frequency": r(inv.get("grid_frequency"), 2), "grid_voltage": r(inv.get("phase_a_voltage"), 1),
                "grid_current": r(inv.get("phase_a_current"), 1), "pcs_temp": r(inv.get("pcs_temp"), 1),
                "insulation_mohm": r(iso, 3) if measured else None, "insulation_min_mohm": INSULATION_MIN,
                "insulation_warn_mohm": INSULATION_WARN, "insulation_level": self.ins_level,
                "inverter_error": inv.get("_error"), "strings": strings,
                "max_active_power": r(p.get("max_active_power")),
                "backup": self._backup_status or {"enabled": False},
                "mqtt": self.mqtt.summary() if self.mqtt else {"enabled": False},
                "feeds": self._feeds(now),
            },
            "alarms": alarms + [a for a in (ev or {}).get("alarms", [])],
            "ev": ev or {"enabled": False, "status": "off"},
            "alarm_registers_ignored": {k: f"0x{v:04X}" for k, v in ignored.items()},
            "events": self.store.events(12),
            "price": self.retailer.summary() if self.retailer else {"enabled": False, "status": "off"},
            "aemo": self.aemo.summary() if self.aemo else {"enabled": False, "status": "off"},
            "weather": self.weather.summary() if self.weather else {"enabled": False, "status": "off"},
            "outlook": self.outlook_summary or {"enabled": False},
            "storm": self.storm if self.storm and self.storm["until"] > now else None,
        }
        with self.cond:
            state["target"] = self.state["target"]
            self.state = state
            self.version += 1
            self.cond.notify_all()

    def _track_day(self, p, inv, now):
        """Daily min/max/average of slow-moving health figures, for the Over time charts."""
        day = local_day(now)
        if day != self.day_key:
            if self.day_key and self.day_acc:
                self.store.save_daily_stats(self.day_key, self.day_acc)
            self.day_key, self.day_acc = day, self.store.daily_stats_for(day)  # carry on after a restart
        iso = inv.get("insulation_resistance")
        vals = {"insulation": iso if iso is not None and 0 < iso < 65 else None,
                "soh": p.get("battery_soh"), "soc": p.get("battery_soc"),
                "battery_temp": inv.get("battery_max_temp", p.get("battery_avg_cell_temp")),
                "inverter_temp": inv.get("pcs_temp"), "grid_voltage": inv.get("phase_a_voltage")}
        for k, v in vals.items():
            if v is None:
                continue
            a = self.day_acc.get(k)
            if a is None:
                self.day_acc[k] = [v, v, v, 1]
            else:
                a[0], a[1], a[2], a[3] = min(a[0], v), max(a[1], v), a[2] + v, a[3] + 1
        if now - getattr(self, "_day_saved", 0) >= 300:
            self.store.save_daily_stats(day, self.day_acc)
            self._day_saved = now

    def fill_gap(self, p, inv, now):
        """After a restart or an outage: the missed minutes from the inverter's counters (gapfill.py)."""
        try:
            with self.rec_lock:
                self.flush_minute()
            last = self.store.last_sample()
            if not last:
                return
            daily = {"pv": p.get("daily_pv_kwh"), "load": p.get("daily_load_kwh"),
                     "charge": inv.get("daily_charge_kwh"), "discharge": inv.get("daily_discharge_kwh")}
            rows, info = gapfill.plan(last, {c: p.get(c) for c in COUNTERS}, now, TZ, daily_now=daily,
                                      model=self.store.model_between(last["ts"], now), profile=self.store.home_profile(),
                                      prices=self.store.prices_between(last["ts"], now),
                                      capacity_kwh=p.get("battery_rated_kwh"), soc_now=p.get("battery_soc"),
                                      soc_min=p.get("discharge_cutoff_soc"))
            if rows is None:
                if info != "short":
                    log(f"Gap since the last reading not filled: {info}")
                return
            self.store.insert_estimated(rows)
            msg = gapfill.describe(info, TZ)
            log(msg)
            self.store.event("gap", msg)
        except Exception as e:  # never stop the readings for it
            log(f"Filling the gap failed: {type(e).__name__}: {e}")
            traceback.print_exc()

    def flush_minute(self):
        """Write the readings collected so far for the current minute (also used on shutdown)."""
        if self.minute_bucket is None or not self.minute_acc:
            return
        acc = self.minute_acc
        srows = []
        for n in sorted({k for a in acc for k in a.get("strings", {})}):  # per-string minute averages
            vals = [a["strings"][n] for a in acc if n in a.get("strings", {})]
            srows.append((self.minute_bucket, n, sum(v for v, _ in vals) / len(vals),
                          sum(c for _, c in vals) / len(vals)))
        if srows:
            self.store.save_strings(srows)
        avg = lambda k: sum(a[k] for a in acc) / len(acc)
        last = acc[-1]
        buy, sell = self.retailer.prices_now() if self.retailer else (None, None)
        evs = [a["ev"] for a in acc if a.get("ev") is not None]
        self.store.insert([(self.minute_bucket, local_day(self.minute_bucket),
                            avg("pv"), avg("load"), avg("battery"), avg("grid"), last["soc"],
                            *[last[c] for c in COUNTERS], buy, sell, sum(evs) / len(evs) if evs else None)])
        self.minute_acc = []

    def record(self, p, now, inv=None):
        with self.rec_lock:
            if not self.stopping.is_set():
                self._record(p, now, inv)

    def _record(self, p, now, inv=None):
        minute = int(now // 60) * 60
        if self.minute_bucket is not None and minute != self.minute_bucket and self.minute_acc:
            self.flush_minute()
            if minute % 86400 == 0:
                self.store.prune()
        self.minute_bucket = minute
        self.minute_acc.append({"pv": p.get("pv_power", 0), "load": p.get("load_power", 0),
                                "battery": p.get("battery_power", 0), "grid": p.get("grid_power", 0),
                                "soc": p.get("battery_soc"), **{c: p.get(c) for c in COUNTERS},
                                "strings": self._strings_now(inv), "ev": self.charger.power})

    def _strings_now(self, inv):
        inv = inv or {}
        count = min(int(inv.get("pv_string_count") or 0) or 4, MAX_PV_STRINGS)
        out = {}
        for n in range(1, count + 1):
            v, a = inv.get(f"pv{n}_voltage"), inv.get(f"pv{n}_current")
            if v is not None and a is not None:
                out[n] = (v, a)
        return out


# ---------------------------------------------------------------- simulator wiring
def start_simulator(store):
    import simulator
    plant = simulator.SimPlant()
    now = datetime.now(TZ).replace(tzinfo=None)
    start = (now - timedelta(days=DEMO_DAYS if store.empty() else 0)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    plant.t = start
    # an existing database: carry on from its last reading, as a real inverter's counters would,
    # so the time the collector was off is filled in (gapfill.py) as it would be at home
    last = None if store.empty() else store.last_sample()
    if last and 0 < time.time() - last["ts"] < 31 * 86400:
        start = datetime.fromtimestamp(last["ts"], TZ).replace(tzinfo=None)
        plant.t = start
        for k, c in (("pv", "total_pv_kwh"), ("load", "total_load_kwh"), ("import", "total_import_kwh"),
                     ("export", "total_export_kwh"), ("charge", "total_charge_kwh"), ("discharge", "total_discharge_kwh")):
            if last.get(c) is not None:
                plant.totals[k] = last[c]
        if last.get("soc") is not None:
            plant.soc = last["soc"]
    fill = store.empty() and DEMO_DAYS > 0
    t, rows, prices, srows = start, [], [], []
    from charger import SessionTracker
    tracker = SessionTracker(store, log) if fill and simulator.EV_ON else None
    while t < now:
        t += timedelta(minutes=1)
        plant.step(t)
        ts = int(t.replace(tzinfo=TZ).timestamp())
        if fill:
            tot = plant.totals
            buy, feed, spot = (None, None, None) if (AMBER_KEY or LV_KEY) else Amber.sim_price(ts)
            rows.append((ts, t.strftime("%Y-%m-%d"), plant.p["pv"], plant.p["load"], plant.p["battery"],
                         plant.p["grid"], plant.soc, tot["pv"], tot["load"], tot["import"], tot["export"],
                         tot["charge"], tot["discharge"], buy, feed, plant.p["ev"] if tracker else None))
            if tracker:
                tracker.update(ts, plant.charger_values(), plant.p["grid"], buy, quiet=True)
            for n, (v, a) in enumerate(plant.string_values(), 1):
                srows.append((ts, n, v, a))
            if buy is not None and ts % 1800 == 0:
                prices.append((ts, "general", ts + 1800, buy, spot, None, Amber._descriptor(buy), None))
                prices.append((ts, "feedIn", ts + 1800, feed, spot, None, None, None))
    if prices:
        store.save_prices(prices)
    if srows:
        store.save_strings(srows)
    if rows:
        store.insert(rows)
        log(f"Pre-filled {DEMO_DAYS} days of simulated history ({len(rows)} samples)")
        # daily health figures for the Over time charts
        socs = {}
        for r in rows:
            socs.setdefault(r[1], []).append(r[6])
        for i, (day, s) in enumerate(sorted(socs.items())[:-1]):
            rnd = lambda k: simulator._rand(day, k)
            iso = 0.6 + 0.12 * rnd("iso") - (0.1 if rnd("cloud") > 0.6 else 0)
            soh = 100 - 0.45 * i / max(1, DEMO_DAYS)
            store.save_daily_stats(day, {
                "insulation": [iso, iso + 0.15, (iso + 0.08) * 1, 1], "soh": [soh, soh, soh, 1],
                "soc": [min(s), max(s), sum(s) / len(s), 1],
                "battery_temp": [21 + 3 * rnd("bt"), 26 + 5 * rnd("bt2"), 24 + 3 * rnd("bt3"), 1],
                "inverter_temp": [24 + 4 * rnd("it"), 42 + 14 * (1 - rnd("cloud")), 33 + 6 * rnd("it2"), 1],
                "grid_voltage": [236 + 3 * rnd("gv"), 248 + 4 * rnd("gv2"), 242 + 2 * rnd("gv3"), 1]})
    srv = simulator.Server(("127.0.0.1", 0), simulator.Handler)
    if fill and tracker:
        tracker.save()
    srv.plant, srv.inverter_unit = plant, INVERTER_UNIT or 1
    srv.alarms = simulator.parse_alarms(ENV("SIM_ALARMS"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "127.0.0.1", srv.server_address[1]

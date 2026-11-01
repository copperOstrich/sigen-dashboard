"""The HTTP API and static files (the dashboard, Settings page and widget)."""
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

from config import *  # noqa: F401,F403
from aemo import Aemo  # noqa: E402
from charger import ChargerWatch  # noqa: E402
from weather import Weather  # noqa: E402
from outlook import Outlook  # noqa: E402
from money import Money  # noqa: E402
from importer import import_folder, reload_bills, import_upload, import_history, clean_bill  # noqa: E402
import base64  # noqa: E402
from fuel import Fuel  # noqa: E402
from backup import Backups  # noqa: E402
from strings_health import StringsHealth  # noqa: E402
from limits import Limits  # noqa: E402
from wear import battery_wear  # noqa: E402
from advice import appliance_advice, parse as parse_appliances  # noqa: E402
from mqtt import Mqtt  # noqa: E402
from baseload import baseload  # noqa: E402
from trading import trading  # noqa: E402
from evplan import ev_plan, deadline  # noqa: E402
from amber import Amber  # noqa: E402
from localvolts import LocalVolts  # noqa: E402
from modbus import DeviceReader, ModbusError, ModbusTCPClient  # noqa: E402
from registers import (EMS_WORK_MODE, INVERTER, MAX_PV_STRINGS, ON_OFF_GRID, PLANT,  # noqa: E402
                       RUNNING_STATE, decode_alarms)


# ---------------------------------------------------------------- HTTP
class Api(BaseHTTPRequestHandler):
    server_version = "SigenDash/" + VERSION
    collector = None
    store = None
    retailer = None
    aemo = None
    weather = None
    restart = None
    backups = None
    limits = None
    updates = None
    money = None
    notifier = None

    def log_message(self, fmt, *args):
        pass

    def _headers(self, code, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._headers(code, "application/json", {"Content-Length": str(len(body))})
        self.wfile.write(body)

    def _settings_access(self):
        return settings_page.access(self.client_address[0], SETTINGS_EDIT_FROM)

    def _settings_get(self, q):
        editable, why = self._settings_access()
        path = SETTINGS_PATH or settings_file_path()
        fv = settings_page.read_file(path)
        reveal = q.get("reveal", [""])[0]
        if reveal:
            if not editable:
                return self._json({"error": "Settings can only be read from your home network"}, 403)
            return self._json({"key": reveal, "value": fv.get(reveal, CONTAINER_ENV.get(reveal, ""))})
        out = settings_page.describe(path, fv, CONTAINER_ENV, os.environ, editable)
        out.update(reason=why, version=VERSION, client=self.client_address[0] if editable else None)
        return self._json(out)

    def _post_refused(self):
        """Why this change request can't be accepted, or None. Home network only, and only from the
        dashboard itself: a JSON body (other sites can't send one without the browser asking first,
        which is refused) from the same origin."""
        editable, why = self._settings_access()
        if not editable:
            return f"Settings can't be changed from here: {why}"
        origin = self.headers.get("Origin")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json" or (
                origin and urlparse(origin).netloc != self.headers.get("Host")):
            return "Changes are only accepted from the dashboard itself"
        return None

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path not in ("/api/settings", "/api/backups", "/api/bills", "/api/import", "/api/maintenance", "/api/notify"):
                return self._json({"error": "not found"}, 404)
            refused = self._post_refused()
            if refused:
                return self._json({"error": refused}, 403)
            if url.path == "/api/backups":
                if not Api.backups or not Api.backups.enabled:
                    return self._json({"error": "Backups are turned off"}, 400)
                res = Api.backups.backup_now()
                self.collector._backup_at = 0
                return self._json(res, 200 if res.get("ok") else 500)
            n = int(self.headers.get("Content-Length") or 0)
            if n > (60_000_000 if url.path == "/api/import" else 100_000):
                return self._json({"error": "too large"}, 413)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                return self._json({"error": "expected an object"}, 400)
            if url.path == "/api/bills":
                return self._post_bills(body)
            if url.path == "/api/import":
                return self._post_import(body)
            if url.path == "/api/maintenance":
                return self._post_maintenance(body)
            if url.path == "/api/notify":
                if not Api.notifier or not Api.notifier.enabled:
                    return self._json({"error": "Set an alerts topic and save first"}, 400)
                ok, err = Api.notifier.test()
                return self._json({"ok": True} if ok else {"error": f"Couldn't send: {err}"}, 200 if ok else 502)
            changes = body.get("changes") or {}
            if not isinstance(changes, dict):
                return self._json({"error": "changes must be an object"}, 400)
            path = SETTINGS_PATH or settings_file_path()
            fv = settings_page.read_file(path)
            current = {**{k: v for k, v in CONTAINER_ENV.items()}, **fv}
            errors, clean = {}, {}
            for key, value in changes.items():
                key = str(key).strip()
                spec = settings_page.BY_KEY.get(key)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in settings_page.HIDDEN:
                    errors[key] = "Not a setting that can be changed here"
                    continue
                if spec and spec.get("readonly"):
                    errors[key] = "Can only be changed in sigen.env or the container"
                    continue
                try:
                    clean[key] = settings_page.validate(key, "" if value is None else str(value))
                except ValueError as e:
                    errors[key] = str(e)
            before = settings_page.check_together(current)
            for k, msg in settings_page.check_together({**current, **clean}).items():
                if before.get(k) != msg:  # only problems this change would cause
                    errors.setdefault(k, msg)
            if errors:
                return self._json({"error": "Some settings need fixing", "errors": errors}, 400)
            clean = {k: v for k, v in clean.items() if current.get(k, "") != v}
            if not clean:
                return self._json({"ok": True, "changed": [], "restarting": False})
            settings_page.write_file(path, clean)
            log(f"Settings changed from the dashboard ({self.client_address[0]}): {', '.join(sorted(clean))}"
                "; restarting to apply them")
            self._json({"ok": True, "changed": sorted(clean), "restarting": True})
            if Api.restart:
                threading.Timer(0.5, Api.restart).start()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def _changed_costs(self):
        if Api.money:
            Api.money.forget()

    def _post_bills(self, body):
        action = body.get("action")
        if action == "delete":
            n = self.store.delete_bill(str(body.get("start")), str(body.get("end")))
            if not n:
                return self._json({"error": "No bill with those dates"}, 404)
            log(f"Bills: removed the bill for {body.get('start')} to {body.get('end')} from the dashboard")
            self._changed_costs()
            return self._json({"ok": True, "bills": self.store.bill_check()})
        if action != "save":
            return self._json({"error": "action must be save or delete"}, 400)
        was = body.get("replace") or {}
        others = [b for b in self.store.bills() if not (b["start"] == was.get("start") and b["end"] == was.get("end"))]
        try:
            b = clean_bill(body.get("bill") or {}, others)
        except ValueError as e:
            errs = e.args[0] if e.args and isinstance(e.args[0], dict) else {"start": str(e)}
            return self._json({"error": "The bill needs fixing", "errors": errs}, 400)
        if was.get("start") and was.get("end"):
            self.store.delete_bill(str(was["start"]), str(was["end"]))
        self.store.save_bill(b, source="typed in")
        r = self.store.apply_bill(b)
        log(f"Bills: {'changed' if was else 'added'} the bill for {b['start']} to {b['end']} from the dashboard "
            f"(${b['energy_cost']:.2f} energy, ${b['export_credit']:.2f} credit)")
        self._changed_costs()
        return self._json({"ok": True, "filled": r["filled"], "bills": self.store.bill_check()})

    def _post_import(self, body):
        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            return self._json({"error": "The file didn't arrive in one piece; try again"}, 400)
        if not data:
            return self._json({"error": "The file is empty"}, 400)
        try:
            res = import_upload(self.store, DATA_DIR, body.get("name"), data, log=log)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        self._changed_costs()
        return self._json(res, 200 if res["ok"] else 422)

    def _post_maintenance(self, body):
        if body.get("action") == "vacuum":
            res = self.store.vacuum()
            log(f"Database compacted from the dashboard: {res['before'] / 1e6:.1f} MB to {res['after'] / 1e6:.1f} MB "
                f"in {res['seconds']} s")
            return self._json(res)
        if body.get("action") == "check-update":
            if not Api.updates:
                return self._json({"enabled": False})
            Api.updates._at = 0
            return self._json(Api.updates.status(wait=True))
        return self._json({"error": "unknown action"}, 400)

    def _maintenance(self):
        editable, _ = self._settings_access()
        info = self.store.db_info()
        try:
            du = shutil.disk_usage(self.store.path.parent)
            info["disk"] = {"total": du.total, "free": du.free}
        except OSError:
            pass
        if not editable:
            info["file"] = None
        return {"editable": editable, "version": VERSION, "database": info,
                "imports": import_history(DATA_DIR) if editable else [],
                "update": Api.updates.status() if Api.updates else {"enabled": False, "current": VERSION},
                "upgrade": {"image": "ghcr.io/copperostrich/sigen-dashboard"}}

    def do_OPTIONS(self):
        self._headers(204, "text/plain", {"Access-Control-Allow-Methods": "GET, OPTIONS",
                                          "Access-Control-Allow-Headers": "*"})

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/live":
                return self._json(self.collector.snapshot())
            if url.path == "/api/history":
                hours = min(24 * 31, max(1, float(q.get("hours", ["24"])[0])))
                return self._json(self.store.history(hours))
            if url.path == "/api/daily":
                days = min(800, max(1, int(q.get("days", ["30"])[0])))
                return self._json(self.store.daily(days))
            if url.path == "/api/aemo":
                if not self.aemo:
                    return self._json({"enabled": False, "status": "off"})
                return self._json({**self.aemo.snapshot(min(48, max(1, float(q.get("hours", ["24"])[0])))), "view": GRID_VIEW})
            if url.path == "/api/prices":
                a = self.retailer.snapshot() if self.retailer else {"enabled": False, "status": "off"}
                a["history"] = self.store.price_history(min(48, max(1, float(q.get("hours", ["12"])[0]))))
                return self._json(a)
            if url.path == "/api/string-history":
                hours = min(24 * 31, max(1, float(q.get("hours", ["24"])[0])))
                h = self.store.string_history(hours)
                h["today"] = self.store.string_day(datetime.now(TZ).strftime("%Y-%m-%d"))
                return self._json(h)
            if url.path == "/api/strings":
                hours = min(24 * 31, max(1, float(q.get("hours", ["48"])[0])))
                rows = self.store.strings(hours)
                if q.get("format", [""])[0] == "csv":
                    lines = ["timestamp_utc,local_time,string,voltage_v,current_a,power_w"]
                    for ts, n, v, a in rows:
                        lt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")
                        lines.append(f"{ts},{lt},PV{n},{v:.1f},{a:.2f},{v * a:.0f}")
                    body = ("\n".join(lines) + "\n").encode()
                    self._headers(200, "text/csv", {"Content-Length": str(len(body)),
                                                    "Content-Disposition": 'attachment; filename="pv-strings.csv"'})
                    self.wfile.write(body)
                    return
                return self._json([{"t": ts, "string": n, "v": round(v, 1), "a": round(a, 2)} for ts, n, v, a in rows])
            if url.path in ("/eink.png", "/api/eink.png"):
                import eink
                if not eink.AVAILABLE:
                    return self._json({"error": "The e-ink image needs Pillow (included in the Docker image)."}, 501)
                w = min(3000, max(300, int(q.get("w", ["1404"])[0])))
                h = min(4000, max(300, int(q.get("h", ["1872"])[0])))
                if q.get("away", [""])[0] == "1":
                    body = eink.render_away(width=w, height=h, version=VERSION, title=(q.get("title", [""])[0] or None),
                                            rm_owner=q.get("rm", [""])[0] == "1")
                    self._headers(200, "image/png", {"Content-Length": str(len(body))})
                    self.wfile.write(body)
                    return
                body = eink.render(self.collector.snapshot(), self.store.history(24), tz=TZ, width=w, height=h,
                                   version=VERSION, prices=self.retailer.snapshot() if self.retailer else None,
                                   aemo=self.aemo.snapshot(3) if self.aemo else None,
                                   title=(q.get("title", [""])[0] or None), owner=(q.get("owner", [""])[0] or None),
                                   phone=(q.get("phone", [""])[0] or None),
                                   rm_owner=q.get("rm", [""])[0] == "1")
                self._headers(200, "image/png", {"Content-Length": str(len(body))})
                self.wfile.write(body)
                return
            if url.path == "/remarkable/install.sh":
                # the installer, with this collector's address filled in (as the tablet reached it)
                host = self.headers.get("Host") or f"127.0.0.1:{HTTP_PORT}"
                body = (WEB_DIR / "remarkable" / "install.sh").read_text().replace("__BASE__", f"http://{host}").encode()
                self._headers(200, "text/x-shellscript; charset=utf-8", {"Content-Length": str(len(body))})
                self.wfile.write(body)
                return
            if url.path == "/api/trends":
                days = min(800, max(7, int(float(q.get("days", ["365"])[0]))))
                since = datetime.now(TZ) - timedelta(days=days)
                stats = self.store.daily_stats(since.strftime("%Y-%m-%d"))
                strings = self.store.string_daily(since.timestamp())
                daily = {d["day"]: d for d in self.store.daily(days)}
                all_days = sorted(set(stats) | set(strings) | set(daily))
                ids = sorted({n for d in strings.values() for n in d})
                r1 = lambda v: None if v is None else round(v, 3)
                return self._json({
                    "days": all_days,
                    "strings": {n: [round(strings.get(d, {}).get(n, 0.0), 2) if d in strings else None for d in all_days]
                                for n in ids},
                    "pv": [daily[d]["pv"] if d in daily else None for d in all_days],
                    "stats": {k: {f: [r1((stats.get(d, {}).get(k) or {}).get(f)) for d in all_days]
                                  for f in ("min", "avg", "max")}
                              for k in ("insulation", "soh", "battery_temp", "inverter_temp", "grid_voltage", "soc")},
                    "insulation_limits": {"warn": INSULATION_WARN, "min": INSULATION_MIN},
                })
            if url.path == "/api/monthly":
                months = min(36, max(1, int(q.get("months", ["13"])[0])))
                rows = self.store.monthly(months)
                if q.get("format", [""])[0] == "csv":
                    f = lambda v: "" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
                    lines = ["month,days,solar_kwh,home_kwh,car_kwh,bought_kwh,sold_kwh,battery_charged_kwh,"
                             "battery_discharged_kwh,self_sufficiency_pct,self_consumption_pct,bought_dollars,"
                             "earned_dollars,net_dollars,saved_dollars,avg_buy_c_per_kwh,car_sessions,car_cost_dollars"]
                    for m in rows:
                        car = m["car"] or {}
                        lines.append(",".join(f(x) for x in (m["month"], m["days"], m["pv"], m["load"], m["ev"], m["import"],
                                     m["export"], m["charge"], m["discharge"], m["self_sufficiency"], m["self_consumption"],
                                     m["bought"], m["earned"], m["net"], m["saved"], m["avg_buy_c"], car.get("sessions"),
                                     car.get("cost"))))
                    return self._csv("home-energy-monthly.csv", lines)
                return self._json(rows)
            if url.path == "/api/money":
                return self._json(self.money.summary())
            if url.path == "/api/outlook":
                return self._json(self.collector.outlook.result or {"enabled": False, "reason": "not worked out yet"})
            if url.path == "/api/weather":
                return self._json(self.weather.snapshot() if self.weather else {"enabled": False, "status": "off"})
            if url.path in ("/api/ev", "/api/ev-sessions", "/api/ev-log"):
                return self._ev(url.path, q)
            if url.path == "/api/debug":
                raw = json.loads(json.dumps(self.collector.raw, default=str))
                for dev in ("plant", "inverter"):
                    for k, v in list((raw.get(dev) or {}).items()):
                        if k.startswith("alarm") and isinstance(v, (int, float)):
                            raw[dev][k] = f"0x{int(v):04X}"
                raw["version"] = VERSION
                return self._json(raw)
            if url.path == "/api/settings":
                return self._settings_get(q)
            if url.path == "/api/backups":
                if not Api.backups:
                    return self._json({"enabled": False})
                return self._json({**Api.backups.status(), "files": Api.backups.files()})
            if url.path == "/api/strings-health":
                return self._json(self.collector.health.result)
            if url.path == "/api/limits":
                return self._json(Api.limits.summary(min(400, max(1, int(q.get("days", ["30"])[0])))))
            if url.path == "/api/battery-wear":
                return self._json(battery_wear(self.store, self.collector.snapshot(), TZ, installed=SYSTEM_INSTALLED or None,
                                               warranty_years=BATTERY_WARRANTY_YEARS, warranty_mwh=BATTERY_WARRANTY_MWH,
                                               warranty_soh=BATTERY_WARRANTY_SOH))
            if url.path == "/api/trading":
                days = min(400, max(3, int(q.get("days", ["30"])[0])))
                bl = baseload(self.store, TZ, 30)
                return self._json(trading(self.store, self.collector.snapshot(), TZ, days,
                                          baseload_w=bl.get("now_w") if bl.get("enabled") else None))
            if url.path == "/api/ev-plan":
                try:
                    kwh = max(0.5, min(150.0, float(q.get("kwh", ["20"])[0])))
                    by = deadline(q.get("by", ["07:00"])[0], TZ, time.time())
                    rated = ((self.collector.snapshot().get("ev") or {}).get("rated_power") or 0) / 1000
                    kw = max(1.0, min(22.0, float(q.get("kw", [rated or 7])[0])))
                except (ValueError, TypeError):
                    return self._json({"error": "kwh, by (HH:MM) and kw must be numbers"}, 400)
                o = self.collector.outlook
                fc = (self.retailer.snapshot().get("forecast") if self.retailer else None) or []
                return self._json(ev_plan(o.rows, fc, kwh, by, kw, time.time()))
            if url.path == "/api/ui":
                up = Api.updates.status() if Api.updates else {}
                return self._json({"time_format": TIME_FORMAT if TIME_FORMAT in ("12h", "24h") else "12h", "version": VERSION,
                                   "update": {k: up.get(k) for k in ("latest", "url", "newer")} if up.get("newer") else None})
            if url.path == "/api/maintenance":
                return self._json(self._maintenance())
            if url.path == "/api/notify":
                n = Api.notifier.snapshot() if Api.notifier else {"enabled": False}
                n.pop("server", None) if not self._settings_access()[0] else None
                return self._json(n)
            if url.path == "/api/bills":
                return self._json({"bills": self.store.bill_check()})
            if url.path == "/api/baseload":
                return self._json(baseload(self.store, TZ, min(400, max(7, int(q.get("days", ["90"])[0])))))
            if url.path == "/api/appliances":
                o = self.collector.outlook
                fc = (self.retailer.snapshot().get("forecast") if self.retailer else None) or []
                return self._json(appliance_advice(o.rows, o.limits, fc, parse_appliances(APPLIANCES), time.time()))
            if url.path == "/api/health":
                s = self.collector.snapshot()
                return self._json({"ok": s.get("status") == "ok", "status": s.get("status"),
                                   "updated": s.get("updated"), "version": VERSION})
            if url.path == "/api/stream":
                return self._stream()
            return self._static(url.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def _csv(self, name, lines):
        body = ("\n".join(lines) + "\n").encode()
        self._headers(200, "text/csv", {"Content-Length": str(len(body)),
                                        "Content-Disposition": f'attachment; filename="{name}"'})
        self.wfile.write(body)

    def _ev(self, path, q):
        from registers import CHARGER_STATE
        days = min(800, max(1, float(q.get("days", ["30"])[0])))
        csv = q.get("format", [""])[0] == "csv"
        lt = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        if path == "/api/ev-log":
            rows = self.store.ev_states(days)
            if csv:
                return self._csv("ev-charger-states.csv", ["timestamp_utc,local_time,state,state_code,meaning,power_w,offered_current_a"] + [
                    f"{ts},{lt(ts)},{code},{st},\"{CHARGER_STATE.get(st, ('', ''))[1]}\",{p or 0:.0f},{'' if a is None else f'{a:g}'}"
                    for ts, st, code, p, a in rows])
            return self._json([{"t": ts, "state_code": st, "state": code, "power": p, "set_current": a}
                               for ts, st, code, p, a in rows])
        sessions = self.store.ev_sessions(days)
        if self.fuel.enabled:
            for x in sessions:
                x["fuel"] = self.fuel.session(x)
        if path == "/api/ev-sessions" and csv:
            fcols = ",km,fuel_litres,fuel_price_c_per_l,fuel_price_from,fuel_cost_dollars,solar_kwh,solar_at_export_dollars," \
                    "charging_cost_dollars,charging_c_per_kwh,saved_dollars" if self.fuel.enabled else ""
            lines = ["plugged_in,unplugged,minutes_plugged_in,minutes_charging,kwh,kwh_from_grid,"
                     "grid_cost_dollars,peak_kw,states_seen" + fcols]
            v = lambda n, f="{:.2f}": "" if n is None else f.format(n)
            for x in sessions:
                mins = round(((x["end"] or time.time()) - x["start"]) / 60)
                cost = "" if x["cost"] is None else f"{x['cost']:.2f}"
                row = (f"{lt(x['start'])},{lt(x['end'])},{mins},{round((x['charging_s'] or 0) / 60)},"
                       f"{x['kwh']:.2f},{x['grid_kwh']:.2f},{cost},{x['peak_w'] / 1000:.2f},"
                       f"{(x['states'] or '').replace(',', ' ')}")
                if self.fuel.enabled:
                    f = x.get("fuel") or {}
                    row += (f",{v(f.get('km'), '{}')},{v(f.get('litres'), '{}')},{v(f.get('price_c'), '{}')},{f.get('price_from') or ''},"
                            f"{v(f.get('fuel_cost'))},{v(f.get('solar_kwh'))},{v(f.get('solar_cost'))},{v(f.get('charge_cost'))},{v(f.get('charge_c_per_kwh'), '{}')},{v(f.get('saved'))}")
                lines.append(row)
            return self._csv("ev-charging-sessions.csv", lines)
        out = {"sessions": sessions, "live": self.collector.charger.summary(), "fuel": self.fuel.summary(sessions)}
        if path == "/api/ev":
            out["log"] = [{"t": ts, "state_code": st, "state": code, "power": p}
                          for ts, st, code, p, _a in self.store.ev_states(min(days, 2))]
        return self._json(out)

    def _stream(self):
        self._headers(200, "text/event-stream", {"Connection": "keep-alive"})
        c = self.collector
        seen = -1
        while True:
            with c.cond:
                if c.version == seen:
                    c.cond.wait(timeout=20)
                if c.version == seen:
                    payload = b": keepalive\n\n"
                else:
                    seen = c.version
                    payload = b"data: " + json.dumps(c.state, separators=(",", ":")).encode() + b"\n\n"
            self.wfile.write(payload)
            self.wfile.flush()

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR or not target.is_file():
            return self._json({"error": "not found"}, 404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._headers(200, ctype, {"Content-Length": str(len(body))})
        self.wfile.write(body)

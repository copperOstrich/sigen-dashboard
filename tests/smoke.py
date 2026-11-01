"""Smoke test: start the collector on the built-in simulator and check that every page and API
endpoint answers without an error, that a setting can be saved (and the collector comes back after
restarting itself), and that nothing went into the log as a traceback.

    python tests/smoke.py          (exit code 0 = all good)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["/", "/settings.html", "/report.html", "/year.html", "/widget.html", "/style.css", "/dashboard.js", "/settings.js",
         "/settings-data.js"]
APIS = ["/api/live", "/api/health", "/api/history?hours=24", "/api/daily?days=30", "/api/prices?hours=12",
        "/api/aemo?hours=24", "/api/string-history?hours=24", "/api/strings?hours=24", "/api/ev?days=30",
        "/api/ev-sessions?days=30", "/api/monthly?months=13", "/api/trends?days=90", "/api/money", "/api/outlook",
        "/api/weather", "/api/debug", "/api/settings", "/api/backups", "/api/strings-health", "/api/limits?days=30",
        "/api/battery-wear", "/api/appliances", "/api/baseload?days=30", "/api/bills", "/api/ui", "/api/trading?days=30", "/api/ev-plan?kwh=20&by=07:00",
        "/api/maintenance", "/api/notify"]
CSV = ["/api/ev-sessions?format=csv&days=30", "/api/monthly?format=csv", "/api/strings?format=csv&hours=24"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(base, path, timeout=30):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, r.read()


def wait_healthy(base, seconds=180, after=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            code, body = get(base, "/api/health", timeout=5)
            h = json.loads(body)
            if code == 200 and h.get("status") == "ok" and (after is None or (h.get("updated") or 0) > after):
                return h
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise SystemExit("collector didn't become healthy in time")


def post(base, path, obj):
    req = urllib.request.Request(base + path, method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps(obj).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


AMBER_CSV = ("NMI,Meter Serial Number,Channel,Channel Type,Day,Start Time,NEM Time,Usage,Price,Cost,Read Quality\n"
             + "".join(f"X,Y,E1,general,2026-01-05,2026-01-05 {h:02d}:00:00,2026-01-05 {h:02d}:05:00,0.1,25,2.5,ACTUAL\n"
                       for h in range(24))
             + "".join(f"X,Y,B1,feedIn,2026-01-05,2026-01-05 {h:02d}:00:00,2026-01-05 {h:02d}:05:00,-0.5,-5,2.5,ACTUAL\n"
                       for h in range(9, 15)))


def fake_ntfy():
    """A stand-in for ntfy.sh that keeps what it's sent."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", got


def data_admin(base):
    """Settings > bills, import, database and version."""
    import base64
    bad = []
    code, r = post(base, "/api/bills", {"action": "save", "bill": {"start": "2026-01-01", "end": "2026-01-31",
                                                                   "energy_cost": "40.10", "export_credit": "12"}})
    if code != 200 or not any(b["start"] == "2026-01-01" for b in r.get("bills", [])):
        bad.append(f"adding a bill: {code} {r}")
    code, r = post(base, "/api/bills", {"action": "save", "bill": {"start": "2026-01-15", "end": "2026-02-10",
                                                                   "energy_cost": "1", "export_credit": "1"}})
    if code != 400 or "start" not in r.get("errors", {}):
        bad.append(f"an overlapping bill wasn't refused: {code} {r}")
    code, r = post(base, "/api/bills", {"action": "save", "bill": {"start": "2026-01-01", "end": "2026-01-31",
                                        "energy_cost": "41", "export_credit": "12"}, "replace": {"start": "2026-01-01", "end": "2026-01-31"}})
    if code != 200 or [b["bill"]["bought"] for b in r["bills"] if b["start"] == "2026-01-01"] != [41.0]:
        bad.append(f"editing a bill: {code} {r}")
    code, r = post(base, "/api/bills", {"action": "delete", "start": "2026-01-01", "end": "2026-01-31"})
    if code != 200 or any(b["start"] == "2026-01-01" for b in r.get("bills", [])):
        bad.append(f"removing a bill: {code} {r}")
    print("  ok   bills added, refused when overlapping, edited and removed" if not bad else "  FAIL bills")
    code, r = post(base, "/api/import", {"name": "Amber Usage test.csv", "data": base64.b64encode(AMBER_CSV.encode()).decode()})
    if code != 200 or not r.get("ok") or not any("Amber usage for 1 day," in m for m in r.get("messages", [])):
        bad.append(f"importing an Amber usage CSV: {code} {r}")
    code, r = post(base, "/api/import", {"name": "../../evil.sh", "data": base64.b64encode(b"x").decode()})
    if code != 400:
        bad.append(f"a .sh upload wasn't refused: {code} {r}")
    code, r = post(base, "/api/import", {"name": "junk.csv", "data": base64.b64encode(b"a,b\n1,2\n").decode()})
    if code != 422:
        bad.append(f"a junk CSV should be reported as not imported: {code} {r}")
    m = json.loads(get(base, "/api/maintenance")[1])
    if not any(f["state"] == "imported" for f in m.get("imports", [])):
        bad.append(f"import history: {m.get('imports')}")
    print("  ok   files uploaded and imported; bad ones refused")
    code, r = post(base, "/api/maintenance", {"action": "vacuum"})
    if code != 200 or not r.get("ok"):
        bad.append(f"compacting: {code} {r}")
    ui = json.loads(get(base, "/api/ui")[1])
    for _ in range(10):
        if ui.get("update"):
            break
        time.sleep(0.5)
        ui = json.loads(get(base, "/api/ui")[1])
    if not (ui.get("update") or {}).get("newer"):
        bad.append(f"the simulated new version wasn't reported: {ui}")
    print("  ok   database compacted; new version reported")
    return bad


def main():
    data = Path(tempfile.mkdtemp(prefix="sigen-smoke-"))
    (data / "sigen.env").write_text("# smoke test\nSIGEN_SIMULATE=1\nCOMPARE_PLANS=nsw-ausgrid\n"
                                    "EV_KWH_PER_100KM=16\nCOMPARE_L_PER_100KM=9\nRETAILER_SUPPLY_C_DAY=2026-01-01:100,2026-07-01:110\n")
    port = free_port()
    ntfy_url, ntfy_got = fake_ntfy()
    env = {**os.environ, "DATA_DIR": str(data), "HTTP_PORT": str(port), "AEMO_SIMULATE": "1",
           "DEMO_HISTORY_DAYS": os.environ.get("DEMO_HISTORY_DAYS", "10"), "PYTHONUNBUFFERED": "1",
           "SIM_UPDATE": "9.9.9", "NTFY_TOPIC": "home-energy-smoketest1234", "NTFY_SERVER": ntfy_url}
    log = open(data / "collector.log", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "collector" / "sigen_collector.py")], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    failures = []
    try:
        h = wait_healthy(base)
        print(f"collector {h.get('version')} healthy on port {port}")
        time.sleep(5)
        for path in PAGES + APIS + CSV:
            try:
                code, body = get(base, path)
                if path.startswith("/api/") and "format=csv" not in path:
                    obj = json.loads(body)
                    if isinstance(obj, dict) and obj.get("error"):
                        failures.append(f"{path}: error {obj['error']}")
                print(f"  ok   {path} ({len(body)} bytes)")
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as e:
                failures.append(f"{path}: {e}")
                print(f"  FAIL {path}: {e}")
        failures += data_admin(base)
        code, r = post(base, "/api/notify", {"action": "test"})
        if code != 200 or not ntfy_got or ntfy_got[-1].get("topic") != "home-energy-smoketest1234":
            failures.append(f"test alert: {code} {r} {ntfy_got}")
        print("  ok   test alert sent to the (fake) ntfy server")
        # save a setting from "home" (loopback) and wait for the restart
        before = time.time()
        req = urllib.request.Request(base + "/api/settings", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"changes": {"SOLAR_KWP": "6.6", "NOTIFY_WEEKLY": "Sun 18:00"}}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
        if not res.get("restarting"):
            failures.append(f"saving a setting didn't restart: {res}")
        wait_healthy(base, after=before)
        if "SOLAR_KWP=6.6" not in (data / "sigen.env").read_text() or "NOTIFY_WEEKLY=sun 18:00" not in (data / "sigen.env").read_text():
            failures.append("the setting wasn't written to sigen.env")
        print("  ok   saved a setting and the collector restarted")
        # a change from another site is refused
        req = urllib.request.Request(base + "/api/settings", method="POST", data=b'{"changes":{"SOLAR_KWP":"1"}}',
                                     headers={"Content-Type": "text/plain"})
        try:
            urllib.request.urlopen(req, timeout=10)
            failures.append("a text/plain change request was accepted")
        except urllib.error.HTTPError as e:
            if e.code != 403:
                failures.append(f"a text/plain change request got {e.code}, not 403")
        print("  ok   change requests from other sites are refused")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    text = (data / "collector.log").read_text()
    if "Traceback" in text:
        failures.append("the log has a traceback:\n" + text[text.index("Traceback"):][:2000])
    if failures:
        print("\nFAILED:\n- " + "\n- ".join(failures))
        print("\n--- log tail ---\n" + text[-3000:])
        sys.exit(1)
    print("\nall good")


if __name__ == "__main__":
    main()

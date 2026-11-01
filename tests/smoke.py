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
PAGES = ["/", "/settings.html", "/report.html", "/year.html", "/widget.html", "/style.css", "/dashboard.js", "/settings.js"]
APIS = ["/api/live", "/api/health", "/api/history?hours=24", "/api/daily?days=30", "/api/prices?hours=12",
        "/api/aemo?hours=24", "/api/string-history?hours=24", "/api/strings?hours=24", "/api/ev?days=30",
        "/api/ev-sessions?days=30", "/api/monthly?months=13", "/api/trends?days=90", "/api/money", "/api/outlook",
        "/api/weather", "/api/debug", "/api/settings", "/api/backups", "/api/strings-health", "/api/limits?days=30",
        "/api/battery-wear", "/api/appliances", "/api/baseload?days=30", "/api/bills", "/api/ui", "/api/trading?days=30", "/api/ev-plan?kwh=20&by=07:00"]
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


def main():
    data = Path(tempfile.mkdtemp(prefix="sigen-smoke-"))
    (data / "sigen.env").write_text("# smoke test\nSIGEN_SIMULATE=1\nCOMPARE_PLANS=nsw-ausgrid\n"
                                    "EV_KWH_PER_100KM=16\nCOMPARE_L_PER_100KM=9\nRETAILER_SUPPLY_C_DAY=2026-01-01:100,2026-07-01:110\n")
    port = free_port()
    env = {**os.environ, "DATA_DIR": str(data), "HTTP_PORT": str(port), "AEMO_SIMULATE": "1",
           "DEMO_HISTORY_DAYS": os.environ.get("DEMO_HISTORY_DAYS", "10"), "PYTHONUNBUFFERED": "1"}
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
        # save a setting from "home" (loopback) and wait for the restart
        before = time.time()
        req = urllib.request.Request(base + "/api/settings", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"changes": {"SOLAR_KWP": "6.6"}}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
        if not res.get("restarting"):
            failures.append(f"saving a setting didn't restart: {res}")
        wait_healthy(base, after=before)
        if "SOLAR_KWP=6.6" not in (data / "sigen.env").read_text():
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

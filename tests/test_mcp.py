"""Tests for the Home Energy MCP server (mcp-server/energy_mcp.py).

Needs the MCP Python SDK (`pip install "mcp==2.3.*"`); without it these tests are skipped, unless
REQUIRE_MCP=1 (as in CI), when a missing SDK is a failure.

  * tools against a fake dashboard: what each one reads and how it reshapes it
  * the token gate, /health and the refusal to start with a short token
  * a real MCP session over HTTP: handshake, tool list, a tool call, the host check
  * every tool against the real collector running on its simulator, so a change to the
    dashboard's API that would break the MCP server fails here
"""
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "mcp-server" / "energy_mcp.py"
TOKEN = "test-token-0123456789abcdef"

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False
    if os.environ.get("REQUIRE_MCP") == "1":
        raise

TOOLS = {"home_energy_now", "electricity_prices", "battery_outlook", "best_time_to_use_power", "plan_ev_charging",
         "ev_charging", "energy_by_day", "energy_by_month", "power_history", "weather_and_solar_forecast",
         "battery_health", "system_health", "money_summary", "dashboard_api"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_server():
    os.environ.setdefault("MCP_TOKEN", TOKEN)
    spec = importlib.util.spec_from_file_location("energy_mcp_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def call(m, name, **kw):
    fn = getattr(m, name)
    fn = getattr(fn, "fn", fn)
    return asyncio.run(fn(**kw))


class FakeDashboard:
    """A stand-in for the collector's API: canned JSON per endpoint, and a log of what was asked."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                outer.seen.append((u.path, {k: v[0] for k, v in parse_qs(u.query).items()}))
                body = outer.routes.get(u.path[len("/api/"):]) if u.path.startswith("/api/") else None
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(HAVE_MCP, "MCP SDK not installed (pip install 'mcp==2.3.*')")
class ToolsAgainstFakeDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        now = time.time()
        cls.dash = FakeDashboard({
            "live": {"updated": now, "status": "ok",
                     "power": {"pv": 5200, "load": 800, "battery": -1500, "grid": 2100, "ev": 0},
                     "battery": {"soc": 61.5, "available_kwh": 24.0, "rated_kwh": 40.3, "cutoff_low": 5},
                     "today": {"pv": 30.1, "load": 12.0, "import": 1.0, "export": 15.0},
                     "price": {"enabled": True, "buy": 22.5, "sell": -3.1, "descriptor": "low", "spike": "none"},
                     "system": {"running_state": "Running", "ems_mode": "VPP scheduling",
                                "feeds": [{"name": "Amber", "status": "ok"}, {"name": "AEMO", "status": "error", "error": "500"}]}},
            "prices": {"provider": "Amber", "status": "ok",
                       "current": {"general": {"per_kwh": 22.5, "descriptor": "low", "spike": "none"},
                                   "feedIn": {"per_kwh": 3.1}},
                       "forecast": [{"t": now + 1800, "buy": 25.0, "sell": 4.0},
                                    {"t": now + 5 * 3600, "buy": 40.0, "sell": 20.0}]},
            "aemo": {"enabled": True, "current": {"price_c": 9.1, "demand_mw": 8000}, "renewables_pct": 40.0},
            "limits": {"days": [], "caps": {}},
        })

    @classmethod
    def tearDownClass(cls):
        cls.dash.close()

    def setUp(self):
        self.m = load_server()
        self.m.DASHBOARD_URL = self.dash.url
        self.dash.seen.clear()

    def test_home_energy_now(self):
        out = call(self.m, "home_energy_now")
        self.assertEqual(out["power_kw"]["solar"], 5.2)
        self.assertEqual(out["power_kw"]["battery_direction"], "discharging")
        self.assertEqual(out["power_kw"]["grid_direction"], "importing")
        self.assertEqual(out["battery"]["charge_pct"], 61.5)
        self.assertEqual(out["price_now"]["buy_c_kwh"], 22.5)
        self.assertNotIn("spike", out["price_now"])                 # "none" isn't a spike
        self.assertEqual(out["system"]["feeds_with_problems"], ["AEMO: 500"])

    def test_prices_window_and_sign(self):
        out = call(self.m, "electricity_prices", hours=2)
        self.assertEqual(out["now"]["sell"], -3.1)                   # feed-in is shown as earned per kWh
        self.assertEqual(len(out["forecast"]), 1)                    # only the next 2 hours
        self.assertEqual(out["wholesale_nsw"]["price_c"], 9.1)
        self.assertEqual(call(self.m, "electricity_prices", hours=999)["forecast"][-1]["buy"], 40.0)  # clamped to 24 h

    def test_dashboard_api_allowlist(self):
        for blocked in ("settings", "backups", "debug", "maintenance", "stream", "../settings", "api/settings"):
            out = call(self.m, "dashboard_api", endpoint=blocked)
            self.assertIn("error", out, blocked)
        self.assertFalse(self.dash.seen, "a blocked endpoint reached the dashboard")
        out = call(self.m, "dashboard_api", endpoint="/api/limits", query="?days=7&format=csv")
        self.assertIn("days", out)
        self.assertEqual(self.dash.seen[-1], ("/api/limits", {"days": "7"}))   # JSON only: format dropped

    def test_every_allowed_endpoint_is_read_only(self):
        for name in self.m.RAW_ALLOWED:
            self.assertNotIn(name, {"settings", "backups", "debug", "maintenance", "stream", "import", "notify"})

    def test_dashboard_errors_are_readable(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            call(self.m, "battery_health")                          # not in the fake: 404
        self.m.DASHBOARD_URL = f"http://127.0.0.1:{free_port()}"
        with self.assertRaisesRegex(RuntimeError, "Can't reach the dashboard"):
            call(self.m, "home_energy_now")


@unittest.skipUnless(HAVE_MCP, "MCP SDK not installed")
class TokenGate(unittest.TestCase):
    def run_gate(self, path, headers=(), kind="http"):
        m = load_server()
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["type"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request", "body": b""}
        gate = m.TokenGate(inner, TOKEN)
        asyncio.run(gate({"type": kind, "path": path, "headers": list(headers)}, receive, send))
        status = next((x["status"] for x in sent if x["type"] == "http.response.start"), None)
        body = b"".join(x.get("body", b"") for x in sent if x["type"] == "http.response.body")
        return status, body, reached, m

    def test_health_is_open_and_says_little(self):
        status, body, reached, m = self.run_gate("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "version": m.VERSION})
        self.assertFalse(reached)

    def test_token_required(self):
        for headers in ((), [(b"authorization", b"Bearer wrong")], [(b"authorization", TOKEN.encode())]):
            status, _, reached, _ = self.run_gate("/mcp", headers)
            self.assertEqual(status, 401)
            self.assertFalse(reached)
        status, body, reached, _ = self.run_gate("/mcp", [(b"authorization", b"bearer " + TOKEN.encode())])
        self.assertEqual((status, body, reached), (200, b"inner", ["http"]))

    def test_lifespan_passes_through(self):
        _, _, reached, _ = self.run_gate("", kind="lifespan")
        self.assertEqual(reached, ["lifespan"])

    def test_refuses_short_token(self):
        env = {**os.environ, "MCP_TOKEN": "short", "MCP_PORT": str(free_port())}
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("16+ characters", r.stderr)


def rpc(url, msg, token=TOKEN, host=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, data=json.dumps(msg).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def wait_for(url, seconds=60):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"{url} didn't come up")


@unittest.skipUnless(HAVE_MCP, "MCP SDK not installed")
class AgainstSimulator(unittest.TestCase):
    """The real collector on its simulator, and the MCP server over HTTP in front of it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port, cls.mcp_port = free_port(), free_port()
        base_env = {**os.environ, "TZ": "Australia/Sydney"}
        cls.collector = subprocess.Popen(
            [sys.executable, str(ROOT / "collector" / "sigen_collector.py")],
            env={**base_env, "DATA_DIR": cls.tmp.name, "HTTP_PORT": str(cls.port), "SIGEN_SIMULATE": "1",
                 "AEMO_SIMULATE": "1", "EV_SIMULATE": "1", "DEMO_HISTORY_DAYS": "7"},
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        cls.dash = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen(
            [sys.executable, str(SCRIPT)],
            env={**base_env, "DASHBOARD_URL": cls.dash, "MCP_TOKEN": TOKEN, "MCP_PORT": str(cls.mcp_port),
                 "MCP_ALLOWED_HOSTS": f"127.0.0.1:{cls.mcp_port}"},
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            t0 = time.time()
            while time.time() - t0 < 180:            # healthy and with a first full reading
                try:
                    with urllib.request.urlopen(cls.dash + "/api/live", timeout=5) as r:
                        if (json.loads(r.read()).get("power") or {}).get("load") is not None:
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            wait_for(f"http://127.0.0.1:{cls.mcp_port}/health")
        except Exception:
            cls.tearDownClass()
            raise
        cls.url = f"http://127.0.0.1:{cls.mcp_port}/mcp"

    @classmethod
    def tearDownClass(cls):
        for p in (cls.server, cls.collector):
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        cls.tmp.cleanup()

    def test_session_over_http(self):
        status, init = rpc(self.url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tests", "version": "1"}}})
        self.assertEqual(status, 200)
        info = init["result"]["serverInfo"]
        self.assertEqual(info["name"], "home-energy")
        self.assertEqual(info["version"], load_server().VERSION)
        _, tl = rpc(self.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = tl["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOLS)
        for t in tools:
            self.assertTrue(t["annotations"]["readOnlyHint"], t["name"])
            self.assertFalse(t["annotations"]["destructiveHint"], t["name"])
        _, res = rpc(self.url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "home_energy_now", "arguments": {}}})
        self.assertFalse(res["result"].get("isError"), res)
        self.assertIn("power_kw", json.loads(res["result"]["content"][0]["text"]))

    def test_refusals_over_http(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(rpc(self.url, msg, token=None)[0], 401)
        self.assertEqual(rpc(self.url, msg, token="wrong-token-0123456789")[0], 401)
        self.assertEqual(rpc(self.url, msg, host=f"evil.example:{self.mcp_port}")[0], 421)

    def test_every_tool_reads_the_real_api(self):
        """Catches a dashboard API change that would leave a tool empty or failing."""
        m = load_server()
        m.DASHBOARD_URL = self.dash
        now = call(m, "home_energy_now")
        for k in ("power_kw", "battery", "today_kwh", "price_now", "system"):
            self.assertIn(k, now)
        self.assertIsNotNone(now["power_kw"].get("house"))
        self.assertIsNotNone(now["battery"].get("charge_pct"))
        p = call(m, "electricity_prices", hours=6)
        self.assertIn("buy", p["now"])
        self.assertTrue(p["forecast"])
        self.assertIn("price_c", p["wholesale_nsw"])
        self.assertIn("headline", call(m, "battery_outlook"))
        self.assertIn("now", call(m, "best_time_to_use_power"))
        self.assertIn("cost", call(m, "plan_ev_charging", kwh=10, ready_by="07:00"))
        self.assertIn("charger_now", call(m, "ev_charging", days=7))
        days = call(m, "energy_by_day", days=3)
        self.assertTrue(days and "solar_kwh" in days[-1])
        self.assertTrue(call(m, "energy_by_month", months=1))
        self.assertTrue(call(m, "power_history", hours=2)["points"])
        self.assertTrue(call(m, "weather_and_solar_forecast")["days"])
        self.assertIn("cycles", call(m, "battery_health"))
        sh = call(m, "system_health")
        self.assertIn("running_state", sh["inverter"])
        self.assertTrue(sh["feeds"])
        self.assertTrue(call(m, "money_summary").get("enabled"))
        for name in sorted(m.RAW_ALLOWED):                          # every endpoint it allows exists
            out = call(m, "dashboard_api", endpoint=name)
            err = out.get("error") if isinstance(out, dict) else None
            self.assertFalse(err and "isn't available" in str(err), f"{name}: {err}")


if __name__ == "__main__":
    unittest.main()

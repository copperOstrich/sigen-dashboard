"""Home Energy MCP server: lets Claude (or any MCP client) ask your dashboard about your home energy.

It is a thin, read-only layer over the collector's own HTTP API (/api/live, /api/prices, ...).
It never talks to the inverter itself, so it adds no Modbus load and can't change anything.

Settings (environment variables):
  DASHBOARD_URL      where the collector is, e.g. http://homeenergy.internal or http://sigen-dashboard:8080
  MCP_TOKEN          the shared secret clients must send as "Authorization: Bearer <token>" (required)
  MCP_PORT           port to listen on (default 8000)
  MCP_ALLOWED_HOSTS  comma-separated host names clients may use to reach this server, e.g.
                     "homeenergy.internal:8000,192.168.1.51:8000" (default: any; the token still applies)
  TZ                 your time zone, for readable times (default Australia/Sydney)
"""
import asyncio
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# The same version as the dashboard (collector/config.py) and both Docker images; a test checks
# they match, so bump them together.
VERSION = "2.4.0"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://sigen-dashboard:8080").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN", "")
PORT = int(os.environ.get("MCP_PORT", "8000"))
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
TZ = ZoneInfo(os.environ.get("TZ", "Australia/Sydney"))
TIMEOUT = float(os.environ.get("DASHBOARD_TIMEOUT", "15"))

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# The collector's read-only endpoints that dashboard_api may pass through. Settings, backups,
# debug dumps, the live stream and anything that changes state are deliberately left out.
RAW_ALLOWED = {
    "live", "history", "daily", "monthly", "prices", "aemo", "outlook", "weather", "money", "trading",
    "battery-wear", "strings-health", "string-history", "limits", "ev", "ev-sessions", "ev-plan",
    "goodtime", "appliances", "baseload", "targets", "trends", "whatif", "bills", "health",
}

server = MCPServer(
    name="home-energy",
    title="Home Energy",
    version=VERSION,
    instructions=(
        "Live and historical data from the user's home solar, SigenStor battery, EV charger and "
        "Amber Electric prices, read from their own dashboard. Power is in watts unless a field says kW; "
        "energy in kWh; prices in cents per kWh; money in dollars. Times are local. "
        "Start with home_energy_now for 'how's the house going' questions, battery_outlook for "
        "'will the battery last', best_time_to_use_power for 'should I run X now', and "
        "plan_ev_charging for car charging. Everything here is read-only."
    ),
)


# ---------------------------------------------------------------- helpers
def _get(path, **params):
    q = {k: v for k, v in params.items() if v is not None}
    url = f"{DASHBOARD_URL}/api/{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"home-energy-mcp/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"The dashboard returned an error for /api/{path}: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Can't reach the dashboard at {DASHBOARD_URL} ({getattr(e, 'reason', e)}). "
                           "Is the sigen-dashboard container running?") from None


async def api(path, **params):
    return await asyncio.to_thread(_get, path, **params)


def when(ts):
    """A readable local time: '18:05 today', '05:00 tomorrow', '14:30 Wed 18 Nov'."""
    if not ts:
        return None
    t = datetime.fromtimestamp(float(ts), TZ)
    today = datetime.now(TZ).date()
    if t.date() == today:
        return t.strftime("%H:%M today")
    if t.date() == today + timedelta(days=1):
        return t.strftime("%H:%M tomorrow")
    if t.date() == today - timedelta(days=1):
        return t.strftime("%H:%M yesterday")
    return t.strftime("%H:%M %a %d %b")


def kw(w):
    return None if w is None else round(w / 1000, 2)


def r(v, n=2):
    return None if v is None else round(v, n)


def drop_none(d):
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


# ---------------------------------------------------------------- tools
@server.tool(annotations=READ_ONLY)
async def home_energy_now() -> dict:
    """Right now: solar, house, battery, grid and car power; battery charge; today's totals and cost;
    the current electricity price; and whether the system is healthy. The best first call."""
    s = await api("live")
    p, b, t, sysi = s.get("power") or {}, s.get("battery") or {}, s.get("today") or {}, s.get("system") or {}
    batt_w, grid_w = p.get("battery") or 0, p.get("grid") or 0
    price = (s.get("price") or {})
    ev = s.get("ev") or {}
    return drop_none({
        "as_of": when(s.get("updated")),
        "status": s.get("status"),
        "error": s.get("error"),
        "power_kw": {
            "solar": kw(p.get("pv")),
            "house": kw(p.get("load")),
            "car": kw(p.get("ev")),
            "battery": kw(abs(batt_w)),
            "battery_direction": "charging" if batt_w > 50 else "discharging" if batt_w < -50 else "idle",
            "grid": kw(abs(grid_w)),
            "grid_direction": "importing" if grid_w > 50 else "exporting" if grid_w < -50 else "idle",
        },
        "battery": drop_none({
            "charge_pct": b.get("soc"),
            "stored_kwh": b.get("available_kwh"),
            "room_to_fill_kwh": b.get("chargeable_kwh"),
            "capacity_kwh": b.get("rated_kwh"),
            "health_pct": b.get("soh"),
            "reserve_pct": b.get("cutoff_low"),
            "temp_c": b.get("cell_temp_avg"),
        }),
        "today_kwh": drop_none({k2: t.get(k1) for k1, k2 in (
            ("pv", "solar"), ("load", "house"), ("ev", "car"), ("import", "bought"), ("export", "sold"),
            ("charge", "battery_charged"), ("discharge", "battery_discharged"))}),
        "today_self_sufficiency_pct": t.get("self_sufficiency"),
        "today_cost": t.get("cost"),
        "price_now": drop_none({
            "buy_c_kwh": price.get("buy"), "sell_c_kwh": price.get("sell"), "level": price.get("descriptor"),
            "spike": None if price.get("spike") in (None, "none") else price.get("spike"),
            "until": when(price.get("ends")),
        }) if price.get("enabled") else None,
        "battery_outlook": (s.get("outlook") or {}).get("headline"),
        "car": drop_none({"state": ev.get("state_label"), "plugged_in": ev.get("plugged"),
                          "charging": ev.get("charging"), "power_kw": kw(ev.get("power"))}) if ev.get("enabled") else None,
        "system": drop_none({
            "state": sysi.get("running_state"),
            "grid": sysi.get("grid_status"),
            "battery_control": sysi.get("ems_mode"),
            "inverter_error": sysi.get("inverter_error"),
            "alarms": s.get("alarms"),
            "feeds_with_problems": [f"{f['name']}: {f.get('error') or f.get('status')}" for f in sysi.get("feeds") or []
                                    if f.get("status") not in ("ok", "first one soon", None)],
        }),
    })


@server.tool(annotations=READ_ONLY)
async def electricity_prices(hours: int = 12) -> dict:
    """Amber buy and feed-in prices now, the forecast for the next few hours (1-24), any price spike,
    and the NSW wholesale price. Prices are cents per kWh."""
    hours = max(1, min(24, int(hours)))
    p = await api("prices", hours=1)
    cur = p.get("current") or {}
    g, f = cur.get("general") or {}, cur.get("feedIn") or {}
    end = datetime.now(TZ).timestamp() + hours * 3600
    forecast = [drop_none({"from": when(x.get("t")), "buy": x.get("buy"), "sell": x.get("sell"),
                           "level": x.get("descriptor"), "spike": None if x.get("spike") in (None, "none") else x.get("spike")})
                for x in p.get("forecast") or [] if (x.get("t") or 0) < end]
    out = {
        "retailer": p.get("provider"),
        "status": p.get("status"),
        "now": drop_none({"buy": g.get("per_kwh"), "sell": -f["per_kwh"] if f.get("per_kwh") is not None else None,
                          "level": g.get("descriptor"),
                          "spike": None if g.get("spike") in (None, "none") else g.get("spike"),
                          "renewables_pct": g.get("renewables"), "estimate": g.get("estimate")}),
        "forecast": forecast,
    }
    try:
        a = await api("aemo", hours=1)
        if a.get("enabled"):
            c = a.get("current") or {}
            out["wholesale_nsw"] = drop_none({"price_c": c.get("price_c"), "demand_mw": c.get("demand_mw"),
                                              "renewables_pct": a.get("renewables_pct")})
    except RuntimeError:
        pass
    return drop_none(out)


@server.tool(annotations=READ_ONLY)
async def battery_outlook() -> dict:
    """Will the battery last? When it will hit its reserve or be full, how much grid power tonight
    will need and cost, and what tomorrow's solar looks like."""
    o = await api("outlook")
    if not o.get("enabled"):
        return {"enabled": False, "reason": o.get("reason")}
    t, ch = o.get("tomorrow") or {}, o.get("cheapest") or {}
    return drop_none({
        "headline": o.get("headline"),
        "advice": o.get("advice"),
        "reaches_reserve": when(o.get("empty_at")),
        "full_at": when(o.get("full_at")),
        "solar_takes_over": when(o.get("solar_covers_at")),
        "lowest_charge": drop_none({"pct": (o.get("lowest") or {}).get("soc"), "at": when((o.get("lowest") or {}).get("t"))}),
        "tonight_grid_kwh": o.get("night_grid_kwh"),
        "tonight_cost": o.get("night_cost"),
        "cheapest_grid_window": drop_none({"from": when(ch.get("t")), "to": when(ch.get("end")), "c_kwh": ch.get("price")}),
        "tomorrow": drop_none({"solar_kwh": t.get("pv_kwh"), "house_kwh": t.get("home_kwh"), "max_charge_pct": t.get("max_soc"),
                               "full_at": when(t.get("full_at")), "spare_solar_kwh": t.get("spare_kwh")}),
        "projected_cost": o.get("cost"),
        "battery_managed_by": o.get("ems_mode"),
    })


@server.tool(annotations=READ_ONLY)
async def best_time_to_use_power() -> dict:
    """Is now a good time to use power (run the dishwasher, pool pump, etc.)? The current level and
    why, when the next good window is, any coming price spike, and the best start time for each
    appliance set up in the dashboard."""
    g = await api("goodtime")
    if not g.get("enabled"):
        return {"enabled": False}
    nxt, spike, apps = g.get("next_good") or {}, g.get("next_spike") or {}, g.get("appliances") or {}
    return drop_none({
        "now": g.get("label"),
        "cost_now_c_kwh": g.get("c_kwh"),
        "power_comes_from": g.get("source"),
        "why": g.get("why"),
        "good_until": when(g.get("good_until")),
        "next_good_window": drop_none({"from": when(nxt.get("start")), "to": when(nxt.get("end")),
                                       "level": nxt.get("level"), "c_kwh": nxt.get("c_kwh")}),
        "next_spike": drop_none({"from": when(spike.get("start")), "to": when(spike.get("end")),
                                 "c_kwh": spike.get("c_kwh") or spike.get("peak_c")}) if spike else None,
        "appliances": [drop_none({
            "name": a.get("name"), "runs_hours": a.get("hours"), "kwh": a.get("kwh"),
            "best_start": when((a.get("best") or {}).get("start")),
            "best_cost": (a.get("best") or {}).get("cost"),
            "best_because": (a.get("best") or {}).get("why"),
            "cost_if_started_now": (a.get("now") or {}).get("cost"),
            "run_now": a.get("run_now"),
        }) for a in apps.get("appliances") or []],
    })


@server.tool(annotations=READ_ONLY)
async def plan_ev_charging(kwh: float = 20, ready_by: str = "07:00", charger_kw: float | None = None) -> dict:
    """The cheapest way to put `kwh` into the car by `ready_by` (24-hour HH:MM, local), using the price
    forecast and expected spare solar. charger_kw defaults to the charger's rated power."""
    params = {"kwh": kwh, "by": ready_by}
    if charger_kw:
        params["kw"] = charger_kw
    p = await api("ev-plan", **params)
    if p.get("error"):
        return {"error": p["error"]}
    return drop_none({
        "wanted_kwh": p.get("wanted_kwh"),
        "ready_by": when(p.get("by")),
        "charger_kw": p.get("kw"),
        "charge_in_windows": [{"from": when(w.get("start")), "to": when(w.get("end")), "kwh": w.get("kwh")}
                              for w in p.get("windows") or []],
        "cost": p.get("cost"),
        "avg_c_kwh": p.get("avg_c"),
        "from_solar_kwh": p.get("solar_kwh"),
        "cost_if_charged_now": p.get("now_cost"),
        "saving": p.get("saving"),
        "shortfall_kwh": p.get("short_kwh") or None,
    })


@server.tool(annotations=READ_ONLY)
async def ev_charging(days: int = 7) -> dict:
    """The EV charger right now (plugged in? charging?) and recent charging sessions with energy,
    grid share and cost."""
    days = max(1, min(90, int(days)))
    e = await api("ev-sessions", days=days)
    live = e.get("live") or {}
    sessions = []
    for x in e.get("sessions") or []:
        if not x.get("kwh") and x.get("end"):
            continue
        sessions.append(drop_none({
            "plugged_in": when(x.get("start")), "unplugged": when(x.get("end")) or "still plugged in",
            "kwh": r(x.get("kwh")), "from_grid_kwh": r(x.get("grid_kwh")), "cost": r(x.get("cost")),
            "peak_kw": kw(x.get("peak_w")), "hours_charging": r((x.get("charging_s") or 0) / 3600, 1),
        }))
    total = lambda k: r(sum((x.get(k) or 0) for x in e.get("sessions") or []))
    return drop_none({
        "charger_now": drop_none({"state": live.get("state_label"), "plugged_in": live.get("plugged"),
                                  "charging": live.get("charging"), "power_kw": kw(live.get("power")),
                                  "since": when(live.get("since")), "error": live.get("error")}),
        f"last_{days}_days": {"sessions": len(sessions), "kwh": total("kwh"), "from_grid_kwh": total("grid_kwh"),
                              "cost": total("cost")},
        "sessions": sessions,
    })


@server.tool(annotations=READ_ONLY)
async def energy_by_day(days: int = 7) -> list:
    """Daily totals for the last `days` days (1-90): solar, house, car, bought, sold, battery in/out,
    lowest and highest battery charge, and what the day cost."""
    rows = await api("daily", days=max(1, min(90, int(days))))
    return [drop_none({
        "day": d.get("day"), "solar_kwh": d.get("pv"), "house_kwh": d.get("load"), "car_kwh": d.get("ev"),
        "bought_kwh": d.get("import"), "sold_kwh": d.get("export"),
        "battery_charged_kwh": d.get("charge"), "battery_discharged_kwh": d.get("discharge"),
        "battery_min_pct": d.get("soc_min"), "battery_max_pct": d.get("soc_max"),
        "bought": d.get("bought"), "earned": d.get("earned"), "net_cost": d.get("cost"),
    }) for d in rows]


@server.tool(annotations=READ_ONLY)
async def energy_by_month(months: int = 3) -> list:
    """Monthly totals for the last `months` months (1-24): energy, self-sufficiency, money bought,
    earned and saved, average buy price, and car charging."""
    return await api("monthly", months=max(1, min(24, int(months))))


@server.tool(annotations=READ_ONLY)
async def power_history(hours: float = 6) -> dict:
    """How power flowed over the last `hours` hours (1-72), averaged into up to 36 steps: solar, house,
    car, battery (+ charging / - discharging), grid (+ importing / - exporting) in kW, and battery %."""
    hours = max(1, min(72, float(hours)))
    h = await api("history", hours=hours)
    ts = h.get("t") or []
    if not ts:
        return {"points": []}
    n = len(ts)
    step = max(1, -(-n // 36))
    points = []
    for i in range(0, n, step):
        sl = slice(i, i + step)

        def avg(key, conv=kw):
            vals = [v for v in (h.get(key) or [])[sl] if v is not None]
            return conv(sum(vals) / len(vals)) if vals else None
        points.append(drop_none({"from": when(ts[i]), "solar": avg("pv"), "house": avg("load"), "car": avg("ev"),
                                 "battery": avg("battery"), "grid": avg("grid"),
                                 "battery_pct": avg("soc", lambda v: round(v, 1))}))
    return {"step_minutes": round(step * (h.get("bucket_seconds") or 60) / 60), "points": points}


@server.tool(annotations=READ_ONLY)
async def weather_and_solar_forecast() -> dict:
    """Current weather and the next few days' forecast, including expected solar generation per day."""
    w = await api("weather")
    if not w.get("enabled"):
        return {"enabled": False, "status": w.get("status")}
    c = w.get("current") or {}
    return drop_none({
        "now": drop_none({"desc": c.get("desc"), "temp_c": c.get("temp"), "cloud_pct": c.get("cloud"),
                          "wind_kmh": c.get("wind"), "humidity_pct": c.get("humidity")}),
        "days": [drop_none({"day": datetime.fromtimestamp(d["t"], TZ).strftime("%a %d %b") if d.get("t") else None,
                            "desc": d.get("desc"), "min_c": d.get("tmin"), "max_c": d.get("tmax"),
                            "rain_pct": d.get("rain_prob"), "rain_mm": d.get("rain_mm"), "uv": d.get("uv"),
                            "solar_kwh_expected": d.get("pv_kwh"),
                            "sunrise": when(d.get("sunrise")), "sunset": when(d.get("sunset"))})
                 for d in w.get("daily") or []],
    })


@server.tool(annotations=READ_ONLY)
async def battery_health() -> dict:
    """Battery wear: state of health, cycles so far and per day, round-trip efficiency, temperatures,
    and how it's tracking against the warranty."""
    return await api("battery-wear")


@server.tool(annotations=READ_ONLY)
async def system_health() -> dict:
    """Is everything working? Inverter state, alarms and errors, insulation, grid voltage and
    frequency, each data feed's status, and whether any solar string is under-performing."""
    s = await api("live")
    sysi = s.get("system") or {}
    out = {
        "collector": drop_none({"status": s.get("status"), "error": s.get("error"), "as_of": when(s.get("updated")),
                                "version": s.get("version"), "reading_from": s.get("mode")}),
        "inverter": drop_none({k: sysi.get(k) for k in (
            "running_state", "grid_status", "grid_meter", "ems_mode", "inverter_error", "model", "firmware",
            "grid_voltage", "grid_frequency", "pcs_temp", "insulation_mohm", "insulation_warn_mohm")}),
        "remote_limits": drop_none(sysi.get("control") or {}) or None,
        "alarms": s.get("alarms"),
        "feeds": [drop_none({"name": f.get("name"), "status": f.get("status"), "error": f.get("error"),
                             "updated": when(f.get("updated"))}) for f in sysi.get("feeds") or []],
    }
    try:
        sh = await api("strings-health")
        if sh.get("enabled"):
            out["solar_strings"] = [drop_none({"string": f"PV{x.get('n')}", "status": x.get("status"),
                                               "vs_normal": x.get("ratio")}) for x in sh.get("strings") or []]
    except RuntimeError:
        pass
    return drop_none(out)


@server.tool(annotations=READ_ONLY)
async def money_summary() -> dict:
    """Money: what the battery's exports and imports have been worth this month and last, price spikes
    caught or missed, retailer plan comparisons and payback, where the dashboard has them."""
    m = await api("money")
    try:
        t = await api("trading", days=30)
        if t.get("enabled"):
            m["spikes_last_30_days"] = drop_none({k: t.get(k) for k in (
                "spike_count", "caught", "spike_earned", "spike_missed_value")})
    except RuntimeError:
        pass
    trades = m.get("trades") or {}
    trades.pop("events", None)
    return drop_none(m)


@server.tool(annotations=READ_ONLY)
async def dashboard_api(endpoint: str, query: str = "") -> dict | list:
    """Read any other part of the dashboard's API directly, for questions the other tools don't cover.
    `endpoint` is the name after /api/ (for example 'limits', 'baseload', 'trends', 'aemo', 'bills',
    'whatif', 'appliances', 'string-history'); `query` is an optional URL query such as 'days=30'.
    Read-only: settings, backups and anything that changes things are not available."""
    name = endpoint.strip().strip("/").removeprefix("api/")
    if name not in RAW_ALLOWED:
        return {"error": f"'{name}' isn't available here. Available: {', '.join(sorted(RAW_ALLOWED))}"}
    params = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    params.pop("format", None)              # JSON only
    return await api(name, **params)


# ---------------------------------------------------------------- HTTP app with a token check
class TokenGate:
    """Plain ASGI middleware: every request needs 'Authorization: Bearer <MCP_TOKEN>', except /health."""

    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            got = b""
            for k, v in scope.get("headers") or []:
                if k == b"authorization" and v[:7].lower() == b"bearer ":
                    got = v[7:].strip()
            if not hmac.compare_digest(got, self.token):
                body = b'{"error":"missing or wrong token"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        if scope["type"] == "http" and scope.get("path") == "/health":
            # no token needed here, so it says nothing about the network (not even the dashboard's address)
            body = json.dumps({"ok": True, "version": VERSION}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_app():
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=ALLOWED_HOSTS,
                                          allowed_origins=[f"http://{h}" for h in ALLOWED_HOSTS] +
                                                          [f"https://{h}" for h in ALLOWED_HOSTS])
                if ALLOWED_HOSTS else TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app = server.streamable_http_app(streamable_http_path="/mcp", transport_security=security,
                                     stateless_http=True, json_response=True, host="0.0.0.0")
    return TokenGate(app, TOKEN)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":   # for running on a PC next to Claude Desktop
        server.run("stdio")
        return
    if len(TOKEN) < 16:
        sys.exit("Set MCP_TOKEN to a long random secret (16+ characters) before starting the server.")
    import uvicorn
    print(f"Home Energy MCP v{VERSION} on http://0.0.0.0:{PORT}/mcp, reading {DASHBOARD_URL}", flush=True)
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()

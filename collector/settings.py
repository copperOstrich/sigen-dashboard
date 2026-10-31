"""The Settings page: what each setting is, reading and writing sigen.env, and who may change it.

Settings are read once at start-up (sigen_collector.py), so the page saves to sigen.env and the
collector restarts itself. Changes are only accepted from the collector's own network (or the
addresses in SETTINGS_EDIT_FROM); from anywhere else the page is read-only and sensitive values
(keys, addresses, location) are never sent.
"""
import ipaddress
import os
import re
import socket
import struct
import time
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PLANS_DIR = Path(__file__).resolve().parent / "plans"


def _s(key, group, label, help, kind="text", default="", **kw):
    return {"key": key, "group": group, "label": label, "help": help, "kind": kind, "default": default, **kw}


INV, PRICES, SITE, MONEY, CAR, CHECKS, COLLECTOR = (
    "Inverter", "Prices", "Weather and solar forecast", "Money", "Car and fuel", "Checks", "Collector")

ON_OFF = [["1", "On"], ["0", "Off"]]

SCHEMA = [
    # ---------------------------------------------------------------- inverter
    _s("SIGEN_SIMULATE", INV, "Simulator", "Run the built-in simulator instead of reading an inverter.",
       "choice", "0", options=[["0", "Off (read the inverter)"], ["1", "On (simulated data)"]]),
    _s("SIGEN_HOST", INV, "Inverter address", "The inverter's fixed IP address on your network.",
       "host", "", sensitive=True, placeholder="192.168.1.60"),
    _s("SIGEN_PORT", INV, "Modbus TCP port", "Almost always 502.", "int", "502", min=1, max=65535),
    _s("SIGEN_PLANT_UNIT", INV, "Plant unit id", "Modbus unit id of the plant (the whole system).",
       "int", "247", min=0, max=255),
    _s("SIGEN_INVERTER_UNIT", INV, "Inverter unit id", "Modbus unit id of the inverter; 0 skips battery cell "
       "and solar string detail.", "int", "1", min=0, max=255),
    _s("SIGEN_CHARGER_UNIT", INV, "AC charger", "auto searches for it, a number reads that Modbus unit, plant "
       "works it out from the inverter's figures (charger with no Modbus address), 0 = no charger.",
       "charger", "auto", placeholder="auto, plant, 0 or a unit id"),
    _s("POLL_SECONDS", INV, "Read every", "How often to read the inverter (2 seconds minimum).",
       "number", "5", min=2, max=300, unit="seconds"),
    # ---------------------------------------------------------------- prices
    _s("PRICE_PROVIDER", PRICES, "Price source", "Which retailer's prices to use, if keys for both are set.",
       "choice", "", options=[["", "Whichever has a key"], ["amber", "Amber Electric"],
                              ["localvolts", "LocalVolts"], ["none", "None"]]),
    _s("AMBER_API_KEY", PRICES, "Amber API key", "From app.amber.com.au/developers.", "secret"),
    _s("AMBER_SITE_ID", PRICES, "Amber site id", "Only if your Amber account has more than one site.",
       "text", "", sensitive=True, placeholder="first active site"),
    _s("LOCALVOLTS_API_KEY", PRICES, "LocalVolts API key", "localvolts.com, My Profile, API Key.", "secret"),
    _s("LOCALVOLTS_PARTNER_ID", PRICES, "LocalVolts Partner ID", "Same page as the API key.", "secret"),
    _s("LOCALVOLTS_NMI", PRICES, "LocalVolts NMI", "Only if the account has more than one meter.",
       "text", "", sensitive=True, placeholder="first on the account"),
    _s("AEMO_ENABLED", PRICES, "AEMO market data", "Wholesale prices, demand and the NEM map.",
       "choice", "1", options=ON_OFF),
    _s("AEMO_REGION", PRICES, "AEMO region", "Your region of the National Electricity Market.",
       "choice", "NSW1", options=[[r, r] for r in ("NSW1", "QLD1", "VIC1", "SA1", "TAS1")]),
    # ---------------------------------------------------------------- weather and solar
    _s("WEATHER_ENABLED", SITE, "Weather", "Weather and the solar forecast (Open-Meteo, free).",
       "choice", "1", options=ON_OFF),
    _s("SITE_LAT", SITE, "Latitude", "Where the panels are (in Google Maps, right-click your roof).",
       "number", "", sensitive=True, min=-90, max=90, placeholder="-33.87"),
    _s("SITE_LON", SITE, "Longitude", "", "number", "", sensitive=True, min=-180, max=180, placeholder="151.21"),
    _s("SITE_ADDRESS", SITE, "Or an address", "An address or suburb to look up once, instead of latitude "
       "and longitude.", "text", "", sensitive=True),
    _s("SOLAR_KWP", SITE, "Array size", "Total panel size, for a first forecast before it learns your system.",
       "number", "", min=0, max=1000, unit="kW"),
    _s("SOLAR_PLANES", SITE, "Roof faces", "Each face as tilt:azimuth:kW, comma separated; azimuth 0 = north, "
       "90 = east, 270 = west.", "planes", "", placeholder="22:90:6.6,22:270:6.6"),
    # ---------------------------------------------------------------- money
    _s("COMPARE_PLANS", MONEY, "Compare with", "The Default Market Offer for your network (or put plans.json "
       "in the data folder).", "plans", ""),
    _s("RETAILER_SUPPLY_C_DAY", MONEY, "Supply charge", "Your retailer's daily supply charge, from your bill.",
       "number", "", min=0, max=1000, unit="c/day"),
    _s("RETAILER_FEE_MONTHLY", MONEY, "Monthly fee", "Any membership fee, e.g. Amber's.", "number", "",
       min=0, max=1000, unit="$/month"),
    _s("RETAILER_SINCE", MONEY, "With this retailer since", "Earlier days count towards payback but not "
       "the comparison.", "date", ""),
    _s("SYSTEM_COST", MONEY, "System cost", "What the solar and battery cost, for payback.", "number", "",
       min=0, max=1e7, unit="$"),
    _s("SYSTEM_INSTALLED", MONEY, "Switched on", "The day the system went in.", "date", ""),
    # ---------------------------------------------------------------- car and fuel
    _s("EV_MODEL", CAR, "Your EV", "", "text", "", placeholder="Tesla Model Y"),
    _s("EV_KWH_PER_100KM", CAR, "EV uses", "Energy use, for the fuel comparison.", "number", "",
       min=1, max=100, unit="kWh/100 km"),
    _s("COMPARE_VEHICLE", CAR, "Fuel car to compare", "", "text", "", placeholder="Toyota RAV4"),
    _s("COMPARE_L_PER_100KM", CAR, "Fuel car uses", "", "number", "", min=1, max=50, unit="L/100 km"),
    _s("FUEL_TYPE", CAR, "Fuel", "", "choice", "U91",
       options=[[f, f] for f in ("E10", "U91", "U95", "U98", "Diesel")]),
    _s("FUEL_RADIUS_KM", CAR, "Servos within", "Stations this far from home are averaged.", "number", "10",
       min=1, max=100, unit="km"),
    _s("FUEL_EXCLUDE_BRANDS", CAR, "Leave out brands", "Comma separated; clear it to include every brand.",
       "text", "Costco"),
    _s("FUEL_PRICE_C_L", CAR, "Fixed fuel price", "Used when there's no live price source.", "number", "",
       min=0, max=1000, unit="c/L"),
    _s("FUELCHECK_API_KEY", CAR, "NSW FuelCheck key", "Free from api.nsw.gov.au.", "secret"),
    _s("FUELCHECK_API_SECRET", CAR, "NSW FuelCheck secret", "", "secret"),
    _s("FUELWATCH_SUBURB", CAR, "WA FuelWatch suburb", "For live prices in WA instead of FuelCheck.",
       "text", "", sensitive=True),
    # ---------------------------------------------------------------- checks
    _s("INSULATION_WARN_MOHM", CHECKS, "Insulation warning below", "Healthy is about 0.5 to 0.8.", "number",
       "0.5", min=0.01, max=100, unit="MΩ"),
    _s("INSULATION_MIN_MOHM", CHECKS, "Insulation alarm below", "About where the inverter shuts down.",
       "number", "0.37", min=0.01, max=100, unit="MΩ"),
    # ---------------------------------------------------------------- collector
    _s("TZ", COLLECTOR, "Time zone", "For when \"today\" starts.", "zone", "Australia/Sydney"),
    _s("RETENTION_DAYS", COLLECTOR, "Keep history for", "Days of minute-by-minute history.", "int", "800",
       min=30, max=10000, unit="days"),
    _s("SETTINGS_EDIT_FROM", COLLECTOR, "Settings can be changed from",
       "Blank = devices on the collector's own network. Or addresses and ranges, comma separated "
       "(192.168.1.0/24), or none to make this page read-only everywhere.", "cidrs", "",
       placeholder="the collector's own network"),
    _s("HTTP_PORT", COLLECTOR, "Dashboard port", "Change in sigen.env or the container, so the dashboard "
       "can't be moved somewhere you can't reach.", "int", "8080", readonly=True),
    _s("DATA_DIR", COLLECTOR, "Data folder", "Set by the container.", "text", "", readonly=True),
]
BY_KEY = {s["key"]: s for s in SCHEMA}
# never offered on the page (set by the container or for development)
HIDDEN = {"SETTINGS_FILE", "WEB_DIR", "PATH", "HOME", "HOSTNAME", "LANG", "PYTHONUNBUFFERED"}


def plan_options():
    names = sorted(p.stem for p in PLANS_DIR.glob("*.json"))
    return [["", "No comparison"]] + [[n, n] for n in names]


# ---------------------------------------------------------------- the file
def parse_line(line):
    """(key, value) for a KEY=value line, or None. The same rules sigen_collector uses on start."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if key.startswith("export "):
        key = key[7:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def read_file(path):
    out = {}
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            kv = parse_line(line)
            if kv:
                out[kv[0]] = kv[1]
    return out


def _fmt(value):
    if value == "":
        return ""
    if value != value.strip() or " #" in value or value[0] in "\"'" or "\t#" in value:
        return "'" + value + "'" if '"' in value else '"' + value + '"'
    return value


def write_file(path, changes):
    """Set each KEY in changes, keeping comments and order; new keys go at the end. Backs up first."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    todo = dict(changes)
    out = []
    for line in lines:
        kv = parse_line(line)
        if kv and kv[0] in changes:
            if kv[0] in todo:
                raw = line.split("=", 1)[1].strip()
                note = ""  # keep a trailing "  # comment" on the line
                if raw[:1] not in ("'", '"') and " #" in raw:
                    v = raw.split(" #", 1)[0]
                    note = raw[len(v.rstrip()):]
                out.append(f"{kv[0]}={_fmt(todo.pop(kv[0]))}{note}")
            # a key repeated further down: drop it, or it would win over the new value
            continue
        out.append(line)
    if todo:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added from the dashboard's Settings page")
        out += [f"{k}={_fmt(v)}" for k, v in todo.items()]
    text = "\n".join(out) + "\n"
    if path.is_file():
        bak = path.with_name(path.name + ".bak")
        bak.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # sigen.env mounted into the container on its own can't be replaced, only rewritten
        tmp.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- checking values
def validate(key, value, all_values=None):
    """Clean value, or raises ValueError with a message for the page."""
    s = BY_KEY.get(key)
    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise ValueError("Must be on one line")
    if '"' in value and "'" in value:
        raise ValueError("Can't contain both kinds of quote mark")
    if not s or value == "":
        return value
    k = s["kind"]

    def num(v, integer=False):
        try:
            n = int(v) if integer else float(v)
        except ValueError:
            raise ValueError("Must be a whole number" if integer else "Must be a number")
        if "min" in s and n < s["min"]:
            raise ValueError(f"Must be at least {s['min']:g}")
        if "max" in s and n > s["max"]:
            raise ValueError(f"Must be at most {s['max']:g}")
        return n

    if k == "int":
        num(value, True)
    elif k == "number":
        num(value)
    elif k == "choice":
        if value not in [o[0] for o in s["options"]]:
            raise ValueError("Pick one of the options")
    elif k == "plans":
        if value.lower() not in [o[0] for o in plan_options()]:
            raise ValueError("Pick one of the options")
        value = value.lower()
    elif k == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("Must be a date, YYYY-MM-DD")
    elif k == "host":
        if not re.fullmatch(r"[A-Za-z0-9.\-:]+", value):
            raise ValueError("Must be an IP address or host name")
    elif k == "charger":
        if value.lower() in ("auto", "plant"):
            value = value.lower()
        else:
            try:
                n = int(value)
            except ValueError:
                raise ValueError("auto, plant, 0 or a unit id")
            if not 0 <= n <= 247:
                raise ValueError("A unit id is 0 to 247")
    elif k == "planes":
        for p in value.split(","):
            parts = p.strip().split(":")
            try:
                t, a, kw = (float(x) for x in parts)
            except ValueError:
                raise ValueError(f"'{p.strip()}' isn't tilt:azimuth:kW")
            if not (0 <= t <= 90 and -360 <= a <= 360 and 0 < kw <= 1000):
                raise ValueError(f"'{p.strip()}': tilt 0-90, azimuth 0-360, kW above 0")
    elif k == "zone":
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("Not a time zone name, e.g. Australia/Sydney")
    elif k == "cidrs":
        if value.lower() == "none":
            return "none"
        for c in value.split(","):
            try:
                ipaddress.ip_network(c.strip(), strict=False)
            except ValueError:
                raise ValueError(f"'{c.strip()}' isn't an address or range")
    return value


def check_together(values):
    """Errors that need more than one setting. values = what the settings will be after saving."""
    errs = {}
    if values.get("SIGEN_SIMULATE", "0") != "1" and not values.get("SIGEN_HOST"):
        errs["SIGEN_HOST"] = "Needed unless the simulator is on"
    try:
        w = float(values.get("INSULATION_WARN_MOHM") or 0.5)
        m = float(values.get("INSULATION_MIN_MOHM") or 0.37)
        if w < m:
            errs["INSULATION_WARN_MOHM"] = "Should be at or above the alarm level"
    except ValueError:
        pass
    if bool(values.get("SITE_LAT")) != bool(values.get("SITE_LON")):
        errs["SITE_LON" if values.get("SITE_LAT") else "SITE_LAT"] = "Set both latitude and longitude"
    return errs


# ---------------------------------------------------------------- who may change settings
_nets = {"at": 0, "nets": None}


def connected_networks():
    """The networks this machine (or container) sits on directly, from the routing table (Linux)."""
    if time.time() - _nets["at"] < 60:
        return _nets["nets"]
    nets = None
    try:
        with open("/proc/net/route") as f:
            rows = f.read().splitlines()[1:]
        nets = []
        for row in rows:
            c = row.split()
            if len(c) < 8 or int(c[2], 16) != 0:  # only routes without a gateway: the local networks
                continue
            dest = socket.inet_ntoa(struct.pack("<L", int(c[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<L", int(c[7], 16)))
            if mask == "0.0.0.0":
                continue
            nets.append(ipaddress.ip_network(f"{dest}/{mask}", strict=False))
    except (OSError, ValueError):
        nets = None
    _nets.update(at=time.time(), nets=nets)
    return nets


def access(client, rule):
    """(may_edit, why) for a request from address `client`. Forwarded-for headers are never trusted."""
    try:
        ip = ipaddress.ip_address(client)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError:
        return False, "your address couldn't be read"
    rule = (rule or "").strip()
    if rule.lower() in ("none", "off"):
        return False, "changing settings from the dashboard is turned off (Settings can be changed from = none)"
    if rule:
        ok = any(ip in ipaddress.ip_network(c.strip(), strict=False) for c in rule.split(",") if c.strip())
        return ok, "" if ok else f"your address ({ip}) isn't in the addresses allowed to change settings"
    if ip.is_loopback:
        return True, ""
    nets = connected_networks()
    if nets is None:  # not Linux: fall back to private addresses
        ok = ip.is_private or ip.is_link_local
    else:
        ok = any(ip in n for n in nets if n.version == ip.version)
    return ok, "" if ok else f"you're not on the collector's home network (your address is {ip})"


# ---------------------------------------------------------------- what the page gets
def describe(file_path, file_values, container_env, live_env, editable):
    """Every setting with its current value and where it came from; masked unless `editable`."""
    def entry(s, key):
        if key in file_values:
            value, source = file_values[key], "file"
        elif container_env.get(key) not in (None, ""):
            value, source = container_env[key], "container"
        else:
            value, source = (live_env.get(key) or s.get("default", "")), "default"
        e = {k: v for k, v in s.items() if k != "group"}
        if s["kind"] == "plans":
            e["kind"], e["options"] = "choice", plan_options()
        e.update(value=value, source=source)
        if s["kind"] == "secret":
            e["set"] = bool(value)
            e["hint"] = ("•••• " + value[-4:] if len(value) >= 8 else "••••") if (value and editable) else ""
            e["value"] = None
        elif s.get("sensitive") and not editable:
            e["set"], e["value"], e["masked"] = bool(value), None, True
        return e

    groups = []
    for s in SCHEMA:
        if not groups or groups[-1]["name"] != s["group"]:
            groups.append({"name": s["group"], "settings": []})
        groups[-1]["settings"].append(entry(s, s["key"]))
    other = [{"key": k, "value": v if editable else None, "set": bool(v), "masked": not editable}
             for k, v in file_values.items() if k not in BY_KEY and k not in HIDDEN]
    return {"editable": editable, "file": str(file_path) if editable else Path(file_path).name,
            "groups": groups, "other": other}

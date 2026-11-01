"""Settings: sigen.env read into the environment, then every setting as a constant.

Imported first by everything else (so sigen.env is applied before any module reads the
environment). The other modules use `from config import *`, so the names stay the same as when
this all lived in sigen_collector.py.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings as settings_page  # noqa: E402,F401  (standard library only, no settings of its own)

CONTAINER_ENV = dict(os.environ)  # before sigen.env is applied: what the container itself sets


def settings_file_path():
    data_dir = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    return Path(os.environ.get("SETTINGS_FILE") or data_dir / "sigen.env")


def load_settings_file():
    """Read KEY=value lines from sigen.env in the data folder (or SETTINGS_FILE).

    Read on every start, so on a NAS you can edit the file and just restart the
    container. Values in the file override the container's environment.
    """
    path = settings_file_path()
    if not path.is_file():
        return None
    for key, value in settings_page.read_file(path).items():
        os.environ[key] = value
    return path


SETTINGS_PATH = load_settings_file()

VERSION = "1.20.0"
ENV = os.environ.get
SIMULATE = ENV("SIGEN_SIMULATE", "0") == "1"
HOST = ENV("SIGEN_HOST", "")
PORT = int(ENV("SIGEN_PORT", "502"))
PLANT_UNIT = int(ENV("SIGEN_PLANT_UNIT", "247"))
INVERTER_UNIT = int(ENV("SIGEN_INVERTER_UNIT", "1"))
POLL = max(2.0, float(ENV("POLL_SECONDS", "5")))
HTTP_PORT = int(ENV("HTTP_PORT", "8080"))
DATA_DIR = Path(ENV("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TZ = ZoneInfo(ENV("TZ", "Australia/Sydney") or "Australia/Sydney")
WEB_DIR = Path(ENV("WEB_DIR", Path(__file__).resolve().parent.parent / "web")).resolve()
DEMO_DAYS = int(ENV("DEMO_HISTORY_DAYS", "14"))
RETENTION_DAYS = int(ENV("RETENTION_DAYS", "800"))
AMBER_KEY = ENV("AMBER_API_KEY", "").strip()
AMBER_SITE = ENV("AMBER_SITE_ID", "").strip() or None
LV_KEY = ENV("LOCALVOLTS_API_KEY", "").strip()
LV_PARTNER = ENV("LOCALVOLTS_PARTNER_ID", "").strip()
LV_NMI = ENV("LOCALVOLTS_NMI", "").strip() or None
PRICE_PROVIDER = (ENV("PRICE_PROVIDER", "") or "auto").strip().lower()
AEMO_REGION = (ENV("AEMO_REGION", "") or "NSW1").strip().upper()
AEMO_ENABLED = ENV("AEMO_ENABLED", "1") != "0"
AEMO_SIMULATE = ENV("AEMO_SIMULATE", "0") == "1"
CHARGER_SETTING = (ENV("SIGEN_CHARGER_UNIT", "") or "auto").strip()
SETTINGS_EDIT_FROM = ENV("SETTINGS_EDIT_FROM", "") or ""
def _env_num(name, default):
    v = (ENV(name, "") or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


INSULATION_MIN = _env_num("INSULATION_MIN_MOHM", 0.37)
SITE_LAT = _env_num("SITE_LAT", None)
SITE_LON = _env_num("SITE_LON", None)
SITE_ADDRESS = ENV("SITE_ADDRESS", "").strip()
SOLAR_KWP = _env_num("SOLAR_KWP", None)
SOLAR_PLANES = ENV("SOLAR_PLANES", "").strip()
WEATHER_ENABLED = ENV("WEATHER_ENABLED", "1") != "0"
INSULATION_WARN = max(INSULATION_MIN, _env_num("INSULATION_WARN_MOHM", 0.5))
# money: plan comparison and payback
COMPARE_PLANS = (ENV("COMPARE_PLANS", "") or "").strip().lower()
SYSTEM_COST = _env_num("SYSTEM_COST", None)
SYSTEM_INSTALLED = (ENV("SYSTEM_INSTALLED", "") or "").strip()
RETAILER_SUPPLY_C_DAY = (ENV("RETAILER_SUPPLY_C_DAY", "") or "").strip() or None  # a number, or dated: see money.parse_dated
RETAILER_FEE_MONTHLY = _env_num("RETAILER_FEE_MONTHLY", None)
RETAILER_SINCE = (ENV("RETAILER_SINCE", "") or "").strip()
# the car against a fuel car (see fuel.py)
FUEL_SETTINGS = {k: (_env_num(k, None) if k in ("EV_KWH_PER_100KM", "COMPARE_L_PER_100KM", "FUEL_RADIUS_KM", "FUEL_PRICE_C_L")
                     else (ENV(k, None) if k == "FUEL_EXCLUDE_BRANDS" else (ENV(k, "") or "").strip()))
                 for k in ("EV_MODEL", "EV_KWH_PER_100KM", "COMPARE_VEHICLE", "COMPARE_L_PER_100KM", "FUEL_TYPE",
                           "FUEL_RADIUS_KM", "FUEL_EXCLUDE_BRANDS", "FUEL_PRICE_C_L", "FUELCHECK_API_KEY",
                           "FUELCHECK_API_SECRET", "FUELWATCH_SUBURB")}
# backups, solar limits, battery wear, appliance timing, MQTT (see backup.py, limits.py, wear.py,
# advice.py, mqtt.py)
BACKUPS_ENABLED = ENV("BACKUPS_ENABLED", "1") != "0"
BACKUP_KEEP_DAILY = int(_env_num("BACKUP_KEEP_DAILY", 7))
BACKUP_KEEP_WEEKLY = int(_env_num("BACKUP_KEEP_WEEKLY", 4))
BACKUP_TIME = (ENV("BACKUP_TIME", "") or "02:30").strip()
BACKUP_DIR = Path(ENV("BACKUP_DIR", "") or DATA_DIR / "backups")
EXPORT_LIMIT_KW = _env_num("EXPORT_LIMIT_KW", None)
BATTERY_WARRANTY_YEARS = _env_num("BATTERY_WARRANTY_YEARS", 10)
BATTERY_WARRANTY_MWH = _env_num("BATTERY_WARRANTY_MWH", None)
BATTERY_WARRANTY_SOH = _env_num("BATTERY_WARRANTY_SOH", None)
APPLIANCES = ENV("APPLIANCES", None)
GRID_VIEW = (ENV("GRID_VIEW", "") or "tiles").strip().lower()
TIME_FORMAT = (ENV("TIME_FORMAT", "") or "12h").strip().lower()
NTFY_TOPIC = (ENV("NTFY_TOPIC", "") or "").strip()
NTFY_SERVER = (ENV("NTFY_SERVER", "") or "https://ntfy.sh").strip().rstrip("/")
NTFY_TOKEN = (ENV("NTFY_TOKEN", "") or "").strip()
NOTIFY_ON = (ENV("NOTIFY_ON", "") or "all").strip()
NOTIFY_WEEKLY = (ENV("NOTIFY_WEEKLY", "") or "off").strip()
NOTIFY_QUIET = (ENV("NOTIFY_QUIET", "") or "").strip()
UPDATE_CHECK = (ENV("UPDATE_CHECK", "") or "on").strip().lower() not in ("off", "0", "no", "false")
MQTT_HOST = (ENV("MQTT_HOST", "") or "").strip()
MQTT_SETTINGS = {k: (ENV(k, "") or "").strip() for k in ("MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_TOPIC",
                                                         "MQTT_INTERVAL")}
MQTT_DISCOVERY = ENV("MQTT_DISCOVERY_PREFIX", None)
INSULATION_CONFIRM = 3  # consecutive readings either side of the limit before it counts

COUNTERS = ["total_pv_kwh", "total_load_kwh", "total_import_kwh", "total_export_kwh",
            "total_charge_kwh", "total_discharge_kwh"]


import store as store_module  # noqa: E402  (the database; needs the time zone and retention first)
from store import Store  # noqa: E402,F401  (kept importable from here)
store_module.configure(TZ, RETENTION_DAYS)


def log(*a):
    print(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def local_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

"""SigenStor local dashboard collector.

Polls the SigenStor over Modbus TCP (read-only), keeps minute-level history in SQLite,
and serves the dashboard + a small JSON API.

Standard library only - runs anywhere Python 3.9+ runs (Docker, Raspberry Pi, NAS, PC).

Configuration (environment variables):
  SIGEN_HOST            inverter IP address                       (required unless SIGEN_SIMULATE=1)
  SIGEN_PORT            Modbus TCP port                           default 502
  SIGEN_PLANT_UNIT      plant Modbus unit id                      default 247
  SIGEN_INVERTER_UNIT   inverter Modbus unit id, 0 to skip        default 1
  SIGEN_SIMULATE        1 = run the built-in simulator instead    default 0
  POLL_SECONDS          seconds between polls (min 2)             default 5
  HTTP_PORT             dashboard/API port                        default 8080
  DATA_DIR              where the SQLite database lives           default ./data
  TZ                    local time zone for "today"               default Australia/Sydney
  DEMO_HISTORY_DAYS     days of simulated history to pre-fill     default 14 (simulator only)
  PRICE_PROVIDER        amber, localvolts or none                 default: whichever has a key
  AMBER_API_KEY         Amber Electric API key (optional)         app.amber.com.au/developers
  AMBER_SITE_ID         Amber site id, if you have more than one  default: first active site
  LOCALVOLTS_API_KEY    LocalVolts API key (optional)             localvolts.com, My Profile -> API Key
  LOCALVOLTS_PARTNER_ID LocalVolts Partner ID                     same page
  LOCALVOLTS_NMI        LocalVolts NMI, if you have more than one default: first on the account
  AEMO_REGION           NEM region for wholesale data             NSW1, QLD1, VIC1, SA1 or TAS1 (default NSW1)
  AEMO_ENABLED          0 to turn off AEMO market data            default 1
  SITE_LAT / SITE_LON   where the panels are, for weather and the solar forecast
  SITE_ADDRESS          or an address / suburb to look up instead (once, cached)
  SOLAR_KWP             array size, kW (helps the forecast before it has learned your system)
  SOLAR_PLANES          roof faces as tilt:azimuth:kW, comma separated; azimuth 0 = north, 90 = east
  WEATHER_ENABLED       0 to turn off weather                     default 1
  INSULATION_WARN_MOHM  insulation resistance "lower than usual" below this, MΩ   default 0.5
  INSULATION_MIN_MOHM   insulation fault below this (inverter's shutdown point)   default 0.37
  SIGEN_CHARGER_UNIT    Sigen AC charger's Modbus unit id         default auto (scan for it); 0 = none;
                        plant = charger has no Modbus address, use the inverter's figures only
  SETTINGS_EDIT_FROM    who may change settings from the dashboard: blank = the collector's own
                        network; or addresses/ranges, comma separated; or none

Most settings can be changed on the dashboard's Settings page (settings.py), which writes
sigen.env and restarts this process in place.
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
from config import *  # noqa: E402,F401,F403  (reads sigen.env first)
from aemo import Aemo  # noqa: E402
from charger import ChargerWatch  # noqa: E402
from weather import Weather  # noqa: E402
from outlook import Outlook  # noqa: E402
from money import Money  # noqa: E402
from importer import import_folder, reload_bills  # noqa: E402
from updates import UpdateCheck  # noqa: E402
from fuel import Fuel  # noqa: E402
from backup import Backups  # noqa: E402
from strings_health import StringsHealth  # noqa: E402
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
from store import Store  # noqa: E402,F401  (kept importable from here, for the tests)
from loop import Collector, start_simulator  # noqa: E402
from api import Api  # noqa: E402


def make_retailer(store):
    """Pick the electricity retailer price feed from the settings."""
    choice = PRICE_PROVIDER
    if choice == "auto":
        choice = "localvolts" if LV_KEY else "amber" if (AMBER_KEY or SIMULATE) else "none"
    if choice == "localvolts":
        if not (LV_KEY and LV_PARTNER):
            log("LocalVolts selected but LOCALVOLTS_API_KEY / LOCALVOLTS_PARTNER_ID are missing; prices off")
            return None
        return LocalVolts(store, LV_KEY, LV_PARTNER, LV_NMI, tz=TZ, log=log)
    if choice == "amber":
        return Amber(store, AMBER_KEY or None, AMBER_SITE, simulate=SIMULATE, log=log)
    return None


def main():
    if not SIMULATE and not HOST:
        sys.exit("Set SIGEN_HOST to your inverter's IP address, or SIGEN_SIMULATE=1 to use the simulator.")
    store = Store(DATA_DIR / ("simulator.db" if SIMULATE else "sigen.db"))
    host, port = (start_simulator(store) if SIMULATE else (HOST, PORT))
    import_folder(store, DATA_DIR, log=log)  # past days from a mySigen export, if any were dropped in
    reload_bills(store, DATA_DIR, log=log)   # bills imported before they were kept
    (DATA_DIR / "import").mkdir(exist_ok=True)
    retailer = make_retailer(store)
    if retailer and RETAILER_SINCE:
        retailer.since = RETAILER_SINCE  # no costs to fetch from before you joined
    if retailer:
        threading.Thread(target=retailer.run, daemon=True).start()
    aemo = Aemo(store, AEMO_REGION, enabled=AEMO_ENABLED, simulate=AEMO_SIMULATE, log=log)
    threading.Thread(target=aemo.run, daemon=True).start()
    weather = Weather(store, SITE_LAT, SITE_LON, SITE_ADDRESS, SOLAR_PLANES, SOLAR_KWP,
                      enabled=WEATHER_ENABLED, simulate=SIMULATE, cache_dir=DATA_DIR, tz=TZ, log=log)
    if weather.enabled:
        threading.Thread(target=weather.run, daemon=True).start()
    elif WEATHER_ENABLED:
        log("Weather off: set SITE_LAT and SITE_LON (or SITE_ADDRESS) to see weather and a solar forecast")
    collector = Collector(store, host, port, retailer, aemo, weather)
    if SIMULATE:
        collector.state["target"] = "built-in simulator"
    threading.Thread(target=collector.run, daemon=True).start()
    rname = (retailer.summary() or {}).get("provider") if retailer else None
    money = Money(store, TZ, DATA_DIR, preset=COMPARE_PLANS, system_cost=SYSTEM_COST, installed=SYSTEM_INSTALLED,
                  supply_c_day=RETAILER_SUPPLY_C_DAY, fee_monthly=RETAILER_FEE_MONTHLY, retailer_name=rname,
                  retailer_since=RETAILER_SINCE, log=log)
    Api.collector, Api.store, Api.retailer, Api.aemo, Api.weather = collector, store, retailer, aemo, weather
    Api.money = money
    sim_update = os.environ.get("SIM_UPDATE", "") if SIMULATE else ""  # the simulator pretends this version is out
    Api.updates = UpdateCheck(VERSION, enabled=UPDATE_CHECK and (not SIMULATE or bool(sim_update)), log=log,
                              fetch=(lambda: {"latest": sim_update, "url": "https://github.com/copperOstrich/sigen-dashboard/releases",
                                              "published": "", "notes": "Simulated release notes."}) if sim_update else None)
    fuel = Fuel(store, TZ, FUEL_SETTINGS, locate=weather.location, simulate=SIMULATE, log=log)
    Api.fuel = money.fuel = collector.fuel = fuel
    if fuel.enabled:
        threading.Thread(target=fuel.run, daemon=True).start()
    backups = Backups(store.path, BACKUP_DIR, TZ, keep_daily=BACKUP_KEEP_DAILY, keep_weekly=BACKUP_KEEP_WEEKLY,
                      at=BACKUP_TIME, enabled=BACKUPS_ENABLED, event=store.event, log=log)
    Api.backups = collector.backups = backups
    threading.Thread(target=backups.run, daemon=True).start()
    Api.limits = Limits(store, TZ, expected=weather.expected if weather.enabled else None,
                        export_limit_kw=EXPORT_LIMIT_KW, log=log)
    if MQTT_HOST:
        m = MQTT_SETTINGS
        mq = Mqtt(MQTT_HOST, port=m["MQTT_PORT"] or 1883, username=m["MQTT_USERNAME"], password=m["MQTT_PASSWORD"],
                  topic=m["MQTT_TOPIC"] or "sigen-dashboard",
                  discovery="homeassistant" if MQTT_DISCOVERY is None else MQTT_DISCOVERY,
                  interval=m["MQTT_INTERVAL"] or 10, version=VERSION, log=log,
                  get_state=lambda: (collector.snapshot(), collector.raw.get("plant")))
        collector.mqtt = mq
        threading.Thread(target=mq.run, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Api)
    httpd.daemon_threads = True
    if SETTINGS_PATH:
        log(f"Settings read from {SETTINGS_PATH}")
    log(f"SigenStor dashboard v{VERSION} on http://0.0.0.0:{HTTP_PORT}  "
        f"(reading {'simulator' if SIMULATE else f'{host}:{port}'}, plant unit {PLANT_UNIT}, "
        f"inverter unit {INVERTER_UNIT or 'off'}, every {POLL:g}s; prices: "
        f"{(retailer.state['provider'] + ' ' + retailer.state['mode']) if retailer else 'off'}; "
        f"AEMO: {AEMO_REGION if AEMO_ENABLED else 'off'}{' (simulated)' if AEMO_SIMULATE else ''})")

    restarting = threading.Event()

    def restart():
        restarting.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    Api.restart = restart

    def stop(signum, _frame):
        # Inside a container this process is PID 1, which ignores SIGTERM unless it
        # installs a handler, so `docker stop` would otherwise wait 10 s and kill it.
        log(f"Stopping ({signal.Signals(signum).name})")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        try:
            with collector.rec_lock:
                collector.stopping.set()
                collector.flush_minute()
                collector.charger.close()
            store.close()
        except Exception as e:  # never block shutdown
            log("Error while saving on shutdown:", e)
        log("Stopped cleanly")
    if restarting.is_set():
        httpd.server_close()
        log("Restarting with the new settings")
        args = getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv
        os.execv(sys.executable, [sys.executable] + list(args[1:]))


if __name__ == "__main__":
    main()

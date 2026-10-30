"""What your car charging would have cost in fuel: each charging session against an equivalent
petrol or diesel car, at the servo prices near you on the day.

Settings
  EV_MODEL, EV_KWH_PER_100KM            your EV and its energy use (kWh per 100 km)
  COMPARE_VEHICLE, COMPARE_L_PER_100KM  the fuel car to compare with (litres per 100 km)
  FUEL_TYPE                             E10, U91, U95, U98 or Diesel (default U91)
  FUEL_RADIUS_KM                        stations within this distance of home (default 10)
  FUEL_EXCLUDE_BRANDS                   brands to leave out, comma separated (default Costco:
                                        members-only prices aren't what most people pay)
  FUEL_PRICE_C_L                        a fixed price (c/L) for days without a recorded price
  FUELCHECK_API_KEY, FUELCHECK_API_SECRET   NSW FuelCheck (free at api.nsw.gov.au), NSW and TAS
  FUELWATCH_SUBURB                      WA FuelWatch (no account needed), your suburb

Home is SITE_LAT/SITE_LON, or SITE_ADDRESS looked up (the same as for the weather).

The average price for your fuel type at the stations in range is recorded a few times a day and
kept per day, so each session is priced at the day it happened. Sessions before the first
recorded price use the fixed price, or failing that the earliest recorded one (marked as such).

A charging session's cost: the energy from the grid at what you paid for it, plus the energy from
your own solar at what it would have earned exported at the time (never below zero: when the
export price is negative, solar is free). Saved = fuel equivalent - that.
"""
import base64
import json
import math
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FUELCHECK = "https://api.onegov.nsw.gov.au"
FUELWATCH = "https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS"
FUELCHECK_CODES = {"E10": "E10", "U91": "U91", "U95": "P95", "U98": "P98", "DIESEL": "DL"}
FUELWATCH_PRODUCTS = {"U91": 1, "U95": 2, "DIESEL": 4, "U98": 6}  # E10 is barely sold in WA
LABEL = {"E10": "E10", "U91": "Unleaded 91", "U95": "Premium 95", "U98": "Premium 98", "DIESEL": "Diesel"}
POLL_S = 3 * 3600


class FuelError(Exception):
    pass


def _km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


class Fuel:
    def __init__(self, store, tz, settings, locate=None, simulate=False, log=print):
        g = settings.get
        self.store, self.tz, self.log, self.locate, self.simulate = store, tz, log, locate, simulate
        self.ev_model = g("EV_MODEL") or "your EV"
        self.ev_kwh = g("EV_KWH_PER_100KM")
        self.car = g("COMPARE_VEHICLE") or "a fuel car"
        self.car_l = g("COMPARE_L_PER_100KM")
        ft = (g("FUEL_TYPE") or "U91").strip().upper().replace(" ", "")
        self.fuel = {"ULP": "U91", "PULP": "U95", "P95": "U95", "P98": "U98", "DL": "DIESEL"}.get(ft, ft)
        if self.fuel not in LABEL:
            log(f"FUEL_TYPE '{ft}' isn't one of E10, U91, U95, U98, Diesel; using U91")
            self.fuel = "U91"
        self.radius = g("FUEL_RADIUS_KM") or 10
        self.exclude = [b.strip().lower() for b in (g("FUEL_EXCLUDE_BRANDS") if g("FUEL_EXCLUDE_BRANDS") is not None
                                                     else "Costco").split(",") if b.strip()]
        self.fixed = g("FUEL_PRICE_C_L")
        self.key, self.secret = g("FUELCHECK_API_KEY") or "", g("FUELCHECK_API_SECRET") or ""
        self.suburb = g("FUELWATCH_SUBURB") or ""
        self.enabled = bool(self.ev_kwh and self.car_l)
        self.source = ("simulator" if simulate and not (self.key or self.suburb) else
                       "FuelCheck" if self.key and self.secret else "FuelWatch" if self.suburb else
                       "fixed" if self.fixed else None)
        self.lock = threading.Lock()
        self.token, self.token_exp = None, 0
        self.state = {"status": "starting" if self.source not in (None, "fixed") else "ok", "error": None,
                      "updated": None, "price_c": None, "stations": []}

    # ---------------------------------------------------------------- polling
    def run(self):
        if not self.enabled or self.source in (None, "fixed"):
            return
        while True:
            try:
                self.poll()
            except Exception as e:  # never let fuel prices take the collector down
                with self.lock:
                    self.state.update(status="error", error=str(e))
                self.log("Fuel prices:", e)
            time.sleep(POLL_S)

    def _home(self):
        loc = self.locate() if self.locate else None
        if not loc or loc.get("lat") is None:
            raise FuelError("needs your location: set SITE_LAT and SITE_LON, or SITE_ADDRESS")
        return loc["lat"], loc["lon"]

    def poll(self):
        if self.source == "simulator":
            stations = self._sim()
        else:
            lat, lon = self._home()
            stations = self._fuelcheck(lat, lon) if self.source == "FuelCheck" else self._fuelwatch(lat, lon)
        stations = [s for s in stations if s.get("price") and s["distance"] <= self.radius
                    and not any(x in (s.get("brand") or "").lower() or x in (s.get("name") or "").lower()
                                for x in self.exclude)]
        if not stations:
            raise FuelError(f"no {LABEL[self.fuel]} prices within {self.radius:g} km")
        avg = sum(s["price"] for s in stations) / len(stations)
        day = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.store.save_fuel_price(day, self.fuel, avg, len(stations), self.source)
        with self.lock:
            self.state.update(status="ok", error=None, updated=time.time(), price_c=round(avg, 1),
                              stations=sorted(stations, key=lambda s: s["distance"])[:30])

    def _get(self, url, headers=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Accept": "application/json", "User-Agent": "sigen-dashboard/1.0",
                                              **({"Content-Type": "application/json"} if body is not None else {}),
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            msg = e.read()[:200].decode("utf-8", "ignore")
            if e.code in (401, 403):
                raise FuelError(f"FuelCheck refused the key ({e.code}); check FUELCHECK_API_KEY and FUELCHECK_API_SECRET")
            raise FuelError(f"fuel price service returned {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise FuelError(f"can't reach the fuel price service: {e.reason}")

    def _fuelcheck(self, lat, lon):
        if not self.token or time.time() > self.token_exp:
            basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            tok = json.loads(self._get(f"{FUELCHECK}/oauth/client_credential/accesstoken?grant_type=client_credentials",
                                       {"Authorization": f"Basic {basic}"}))
            self.token = tok["access_token"]
            self.token_exp = time.time() + int(tok.get("expires_in", 43199)) - 300
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %I:%M:%S %p")
        res = json.loads(self._get(f"{FUELCHECK}/FuelPriceCheck/v2/fuel/prices/nearby", {
            "Authorization": f"Bearer {self.token}", "apikey": self.key, "transactionid": str(uuid.uuid4()),
            "requesttimestamp": now}, {
            "fueltype": FUELCHECK_CODES[self.fuel], "latitude": f"{lat}", "longitude": f"{lon}",
            "radius": f"{self.radius:g}", "sortby": "price", "sortascending": "true"}))
        prices = {}
        for p in res.get("prices") or []:
            if p.get("fueltype") == FUELCHECK_CODES[self.fuel] and p.get("price") is not None:
                prices[str(p.get("stationcode"))] = float(p["price"])
        out = []
        for s in res.get("stations") or []:
            code = str(s.get("code") or s.get("stationcode") or "")
            if code in prices:
                loc = s.get("location") or {}
                dist = loc.get("distance")
                if dist is None and loc.get("latitude") is not None:
                    dist = _km(lat, lon, float(loc["latitude"]), float(loc["longitude"]))
                out.append({"name": s.get("name"), "brand": s.get("brand"), "address": s.get("address"),
                            "price": prices[code], "distance": round(float(dist if dist is not None else 0), 1)})
        return out

    def _fuelwatch(self, lat, lon):
        product = FUELWATCH_PRODUCTS.get(self.fuel)
        if not product:
            raise FuelError(f"FuelWatch doesn't list {LABEL[self.fuel]}; set FUEL_PRICE_C_L instead")
        q = urllib.parse.urlencode({"Product": product, "Suburb": self.suburb, "Surrounding": "yes"})
        root = ET.fromstring(self._get(f"{FUELWATCH}?{q}", {"Accept": "application/rss+xml"}))
        out = []
        for it in root.iter("item"):
            f = lambda k: (it.findtext(k) or "").strip()
            try:
                la, lo, pr = float(f("latitude")), float(f("longitude")), float(f("price"))
            except ValueError:
                continue
            out.append({"name": f("trading-name"), "brand": f("brand"), "address": f("address"),
                        "price": pr, "distance": round(_km(lat, lon, la, lo), 1)})
        return out

    def _sim(self):
        base = {"E10": 186, "U91": 189, "U95": 204, "U98": 213, "DIESEL": 196}[self.fuel]
        r = random.Random(datetime.now(self.tz).strftime("%Y-%m-%d"))
        names = ["Ampol", "BP", "Shell", "7-Eleven", "Metro", "United", "Costco"]
        return [{"name": f"{n} Example {i}", "brand": n, "address": "", "price": base + r.uniform(-12, 14),
                 "distance": round(r.uniform(0.5, 12), 1)} for i, n in enumerate(names * 2)]

    # ---------------------------------------------------------------- prices and values
    def price_on(self, day):
        """(c/L, how it was found) for a local day."""
        p = self.store.fuel_price(day, self.fuel)
        if p:
            return p, "recorded"
        if self.fixed:
            return float(self.fixed), "fixed"
        p = self.store.fuel_price_nearest(day, self.fuel)
        return (p, "nearest") if p else (None, None)

    def per_kwh(self, price_c):
        """$ of fuel the fuel car would burn for the distance one kWh takes your EV."""
        return (self.car_l / self.ev_kwh) * price_c / 100

    def session(self, s):
        if not self.enabled or not s.get("kwh"):
            return None
        day = datetime.fromtimestamp(s["start"], self.tz).strftime("%Y-%m-%d")
        price, how = self.price_on(day)
        km = s["kwh"] / self.ev_kwh * 100
        litres = km * self.car_l / 100
        grid = s.get("grid_kwh") or 0
        solar = max(0.0, s["kwh"] - grid)
        sell = self.store.avg_sell_c(s["start"], s.get("end") or time.time())
        solar_cost = solar * max(0.0, sell) / 100 if sell is not None else None
        charge = None if s.get("cost") is None else s["cost"] + (solar_cost or 0)
        fuel = litres * price / 100 if price else None
        return {"km": round(km), "litres": round(litres, 1), "price_c": round(price, 1) if price else None,
                "price_from": how, "fuel_cost": round(fuel, 2) if fuel is not None else None,
                "solar_kwh": round(solar, 2), "solar_cost": round(solar_cost, 2) if solar_cost is not None else None,
                "charge_cost": round(charge, 2) if charge is not None else None,
                "charge_c_per_kwh": round(charge / s["kwh"] * 100, 1) if charge is not None else None,
                "saved": round(fuel - charge, 2) if fuel is not None and charge is not None else None}

    def summary(self, sessions=None):
        with self.lock:
            st = json.loads(json.dumps(self.state))
        price, how = self.price_on(datetime.now(self.tz).strftime("%Y-%m-%d"))
        out = {"enabled": self.enabled, "ev_model": self.ev_model, "ev_kwh_per_100km": self.ev_kwh,
               "car": self.car, "car_l_per_100km": self.car_l, "fuel": self.fuel, "fuel_label": LABEL[self.fuel],
               "radius_km": self.radius, "exclude": self.exclude, "source": self.source,
               "status": st["status"], "error": st["error"], "updated": st["updated"],
               "price_c": round(price, 1) if price else None, "price_from": how,
               "stations": st["stations"]}
        if not self.enabled:
            out["reason"] = "set EV_KWH_PER_100KM and COMPARE_L_PER_100KM"
        if sessions is not None and self.enabled:
            vals = [v for v in (x.get("fuel") for x in sessions) if v and v.get("saved") is not None]
            month = datetime.now(self.tz).strftime("%Y-%m")
            this = [v for x, v in ((x, x.get("fuel")) for x in sessions)
                    if v and v.get("saved") is not None and
                    datetime.fromtimestamp(x["start"], self.tz).strftime("%Y-%m") == month]
            out["totals"] = {"sessions": len(vals), "km": sum(v["km"] for v in vals),
                             "litres": round(sum(v["litres"] for v in vals), 1),
                             "fuel_cost": round(sum(v["fuel_cost"] for v in vals), 2),
                             "charge_cost": round(sum(v["charge_cost"] for v in vals), 2),
                             "saved": round(sum(v["saved"] for v in vals), 2),
                             "this_month": round(sum(v["saved"] for v in this), 2)}
        return out

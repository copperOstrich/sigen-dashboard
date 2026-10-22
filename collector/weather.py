"""Local weather and a solar production forecast, from Open-Meteo (free, no account).

Location: SITE_LAT/SITE_LON, or SITE_ADDRESS looked up once (OpenStreetMap's Nominatim, then
Open-Meteo's place search) and cached in the data folder.

Solar forecast: Open-Meteo's irradiance on a tilted surface (W/m², average of the preceding hour)
for each roof face in SOLAR_PLANES, times that face's kW, times a performance factor. The factor
starts at 0.85 and is then learned from your own system: each refresh compares the last two
weeks of the model's hourly irradiance with what the panels actually produced, so it soaks up
inverter losses, temperature, shading and the array's real size.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

UA = "sigen-dashboard (https://github.com/copperOstrich/sigen-dashboard)"
FORECAST = "https://api.open-meteo.com/v1/forecast"
REFRESH = 30 * 60
BASE_FACTOR = 0.85
PAST_DAYS = 14

# WMO weather codes used by Open-Meteo -> (description, icon)
WMO = {
    0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "part"), 3: ("Cloudy", "cloud"),
    45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorms", "storm"), 96: ("Thunderstorms with hail", "storm"), 99: ("Thunderstorms with hail", "storm"),
}


class WeatherError(Exception):
    pass


def describe(code):
    try:
        return WMO.get(int(code), ("Unknown", "cloud"))
    except (TypeError, ValueError):
        return ("Unknown", "cloud")


def _get(url, params=None, timeout=30):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read()).get("reason")
        except Exception:
            reason = None
        raise WeatherError(f"{urllib.parse.urlparse(url).netloc} returned {e.code}{': ' + reason if reason else ''}")
    except urllib.error.URLError as e:
        raise WeatherError(f"Can't reach {urllib.parse.urlparse(url).netloc}: {e.reason}")
    except ValueError:
        raise WeatherError("weather service returned something that isn't JSON")


def parse_planes(spec, kwp):
    """'tilt:azimuth:kW,...' with compass azimuth (0 = north, 90 = east, 180 = south, 270 = west).

    Default: one north-facing plane at 20° with the whole array (or a south-facing one in the
    northern hemisphere, handled by the caller passing the right default).
    """
    planes = []
    for part in filter(None, (spec or "").replace(";", ",").split(",")):
        bits = [b.strip() for b in part.split(":")]
        try:
            tilt, az = float(bits[0]), float(bits[1])
            kw = float(bits[2]) if len(bits) > 2 and bits[2] else None
        except (ValueError, IndexError):
            raise WeatherError(f"SOLAR_PLANES entry '{part}' should look like tilt:azimuth:kW, e.g. 20:0:6.6")
        planes.append({"tilt": max(0.0, min(90.0, tilt)), "azimuth": az % 360, "kw": kw})
    if not planes:
        planes = [{"tilt": 20.0, "azimuth": 0.0, "kw": kwp}]
    unknown = [p for p in planes if p["kw"] is None]
    if unknown:  # share the remaining array size between faces without their own kW
        rest = max(0.0, (kwp or 0) - sum(p["kw"] for p in planes if p["kw"] is not None))
        for p in unknown:
            p["kw"] = rest / len(unknown) if rest else None
    return planes


def om_azimuth(compass):
    """Compass bearing the panels face -> Open-Meteo's convention (0 south, -90 east, 90 west, ±180 north)."""
    a = (compass - 180.0) % 360.0
    return a - 360.0 if a > 180.0 else a


class Weather:
    def __init__(self, store, lat=None, lon=None, address=None, planes="", kwp=None, enabled=True,
                 simulate=False, cache_dir=None, tz=None, log=print):
        self.store, self.log, self.tz = store, log, tz
        self.lat, self.lon, self.address = lat, lon, (address or "").strip() or None
        self.kwp, self.plane_spec = kwp, planes
        self.cache = cache_dir / "site-location.json" if cache_dir else None
        self.simulate = simulate and lat is None and not self.address
        self.enabled = enabled and (lat is not None or bool(self.address) or self.simulate)
        self.lock = threading.Lock()
        self.factor, self.factor_n = BASE_FACTOR, 0
        self.state = {"enabled": self.enabled, "status": "starting" if self.enabled else "off",
                      "mode": "simulator" if self.simulate else "live", "error": None, "updated": None,
                      "location": None, "planes": [], "factor": None, "current": None, "hourly": [], "daily": []}

    # ---------------------------------------------------------------- public
    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def summary(self):
        """Small version for the live stream: now, and today's and tomorrow's solar forecast."""
        with self.lock:
            s = self.state
            d = s["daily"]
            out = {"enabled": s["enabled"], "status": s["status"], "current": s["current"],
                   "today": d[0] if d else None, "tomorrow": d[1] if len(d) > 1 else None}
        if out["today"]:
            out["remaining_kwh"] = self.remaining_today()
        return out

    def remaining_today(self):
        now = time.time()
        with self.lock:
            hours = self.state["hourly"]
        end = self._local_midnight(now) + 86400
        total = 0.0
        for h in hours:
            a, b = h["t"], h["t"] + 3600
            if b <= now or a >= end or h.get("pv_w") is None:
                continue
            total += h["pv_w"] * (b - max(a, now)) / 3.6e6
        return round(total, 1)

    def run(self):
        if not self.enabled:
            return
        while True:
            try:
                self.poll()
                wait = REFRESH
            except WeatherError as e:
                self._error(str(e))
                wait = 5 * 60
            except Exception as e:  # never take the collector down
                self._error(f"{type(e).__name__}: {e}")
                wait = 10 * 60
            time.sleep(wait)

    # ---------------------------------------------------------------- internals
    def _error(self, msg):
        if self.state.get("error") != msg:
            self.log("Weather:", msg)
        with self.lock:
            self.state.update(status="error", error=msg)

    def _local_midnight(self, ts):
        d = datetime.fromtimestamp(ts, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def _locate(self):
        if self.lat is not None and self.lon is not None:
            return {"lat": self.lat, "lon": self.lon, "name": None, "source": "SITE_LAT/SITE_LON"}
        if self.cache and self.cache.is_file():
            try:
                c = json.loads(self.cache.read_text())
                if c.get("address") == self.address:
                    return c
            except ValueError:
                pass
        hit = None
        try:
            r = _get("https://nominatim.openstreetmap.org/search",
                     {"q": self.address, "format": "jsonv2", "limit": 1, "addressdetails": 0})
            if r:
                hit = {"lat": float(r[0]["lat"]), "lon": float(r[0]["lon"]), "name": r[0].get("display_name"),
                       "source": "OpenStreetMap"}
        except WeatherError as e:
            self.log("Weather: address lookup on OpenStreetMap failed:", e)
        if not hit:  # Open-Meteo's search knows suburbs and towns, not street addresses
            name = self.address.split(",")[-2 if "," in self.address else -1].strip()
            r = _get("https://geocoding-api.open-meteo.com/v1/search", {"name": name, "count": 1})
            res = (r or {}).get("results") or []
            if res:
                hit = {"lat": res[0]["latitude"], "lon": res[0]["longitude"],
                       "name": ", ".join(x for x in (res[0].get("name"), res[0].get("admin1"), res[0].get("country")) if x),
                       "source": "Open-Meteo place search"}
        if not hit:
            raise WeatherError(f"couldn't find '{self.address}'; try a suburb and state, or set SITE_LAT and SITE_LON")
        hit["address"] = self.address
        if self.cache:
            try:
                self.cache.write_text(json.dumps(hit))
            except OSError:
                pass
        self.log(f"Weather: located '{self.address}' at {hit['lat']:.4f}, {hit['lon']:.4f} ({hit['source']})")
        return hit

    def poll(self):
        if self.simulate:
            return self._poll_sim()
        loc = self._locate()
        lat, lon = loc["lat"], loc["lon"]
        kwp = self.kwp
        planes = parse_planes(self.plane_spec, kwp)
        if lat > 0 and not self.plane_spec:
            planes[0]["azimuth"] = 180.0  # northern hemisphere default: face south
        base = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "auto",
                "past_days": PAST_DAYS, "forecast_days": 4, "timeformat": "unixtime"}
        main = _get(FORECAST, {**base,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,cloud_cover,"
                       "wind_speed_10m,is_day",
            "hourly": "temperature_2m,cloud_cover,precipitation_probability,precipitation,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,uv_index_max,sunrise,sunset"})
        times = main["hourly"]["time"]  # unix seconds, end of each hour's averaging period
        pv = [0.0] * len(times)
        have_kw = all(p["kw"] for p in planes)
        for p in planes:
            r = _get(FORECAST, {**base, "hourly": "global_tilted_irradiance",
                                "tilt": f"{p['tilt']:g}", "azimuth": f"{om_azimuth(p['azimuth']):g}"})
            gti = r["hourly"]["global_tilted_irradiance"]
            for i, g in enumerate(gti):
                if g is not None:
                    pv[i] += (g / 1000.0) * (p["kw"] or 1.0)
        # pv[i] is now kW per (factor), for the hour ENDING at times[i]
        starts = [t - 3600 for t in times]
        self._calibrate(starts, pv, have_kw)
        self._publish(loc, planes, main, starts, pv, have_kw)

    def _calibrate(self, starts, raw, have_kw):
        """Learn how the real system compares with the model: a high percentile of actual/model over
        reasonably sunny past hours, so curtailment (full battery, export limit) and cloudy-hour misses
        don't drag it down."""
        now = time.time()
        actual = self.store.hourly_pv(starts[0], now)
        peak = max(raw) if raw else 0
        ratios = []
        for t, r in zip(starts, raw):
            if t + 3600 > now or r < 0.25 * peak or t not in actual:
                continue
            ratios.append(actual[t] / 1000.0 / r)
        if len(ratios) >= 12:
            ratios.sort()
            f = ratios[min(len(ratios) - 1, int(len(ratios) * 0.85))]
            # with no array size given, the factor also carries the size, so only bound it loosely
            lo, hi = (0.4, 1.3) if have_kw else (0.05, 100.0)
            self.factor, self.factor_n = max(lo, min(hi, f)), len(ratios)
        elif not have_kw:
            self.factor, self.factor_n = None, len(ratios)  # nothing to go on yet

    def _publish(self, loc, planes, main, starts, raw, have_kw):
        f = self.factor
        cur = main.get("current") or {}
        h = main["hourly"]
        now = time.time()
        hourly = []
        for i, t in enumerate(starts):
            if t < now - 26 * 3600 or i == 0:
                continue
            # Irradiance is the average of the hour ENDING at time[i] (so it belongs to the hour starting
            # at t); temperature, cloud and so on are spot values AT time[i - 1], which is t.
            j = i - 1
            desc, icon = describe(h["weather_code"][j])
            hourly.append({"t": t, "temp": h["temperature_2m"][j], "cloud": h["cloud_cover"][j],
                           "rain_prob": h["precipitation_probability"][j], "rain_mm": h["precipitation"][i],
                           "desc": desc, "icon": icon, "is_day": h["is_day"][j],
                           "pv_w": round(raw[i] * f * 1000) if f is not None else None})
        d = main["daily"]
        daily = []
        for i, t in enumerate(d["time"]):
            day_start, day_end = t, t + 86400
            if day_end <= self._local_midnight(now):
                continue
            kwh = sum(r for s, r in zip(starts, raw) if day_start <= s < day_end) * f if f is not None else None
            desc, icon = describe(d["weather_code"][i])
            daily.append({"t": t, "desc": desc, "icon": icon, "tmax": d["temperature_2m_max"][i],
                          "tmin": d["temperature_2m_min"][i], "rain_prob": d["precipitation_probability_max"][i],
                          "rain_mm": d["precipitation_sum"][i], "uv": d["uv_index_max"][i],
                          "sunrise": d["sunrise"][i], "sunset": d["sunset"][i],
                          "pv_kwh": round(kwh, 1) if kwh is not None else None})
        desc, icon = describe(cur.get("weather_code"))
        current = {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                   "humidity": cur.get("relative_humidity_2m"), "cloud": cur.get("cloud_cover"),
                   "wind": cur.get("wind_speed_10m"), "is_day": cur.get("is_day"), "desc": desc, "icon": icon}
        with self.lock:
            self.state.update(status="ok", error=None, updated=time.time(),
                              location={"lat": loc["lat"], "lon": loc["lon"], "name": loc.get("name")},
                              planes=planes, factor={"value": round(f, 3) if f is not None else None,
                                                     "hours": self.factor_n, "learned": self.factor_n >= 12},
                              current=current, hourly=hourly, daily=daily[:4])

    # ---------------------------------------------------------------- simulator
    def _poll_sim(self):
        """Weather to match the built-in simulator's solar model, so the panel can be tried out."""
        import simulator
        now = time.time()
        mid = self._local_midnight(now)
        starts = [mid - 86400 + 3600 * i for i in range(24 * 5)]
        raw, hourly_codes = [], []
        for t in starts:
            lt = datetime.fromtimestamp(t, self.tz).replace(tzinfo=None)
            w = sum(simulator.solar_w(lt + timedelta(minutes=m)) for m in range(0, 60, 10)) / 6
            raw.append(w / 1000.0)
            cloud = simulator._rand(lt.date().isoformat(), "cloud") * 0.75  # the simulator's cloudiness
            hourly_codes.append(0 if cloud < 0.1 else 1 if cloud < 0.25 else 2 if cloud < 0.5 else 3)
        hourly = []
        for i, t in enumerate(starts):
            if t < now - 26 * 3600:
                continue
            lt = datetime.fromtimestamp(t, self.tz)
            cloud_frac = simulator._rand(lt.date().isoformat(), "cloud")
            temp = 15 + 9 * math.sin(math.pi * max(0, min(1, (lt.hour - 5) / 15)))
            desc, icon = describe(hourly_codes[i])
            hourly.append({"t": t, "temp": round(temp, 1), "cloud": round(cloud_frac * 75),
                           "rain_prob": 10 if hourly_codes[i] < 3 else 40, "rain_mm": 0, "desc": desc, "icon": icon,
                           "is_day": 1 if 6 <= lt.hour < 19 else 0, "pv_w": round(raw[i] * 1000)})
        daily = []
        for k in range(4):
            ds = mid + 86400 * k
            hs = [h for h in hourly if ds <= h["t"] < ds + 86400]
            if not hs:
                continue
            noon = hs[min(len(hs) - 1, 12)]
            daily.append({"t": ds, "desc": noon["desc"], "icon": noon["icon"], "tmax": max(h["temp"] for h in hs),
                          "tmin": min(h["temp"] for h in hs), "rain_prob": max(h["rain_prob"] for h in hs),
                          "rain_mm": 0, "uv": 9, "sunrise": ds + 5 * 3600 + 50 * 60, "sunset": ds + 19 * 3600 + 10 * 60,
                          "pv_kwh": round(sum(h["pv_w"] for h in hs) / 1000, 1)})
        cur_h = next((h for h in hourly if h["t"] <= now < h["t"] + 3600), hourly[0])
        with self.lock:
            self.state.update(status="ok", error=None, updated=now,
                              location={"lat": -33.87, "lon": 151.21, "name": "Simulated location"},
                              planes=[{"tilt": 20.0, "azimuth": 0.0, "kw": simulator.PV_KWP}],
                              factor={"value": 1.0, "hours": 0, "learned": False},
                              current={"temp": cur_h["temp"], "feels": cur_h["temp"], "humidity": 55,
                                       "cloud": cur_h["cloud"], "wind": 12, "is_day": cur_h["is_day"],
                                       "desc": cur_h["desc"], "icon": cur_h["icon"]},
                              hourly=hourly, daily=daily)

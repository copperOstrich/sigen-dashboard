"""AEMO National Electricity Market data (no account needed).

Reads AEMO's official public market files on NEMWeb (https://nemweb.com.au/Reports/Current/),
the same data market participants use. Every part is fetched independently, so a failure
in one never breaks the others or the rest of the collector.

  DispatchIS_Reports     every 5 min  price, price status, demand, interchange, interconnectors (all regions)
  PredispatchIS_Reports  every 30 min AEMO's 30-minute price and demand forecast
  Dispatch_SCADA         every 5 min  output of every large generator; with AEMO's registration
                                      list (which fuel each generator uses) this gives the fuel mix

Until November 2026 this used the feeds behind AEMO's old NEM data dashboard; AEMO retired
those when the dashboard moved, so NEMWeb is used instead.

Times are NEM time (AEST, UTC+10, no daylight saving). Prices are $/MWh (RRP);
divide by 10 for c/kWh.
"""
import csv
import gzip
import io
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEMWEB = "https://nemweb.com.au/Reports/Current/"
REGISTRATION_URL = ("https://www.aemo.com.au/-/media/files/electricity/nem/participant_information/"
                    "nem-registration-and-exemption-list.xlsx")
NEM_TZ = timezone(timedelta(hours=10))
REGIONS = ("NSW1", "QLD1", "VIC1", "SA1", "TAS1")
RENEWABLE = {"solar", "wind", "hydro", "biomass"}
# which regions each interconnector joins (first -> second is a positive flow)
INTERCONNECTORS = {"NSW1-QLD1": ("NSW1", "QLD1"), "N-Q-MNSP1": ("NSW1", "QLD1"), "VIC1-NSW1": ("VIC1", "NSW1"),
                   "V-SA": ("VIC1", "SA1"), "V-S-MNSP1": ("VIC1", "SA1"), "T-V-MNSP1": ("TAS1", "VIC1"),
                   "NSW1-SA1": ("NSW1", "SA1")}
DUID_MAX_AGE = 7 * 86400        # refresh the generator list weekly
BACKFILL_FILES = 36             # on start, catch up at most 3 hours of 5-minute files
UA = "sigen-dashboard (home energy dashboard)"


class AemoError(Exception):
    pass


def _nem_ts(s):
    """'2026/11/09 19:15:00' or '2026-10-17T16:35:00' in NEM time -> epoch seconds."""
    s = s.strip().strip('"').replace("/", "-").replace(" ", "T")[:19]
    return datetime.fromisoformat(s).replace(tzinfo=NEM_TZ).timestamp()


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        raise AemoError(f"AEMO returned {e.code} for {url.rsplit('/', 2)[-2] or url}")
    except urllib.error.URLError as e:
        raise AemoError(f"Can't reach AEMO: {e.reason}")
    except OSError as e:
        raise AemoError(f"Can't reach AEMO: {e}")


def list_dir(folder, fetch=_get):
    """File names in a NEMWeb folder, oldest first (names start with the interval time)."""
    html = fetch(NEMWEB + folder + "/").decode("utf-8", "replace")
    names = {m.rsplit("/", 1)[-1] for m in re.findall(r'href="([^"]+\.zip)"', html, re.I)}
    if not names:
        raise AemoError(f"no files listed in NEMWeb {folder}")
    return sorted(names, key=file_key)


def file_key(name):
    """Sort key for NEMWeb file names: the interval time and sequence numbers inside them."""
    return tuple(int(x) for x in re.findall(r"\d{8,}", name)) or (0,)


def zip_lines(data):
    """Lines of the CSV inside a NEMWeb zip, streamed (forecast files are ~10 MB unzipped)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        name = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
    except (zipfile.BadZipFile, StopIteration):
        raise AemoError("AEMO file isn't a readable zip")
    with z.open(name) as f:
        yield from io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")


def parse_mms(lines, tables, keep=None):
    """Rows of the wanted tables from an AEMO MMS CSV ('I' header lines, 'D' data lines).

    tables: {"DISPATCH,PRICE", ...}. keep(table, row) can drop rows early.
    Returns {table: [row dict, ...]}."""
    out, cols = {t: [] for t in tables}, {}
    prefixes = tuple(f"{k},{t}," for t in tables for k in ("I", "D"))
    for line in lines:
        if not line.startswith(prefixes):
            continue
        row = next(csv.reader([line]))
        table = row[1] + "," + row[2]
        if row[0] == "I":
            cols[table] = row[4:]
        elif table in cols:
            d = dict(zip(cols[table], row[4:]))
            if keep is None or keep(table, d):
                out[table].append(d)
    return out


def _interval0(rows):
    """AEMO publishes an extra 'intervention' run when it steps in; prices come from run 0."""
    return [r for r in rows if str(r.get("INTERVENTION", "0")).strip() in ("0", "")]


# ------------------------------------------------------------------ fuel types
def fuel_of(primary, descriptor="", dispatch_type=""):
    """Fuel group for a generator from AEMO's registration list, or None for loads (pumps)."""
    p, d = (primary or "").strip().lower(), (descriptor or "").strip().lower()
    if "load" in (dispatch_type or "").lower() and "bidirectional" not in (dispatch_type or "").lower():
        return None
    if "battery" in p:
        return "Battery"
    for k in ("solar", "wind", "hydro"):
        if k in p:
            return k.capitalize()
    if "biomass" in p or "renewable" in p:
        return "Biomass"
    if "fossil" in p:
        if "black coal" in d:
            return "Black coal"
        if "brown coal" in d:
            return "Brown coal"
        if "gas" in d or "methane" in d or "ethane" in d:
            return "Gas"
        if "diesel" in d or "kerosene" in d or "oil" in d:
            return "Liquid fuel"
        return "Gas"
    return "Other"


def _col(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n


def parse_registration(data):
    """{DUID: [region, fuel or None]} from AEMO's NEM Registration and Exemption List (.xlsx)."""
    M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise AemoError("AEMO's registration list isn't a readable spreadsheet")
    rid = next((s.get(R + "id") for s in wb.iter(M + "sheet")
                if (s.get("name") or "").strip().lower().startswith("pu and scheduled")), None)
    target = next((r.get("Target") for r in rels if r.get("Id") == rid), None)
    if not target:
        raise AemoError("AEMO's registration list has no generator sheet")
    target = target.lstrip("/")
    path = target if target.startswith("xl/") else "xl/" + target
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(M + "si"):
            shared.append("".join(t.text or "" for t in si.iter(M + "t")))
    rows = []
    for row in ET.fromstring(z.read(path)).iter(M + "row"):
        vals = {}
        for c in row.iter(M + "c"):
            t, v = c.get("t"), c.find(M + "v")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(M + "t"))
            else:
                val = v.text if v is not None else ""
            vals[_col(c.get("r"))] = (val or "").strip()
        rows.append(vals)
    head_i = next((i for i, r in enumerate(rows) if "DUID" in r.values()), None)
    if head_i is None:
        raise AemoError("AEMO's registration list has no DUID column")
    idx = {v: k for k, v in rows[head_i].items()}
    need = ("DUID", "Region", "Fuel Source - Primary")
    if any(n not in idx for n in need):
        raise AemoError("AEMO's registration list columns have changed")
    out = {}
    for r in rows[head_i + 1:]:
        duid, region = r.get(idx["DUID"], ""), r.get(idx["Region"], "")
        if not duid or duid == "-" or region not in REGIONS:
            continue
        fuel = fuel_of(r.get(idx["Fuel Source - Primary"], ""), r.get(idx.get("Fuel Source - Descriptor"), ""),
                       r.get(idx.get("Dispatch Type"), ""))
        out[duid] = [region, fuel]      # fuel None: a load (pump), known but not generation
    if len(out) < 50:
        raise AemoError("AEMO's registration list looks incomplete")
    return out


def fuel_mix(scada_rows, duids):
    """{region: {fuel: MW}} from generator output; charging batteries and pumps are left out."""
    mixes, unknown = {}, 0
    for r in scada_rows:
        mw = _num(r.get("SCADAVALUE"))
        if mw is None or mw <= 0:
            continue
        info = duids.get(r.get("DUID"))
        if not info:
            unknown += 1
            continue
        region, fuel = info
        if not fuel:
            continue
        m = mixes.setdefault(region, {})
        m[fuel] = m.get(fuel, 0) + mw
    return mixes, unknown


class Aemo:
    def __init__(self, store, region="NSW1", enabled=True, simulate=False, log=print, data_dir=None, fetch=None):
        self.store, self.log = store, log
        self.region = region if region in REGIONS else "NSW1"
        self.enabled, self.simulate = enabled, simulate
        self.fetch = fetch or _get
        self.data_dir = Path(data_dir) if data_dir else None
        self.lock = threading.Lock()
        self.errors = {}
        self.seen = {}              # NEMWeb folder -> last file read
        self.duids, self.duids_at, self.duids_tried = {}, 0, 0
        self.state = {"enabled": enabled, "mode": "simulator" if simulate else "live",
                      "status": "starting" if enabled else "off", "region": self.region, "error": None,
                      "current": None, "forecast": [], "history": [], "fuel": [], "interconnectors": [],
                      "renewables_pct": None, "updated": None, "source": "NEMWeb",
                      "region_rows": {}, "region_mix": {}, "all_interconnectors": []}

    def snapshot(self, hours=24):
        with self.lock:
            s = json.loads(json.dumps(self.state))
        s["history"] = self.store.aemo_history(self.region, hours)
        s["regions"], s["nem"] = self._regions(s.pop("region_rows", {}), s.pop("region_mix", {}))
        return s

    @staticmethod
    def _regions(rows, mixes):
        """Every NEM region side by side, plus whole-market totals."""
        out = []
        for r in REGIONS:
            row, mix = rows.get(r) or {}, mixes.get(r) or {}
            if not row and not mix:
                continue
            out.append({"region": r, **row, "renewables_pct": mix.get("renewables_pct"),
                        "generation_mw": mix.get("total_mw"), "top_fuel": mix.get("top_fuel")})
        dem = [x for x in out if x.get("demand_mw") is not None]
        total = sum(x["demand_mw"] for x in dem) if dem else None
        priced = [x for x in dem if x.get("rrp") is not None]
        wsum = sum(x["demand_mw"] for x in priced)
        avg = sum(x["rrp"] * x["demand_mw"] for x in priced) / wsum if wsum > 0 else None
        gen = sum(m.get("total_mw") or 0 for m in mixes.values())
        ren = sum((m.get("total_mw") or 0) * (m.get("renewables_pct") or 0) / 100 for m in mixes.values())
        nem = {"demand_mw": round(total) if total is not None else None,
               "rrp": round(avg, 2) if avg is not None else None,
               "price_c": round(avg / 10, 2) if avg is not None else None,
               "generation_mw": round(gen) if gen else None,
               "renewables_pct": round(ren / gen * 100, 1) if gen else None}
        return out, nem

    def summary(self):
        with self.lock:
            c = self.state.get("current") or {}
            return {"enabled": self.enabled, "status": self.state["status"], "region": self.region,
                    "mode": self.state["mode"], "rrp": c.get("rrp"), "price_c": c.get("price_c"),
                    "price_status": c.get("price_status"), "demand_mw": c.get("demand_mw"),
                    "renewables_pct": self.state.get("renewables_pct"), "at": c.get("at"),
                    "apc": c.get("apc"), "suspended": c.get("suspended")}

    def run(self):
        if not self.enabled:
            return
        while True:
            try:
                self.poll()
            except Exception as e:  # never let AEMO take the collector down
                with self.lock:
                    self.state.update(status="error", error=str(e))
            # AEMO publishes each 5-minute interval about 10 seconds after it starts
            now = time.time()
            time.sleep(max(30, 300 - now % 300 + 45))

    # -------------------------------------------------------------- polling
    def poll(self):
        if self.simulate:
            return self._poll_sim()
        upd, errs = {}, {}
        for name, fn in (("prices", self._dispatch), ("forecast", self._forecast), ("fuel", self._fuel)):
            try:
                upd.update(fn())
            except Exception as e:
                errs[name] = str(e)
        if errs and errs != self.errors:
            self.log("AEMO:", "; ".join(dict.fromkeys(errs.values())))
        self.errors = errs
        with self.lock:
            cur = dict(self.state.get("current") or {})
            cur.update(upd.pop("current", {}))
            self.state.update(upd)
            self.state["current"] = cur or None
            self.state["status"] = "ok" if len(errs) < 3 else "error"
            self.state["error"] = "; ".join(dict.fromkeys(errs.values())) or None
            self.state["updated"] = time.time()

    def _new_files(self, folder, backfill=1):
        """Files in a NEMWeb folder not read yet (the latest `backfill` on the first look)."""
        names = list_dir(folder, self.fetch)
        last = self.seen.get(folder)
        new = [n for n in names if last is None or file_key(n) > file_key(last)]
        return new[-backfill:] if last is None else new[-BACKFILL_FILES:]

    def _backfill_count(self):
        """How many 5-minute files to read on start so the price history has no hole."""
        hist = self.store.aemo_history(self.region, BACKFILL_FILES / 12)
        if not hist:
            return BACKFILL_FILES
        return max(1, min(BACKFILL_FILES, int((time.time() - hist[-1]["t"]) // 300)))

    def _dispatch(self):
        folder = "DispatchIS_Reports"
        files = self._new_files(folder, self._backfill_count() if folder not in self.seen else 1)
        out = {}
        for name in files:
            out = self._read_dispatch(self.fetch(NEMWEB + folder + "/" + name)) or out
            self.seen[folder] = name
        return out

    def _read_dispatch(self, data):
        t = parse_mms(zip_lines(data), {"DISPATCH,PRICE", "DISPATCH,REGIONSUM", "DISPATCH,INTERCONNECTORRES"})
        prices = {r["REGIONID"]: r for r in _interval0(t["DISPATCH,PRICE"])}
        sums = {r["REGIONID"]: r for r in _interval0(t["DISPATCH,REGIONSUM"])}
        if self.region not in prices:
            raise AemoError(f"no {self.region} price in AEMO's dispatch file")
        rows = {}
        for rid in REGIONS:
            p, s = prices.get(rid) or {}, sums.get(rid) or {}
            if not p and not s:
                continue
            rrp = _num(p.get("RRP"))
            rows[rid] = {"rrp": rrp, "price_c": round(rrp / 10, 2) if rrp is not None else None,
                         "price_status": (p.get("PRICE_STATUS") or "").strip() or None,
                         "demand_mw": _num(s.get("TOTALDEMAND")),
                         # AEMO's NETINTERCHANGE is positive when the region exports
                         "net_interchange_mw": _num(s.get("NETINTERCHANGE")),
                         "apc": str(p.get("APCFLAG", "0")).strip() == "1"}
        ics = []
        for r in _interval0(t["DISPATCH,INTERCONNECTORRES"]):
            # limits are signed like the flow (import is negative); either can cross zero when
            # AEMO forces a link one way
            ics.append({"name": r.get("INTERCONNECTORID"), "mw": _num(r.get("MWFLOW")),
                        "export_limit": _num(r.get("EXPORTLIMIT")), "import_limit": _num(r.get("IMPORTLIMIT"))})
        p, s = prices[self.region], sums.get(self.region) or {}
        at = _nem_ts(p["SETTLEMENTDATE"])
        me = rows[self.region]
        if me["rrp"] is not None:
            self.store.save_aemo([(int(at - 300), self.region, me["rrp"], me["demand_mw"])])
        semi = [_num(s.get(k)) for k in ("SS_SOLAR_CLEAREDMW", "SS_WIND_CLEAREDMW")]
        cur = {"rrp": me["rrp"], "price_c": me["price_c"], "at": at, "price_status": me["price_status"],
               "demand_mw": me["demand_mw"], "net_interchange_mw": me["net_interchange_mw"],
               "scheduled_gen_mw": _num(s.get("DISPATCHABLEGENERATION")),
               "semischeduled_gen_mw": round(sum(v for v in semi if v is not None), 1) if any(v is not None for v in semi) else None,
               "apc": me["apc"], "suspended": str(p.get("MARKETSUSPENDEDFLAG", "0")).strip() == "1"}
        mine = [x for x in ics if self.region in self._ends(x["name"])]
        return {"current": cur, "interconnectors": mine, "region_rows": rows, "all_interconnectors": ics}

    @staticmethod
    def _ends(name):
        if name in INTERCONNECTORS:
            return INTERCONNECTORS[name]
        parts = (name or "").split("-")
        return tuple(parts) if len(parts) == 2 else ()

    def _forecast(self):
        folder = "PredispatchIS_Reports"
        # a new forecast every 30 minutes: only look again once the current one is getting old
        if self.state.get("forecast") and time.time() - self.state.get("forecast_at", 0) < 25 * 60:
            return {}
        files = self._new_files(folder)
        if not files:
            return {"forecast_at": time.time()}
        name = files[-1]
        fc = self._read_forecast(self.fetch(NEMWEB + folder + "/" + name, timeout=120))
        self.seen[folder] = name
        return {"forecast": fc, "forecast_at": time.time()}

    def _read_forecast(self, data):
        mine = lambda table, r: r.get("REGIONID") == self.region
        t = parse_mms(zip_lines(data), {"PREDISPATCH,REGION_PRICES", "PREDISPATCH,REGION_SOLUTION"}, mine)
        demand = {r.get("DATETIME"): _num(r.get("TOTALDEMAND")) for r in _interval0(t["PREDISPATCH,REGION_SOLUTION"])}
        now, out = time.time(), {}
        for r in _interval0(t["PREDISPATCH,REGION_PRICES"]):
            rrp, end = _num(r.get("RRP")), r.get("DATETIME")
            if rrp is None or not end:
                continue
            e = _nem_ts(end)                       # DATETIME is when the half hour ends
            if e <= now:
                continue
            out[e] = {"t": e - 1800, "end": e, "rrp": rrp, "price_c": round(rrp / 10, 2), "demand_mw": demand.get(end)}
        if not out:
            raise AemoError(f"no {self.region} forecast in AEMO's predispatch file")
        return [out[k] for k in sorted(out)]

    # ------------------------------------------------------------- fuel mix
    def _duid_file(self):
        return self.data_dir / "aemo-generators.json" if self.data_dir else None

    def _load_duids(self):
        """Which fuel each generator uses: cached for a week, refreshed from AEMO's registration list."""
        f = self._duid_file()
        if not self.duids and f and f.exists():
            try:
                saved = json.loads(f.read_text())
                self.duids, self.duids_at = saved["duids"], saved["at"]
            except (ValueError, KeyError, OSError):
                pass
        now = time.time()
        if now - self.duids_at < DUID_MAX_AGE or now - self.duids_tried < 3600:
            return
        self.duids_tried = now
        try:
            self.duids = parse_registration(self.fetch(REGISTRATION_URL, timeout=120))
            self.duids_at = now
            if f:
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps({"at": now, "duids": self.duids}))
                tmp.replace(f)
        except AemoError as e:
            if not self.duids:
                raise AemoError(f"can't get AEMO's generator list: {e}")
            self.log("AEMO: keeping the saved generator list:", e)

    def _fuel(self):
        self._load_duids()
        folder = "Dispatch_SCADA"
        files = self._new_files(folder)
        if not files:
            return {}
        name = files[-1]
        rows = parse_mms(zip_lines(self.fetch(NEMWEB + folder + "/" + name)), {"DISPATCH,UNIT_SCADA"})["DISPATCH,UNIT_SCADA"]
        self.seen[folder] = name
        mixes, unknown = fuel_mix(rows, self.duids)
        if not mixes:
            raise AemoError("no generator output in AEMO's SCADA file")
        out = self._mix(mixes.get(self.region, {}))
        out["region_mix"] = {k: self._brief(v) for k, v in mixes.items()}
        out["unmatched_generators"] = unknown
        return out

    @classmethod
    def _brief(cls, mix):
        m = cls._mix(mix)
        return {"total_mw": round(sum(mix.values())), "renewables_pct": m["renewables_pct"],
                "top_fuel": m["fuel"][0]["fuel"] if m["fuel"] else None}

    @staticmethod
    def _mix(mix):
        total = sum(mix.values())
        if total <= 0:
            return {"fuel": [], "renewables_pct": None}
        fuel = [{"fuel": k, "mw": round(v), "pct": round(v / total * 100, 1),
                 "renewable": k.lower() in RENEWABLE} for k, v in sorted(mix.items(), key=lambda kv: -kv[1])]
        ren = sum(f["mw"] for f in fuel if f["renewable"])
        return {"fuel": fuel, "renewables_pct": round(ren / total * 100, 1)}


    # -------------------------------------------------------------- simulator
    def _poll_sim(self):
        from amber import Amber  # same synthetic NEM price shape as the Amber simulator
        now = time.time()
        t = now - now % 300
        rrp = lambda ts: round(Amber.sim_price(ts)[2] * 10, 2)
        hist = [(int(t - i * 300), self.region, rrp(t - i * 300),
                 7200 + 1800 * math.sin((t - i * 300) / 13000)) for i in range(0, 288)]
        self.store.save_aemo(hist)
        f0 = t - t % 1800 + 1800
        forecast = [{"t": f0 + i * 1800, "end": f0 + (i + 1) * 1800, "rrp": rrp(f0 + i * 1800),
                     "price_c": round(rrp(f0 + i * 1800) / 10, 2), "demand_mw": None} for i in range(40)]
        h = datetime.fromtimestamp(now, NEM_TZ).hour + 1
        sun = max(0.0, math.sin(math.pi * (h - 6) / 13)) if 6 <= h <= 19 else 0.0
        mix = {"Black coal": 5200 - 1800 * sun, "Solar": 3200 * sun, "Wind": 900 + 300 * random.random(),
               "Hydro": 600 + 400 * (1 - sun), "Gas": 250, "Battery": 120 * (1 - sun)}
        m = self._mix(mix)
        cur = {"rrp": hist[0][2], "price_c": round(hist[0][2] / 10, 2), "at": hist[0][0] + 300,
               "price_status": "FIRM", "demand_mw": round(hist[0][3]), "net_interchange_mw": -640,
               "scheduled_gen_mw": 6900, "semischeduled_gen_mw": 2100, "apc": False, "suspended": False}
        if self.region == "NSW1":
            cur["net_interchange_mw"] = -1380
        ics = [{"name": "N-Q-MNSP1", "mw": -48, "export_limit": 115, "import_limit": -122},
               {"name": "NSW1-QLD1", "mw": -512, "export_limit": 700, "import_limit": -1200},
               {"name": "VIC1-NSW1", "mw": 820, "export_limit": 1400, "import_limit": -1000}]
        # the other regions, loosely shaped like the real NEM
        # net interchange (positive = exporting) matches the interconnector flows below
        shape = {"NSW1": (7400, 0, 1.00, -1380), "QLD1": (6600, 0.10, 0.93, 560), "VIC1": (5200, 0.05, 0.85, 1000),
                 "SA1": (1500, 0.25, 1.20, -190), "TAS1": (1100, 0.85, 0.70, 10)}
        rows, mixes = {}, {}
        for r, (dem, base_ren, pf, ni) in shape.items():
            p = round(rrp(t) * pf, 2)
            d = round(dem * (cur["demand_mw"] / 7400 if cur["demand_mw"] else 1))
            rows[r] = {"rrp": p, "price_c": round(p / 10, 2), "price_status": "FIRM", "demand_mw": d,
                       "net_interchange_mw": ni, "apc": False}
            ren = min(98.0, base_ren * 100 + (m["renewables_pct"] or 0) * (1 - base_ren))
            mixes[r] = {"total_mw": d + ni, "renewables_pct": round(ren, 1),
                        "top_fuel": "Hydro" if r == "TAS1" else "Solar" if sun > 0.6 else "Wind" if r == "SA1" else
                        "Brown coal" if r == "VIC1" else "Black coal"}
        rows[self.region] = {**rows.get(self.region, {}), "rrp": cur["rrp"], "price_c": cur["price_c"]}
        all_ics = ics + [{"name": "V-SA", "mw": 150, "export_limit": 600, "import_limit": -550},
                         {"name": "V-S-MNSP1", "mw": 40, "export_limit": 220, "import_limit": -200},
                         {"name": "T-V-MNSP1", "mw": 10, "export_limit": 478, "import_limit": -478},
                         {"name": "NSW1-SA1", "mw": -120, "export_limit": 400, "import_limit": -350}]
        with self.lock:
            self.state.update(status="ok", error=None, current=cur, forecast=forecast, interconnectors=ics,
                              updated=now, region_rows=rows, region_mix=mixes, all_interconnectors=all_ics, **m)

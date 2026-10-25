"""Import past daily energy from a mySigen export, so history starts when the system did.

In the mySigen app: Statistics (or the station's data page) -> Energy -> choose Day view ->
export. The file is "stationData-...-Energy.xlsx" with one row per day:

  Date | Solar Production Energy (kWh) | Load Consumed Energy (kWh) | Battery Charge Energy (kWh) |
  Battery Discharge Energy (kWh) | To EVAC (kWh) | Grid Imported Energy (kWh) |
  Grid Exported Energy (kWh) | Revenue(A$)

Put it (or a CSV with the same columns) in the data folder's "import" folder and restart the
collector.

Retailer bills for days older than the retailer's own service reaches (Amber: about 90 days)
can go in the same folder as a CSV, one row per bill, amounts in dollars incl. GST:

  start,end,energy_cost,export_credit,avg_buy_c
  2026-05-10,2026-05-31,18.94,30.48,19.35

energy_cost is the usage and network demand charges (not the supply charge or membership fee,
which come from RETAILER_SUPPLY_C_DAY / RETAILER_FEE_MONTHLY); export_credit is what the bill
credits for exports; avg_buy_c (optional) is the bill's average usage rate in c/kWh. Each bill is
spread over its days that have no costs yet, in proportion to each day's grid use and exports
from the imported mySigen days, after taking off days whose costs are already known. Each file is imported once and renamed to "*.imported". Imported days only fill in
days the collector didn't record itself; they never replace its own readings. Other exports
(the 5-minute "Power" file, or yearly totals) are recognised and skipped.

Standard library only: an .xlsx file is a zip of XML.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
COLUMNS = {  # mySigen header -> our field
    "solar production energy": "pv", "load consumed energy": "load", "battery charge energy": "charge",
    "battery discharge energy": "discharge", "to evac": "ev", "grid imported energy": "import",
    "grid exported energy": "export",
}
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path):
    """Rows of the first worksheet as lists of strings."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        root = ET.fromstring(z.read(sheets[0]))
    rows = []
    for r in root.iter(f"{{{NS['m']}}}row"):
        vals = {}
        for c in r.findall("m:c", NS):
            t, v = c.get("t"), c.find("m:v", NS)
            if t == "inlineStr":
                text = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
            elif v is None:
                text = ""
            elif t == "s":
                text = shared[int(v.text)]
            else:
                text = v.text or ""
            vals[_col_index(c.get("r"))] = text
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    return rows


def read_rows(path):
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        return read_xlsx(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def parse_daily(rows):
    """{day: {pv, load, ev, charge, discharge, import, export}} from a mySigen daily Energy export,
    or raises ValueError saying what the file is instead."""
    if not rows:
        raise ValueError("the file is empty")
    head = [re.sub(r"\s*\(.*?\)\s*", "", h or "").strip().lower() for h in rows[0]]
    fields = {i: COLUMNS[h] for i, h in enumerate(head) if h in COLUMNS}
    if not head or head[0] != "date" or len(fields) < 5:
        if any("power" in h for h in head):
            raise ValueError("this is the 5-minute Power export; the daily Energy export is the one to import")
        raise ValueError("it doesn't look like a mySigen Energy export (no Date and energy columns)")
    out = {}
    for r in rows[1:]:
        day = (r[0] or "").strip()[:10] if r else ""
        if not DAY.match(day):
            continue  # "total", or yearly / monthly rows
        rec = {}
        for i, f in fields.items():
            try:
                rec[f] = max(0.0, float(r[i]))
            except (ValueError, IndexError, TypeError):
                rec[f] = None  # "--"
        if any(v is not None for v in rec.values()):
            out[day] = rec
    if not out:
        raise ValueError("it has no daily rows (export the Day view, not Month, Year or Lifetime)")
    return out


def parse_bills(rows):
    """[{start, end, energy_cost, export_credit, avg_buy_c}] from a bills CSV, or None if it isn't one."""
    if not rows:
        return None
    head = [(h or "").strip().lower() for h in rows[0]]
    if not {"start", "end", "energy_cost", "export_credit"} <= set(head):
        return None
    ix = {h: i for i, h in enumerate(head)}
    out = []
    for r in rows[1:]:
        if not r or not DAY.match((r[ix["start"]] or "").strip()):
            continue
        num = lambda k: float(r[ix[k]]) if k in ix and ix[k] < len(r) and (r[ix[k]] or "").strip() else None
        b = {"start": r[ix["start"]].strip(), "end": r[ix["end"]].strip(), "energy_cost": num("energy_cost") or 0.0,
             "export_credit": num("export_credit") or 0.0, "avg_buy_c": num("avg_buy_c")}
        if not DAY.match(b["end"]) or b["end"] < b["start"]:
            raise ValueError(f"bill {b['start']}: end date isn't a date after the start")
        out.append(b)
    if not out:
        raise ValueError("the bills file has no rows")
    return out


def import_folder(store, data_dir, log=print):
    """Import every new file in DATA_DIR/import (mySigen days first, then bills). Returns days imported."""
    folder = Path(data_dir) / "import"
    if not folder.is_dir():
        return 0
    total = 0
    files = [f for f in sorted(folder.iterdir()) if f.suffix.lower() in (".xlsx", ".csv") and not f.name.startswith(".")]
    bills = []
    for f in files:
        if f.suffix.lower() == ".csv":
            try:
                b = parse_bills(read_rows(f))
            except (ValueError, OSError, IndexError) as e:
                log(f"Import: skipped {f.name}: {e}")
                f.rename(f.with_name(f.name + ".skipped"))
                continue
            if b is not None:
                bills.append((f, b))
                continue
        try:
            days = parse_daily(read_rows(f))
        except (ValueError, zipfile.BadZipFile, ET.ParseError, OSError, KeyError, IndexError) as e:
            log(f"Import: skipped {f.name}: {e}")
            f.rename(f.with_name(f.name + ".skipped"))
            continue
        n = store.save_imported_days(days, source=f"mySigen {f.name}")
        first, last = min(days), max(days)
        log(f"Import: {f.name}: {len(days)} days, {first} to {last} ({n} new or updated)")
        f.rename(f.with_name(f.name + ".imported"))
        total += n
    for f, bs in bills:  # after the days, so each bill can be spread over them
        for b in bs:
            r = store.apply_bill(b)
            log(f"Import: {f.name}: bill {b['start']} to {b['end']}: ${b['energy_cost']:.2f} energy, "
                f"${b['export_credit']:.2f} credit, spread over {r['filled']} days"
                + (f" ({r['known']} days already had costs)" if r["known"] else "")
                + ("" if r["filled"] else "; no days without costs and with imported figures to spread it over"))
        f.rename(f.with_name(f.name + ".imported"))
    return total

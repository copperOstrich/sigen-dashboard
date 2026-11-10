"""Unit tests for the parts with rules in them. Standard library only:
    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
_TMP = tempfile.mkdtemp(prefix="sigen-test-")
os.environ.setdefault("DATA_DIR", _TMP)  # before anything reads the settings

import advice  # noqa: E402
import importer  # noqa: E402
import limits  # noqa: E402
import money  # noqa: E402
import mqtt  # noqa: E402
import settings  # noqa: E402
import updates  # noqa: E402
import notify  # noqa: E402
import targets  # noqa: E402
import whatif  # noqa: E402
import goodtime  # noqa: E402
import gapfill  # noqa: E402
import aemo  # noqa: E402
from backup import Backups  # noqa: E402


class SettingsChecks(unittest.TestCase):
    def ok(self, key, value, want=None):
        self.assertEqual(settings.validate(key, value), value if want is None else want)

    def bad(self, key, value):
        with self.assertRaises(ValueError):
            settings.validate(key, value)

    def test_numbers(self):
        self.ok("SIGEN_PORT", "502")
        self.bad("SIGEN_PORT", "abc")
        self.bad("SIGEN_PORT", "70000")
        self.ok("POLL_SECONDS", "2.5")
        self.bad("POLL_SECONDS", "1")

    def test_shapes(self):
        self.ok("SOLAR_PLANES", "22:90:6.6,22:270:6.6")
        self.bad("SOLAR_PLANES", "22:90")
        self.ok("APPLIANCES", "Pool pump:1.1:4")
        self.bad("APPLIANCES", "Pool:x:4")
        self.ok("BACKUP_TIME", "2:30", "02:30")
        self.bad("BACKUP_TIME", "25:00")
        self.ok("SIGEN_CHARGER_UNIT", "Plant", "plant")
        self.bad("SIGEN_CHARGER_UNIT", "300")
        self.ok("TZ", "Australia/Sydney")
        self.bad("TZ", "Mars/Olympus")
        self.ok("SETTINGS_EDIT_FROM", "None", "none")
        self.bad("SETTINGS_EDIT_FROM", "not-a-range")

    def test_dated_supply(self):
        self.ok("RETAILER_SUPPLY_C_DAY", "119.6")
        self.ok("RETAILER_SUPPLY_C_DAY", "2026-05-10:108.96,2026-07-01:119.6")
        self.bad("RETAILER_SUPPLY_C_DAY", "2026-13-01:100")
        self.bad("RETAILER_SUPPLY_C_DAY", "cheap")

    def test_together(self):
        self.assertIn("SIGEN_HOST", settings.check_together({"SIGEN_SIMULATE": "0"}))
        self.assertNotIn("SIGEN_HOST", settings.check_together({"SIGEN_SIMULATE": "1"}))
        self.assertIn("INSULATION_WARN_MOHM", settings.check_together({"SIGEN_SIMULATE": "1", "INSULATION_WARN_MOHM": "0.2",
                                                                       "INSULATION_MIN_MOHM": "0.37"}))
        self.assertIn("SITE_LON", settings.check_together({"SIGEN_SIMULATE": "1", "SITE_LAT": "-33.87"}))

    def test_write_keeps_comments_and_order(self):
        p = Path(tempfile.mkdtemp()) / "sigen.env"
        p.write_text("# top\nA=1   # keep me\nB=\"x\"\n# middle\nA=2\n")
        settings.write_file(p, {"A": "5", "B": "has # hash", "C": "new"})
        text = p.read_text()
        self.assertIn("# top", text)
        self.assertIn("A=5   # keep me", text)
        self.assertEqual(settings.read_file(p), {"A": "5", "B": "has # hash", "C": "new"})
        self.assertTrue((p.parent / "sigen.env.bak").exists())

    def test_access(self):
        self.assertTrue(settings.access("127.0.0.1", "")[0])
        self.assertFalse(settings.access("127.0.0.1", "10.0.0.0/8")[0])
        self.assertTrue(settings.access("10.1.2.3", "10.0.0.0/8")[0])
        self.assertFalse(settings.access("10.1.2.3", "none")[0])
        self.assertFalse(settings.access("not an address", "")[0])

    def test_describe_hides_secrets_away_from_home(self):
        fv = {"AMBER_API_KEY": "psk_secret123456", "SIGEN_HOST": "10.0.0.5"}
        away = settings.describe("/x/sigen.env", fv, {}, {}, editable=False)
        flat = {s["key"]: s for g in away["groups"] for s in g["settings"]}
        self.assertIsNone(flat["AMBER_API_KEY"]["value"])
        self.assertEqual(flat["AMBER_API_KEY"]["hint"], "")
        self.assertIsNone(flat["SIGEN_HOST"]["value"])
        self.assertNotIn("psk_secret", str(away))
        home = settings.describe("/x/sigen.env", fv, {}, {}, editable=True)
        flat = {s["key"]: s for g in home["groups"] for s in g["settings"]}
        self.assertEqual(flat["AMBER_API_KEY"]["hint"], "•••• 3456")
        self.assertEqual(flat["SIGEN_HOST"]["value"], "10.0.0.5")


class Importers(unittest.TestCase):
    def test_bills(self):
        rows = [["start", "end", "energy_cost", "export_credit", "avg_buy_c"],
                ["2026-05-10", "2026-05-31", "18.94", "30.48", "19.35"]]
        self.assertEqual(importer.parse_bills(rows), [{"start": "2026-05-10", "end": "2026-05-31", "energy_cost": 18.94,
                                                       "export_credit": 30.48, "avg_buy_c": 19.35}])
        self.assertIsNone(importer.parse_bills([["Date", "Solar"]]))
        with self.assertRaises(ValueError):
            importer.parse_bills([["start", "end", "energy_cost", "export_credit"], ["2026-05-10", "2026-05-01", "1", "1"]])

    def test_amber_usage(self):
        head = ["Start Time", "End Time", "NEM Time", "Day", "Channel Type", "Channel Identifier", "Price", "Usage",
                "Cost", "Read Quality"]
        rows = [head,
                ["2026-10-12 15:15:01", "", "", "2026-10-12", "general", "E1", "30", "0.5", "15", "ACTUAL"],
                ["2026-10-12 15:15:01", "", "", "2026-10-12", "feedIn", "B1", "-2476", "0.885", "-2191.26", "ACTUAL"],
                ["2026-10-13 00:00:01", "", "", "2026-10-13", "general", "E1", "20", "1.0", "20", "ESTIMATED"]]
        out = importer.parse_amber_usage(rows)
        self.assertAlmostEqual(out["2026-10-12"]["export_cost"], -2191.26)
        self.assertAlmostEqual(out["2026-10-12"]["import_kwh"], 0.5)
        self.assertFalse(out["2026-10-12"]["estimated"])
        self.assertTrue(out["2026-10-13"]["estimated"])
        self.assertIsNone(importer.parse_amber_usage([["start", "end"]]))

    def test_mysigen_daily(self):
        rows = [["Date", "Solar Production Energy (kWh)", "Load Consumed Energy (kWh)", "Battery Charge Energy (kWh)",
                 "Battery Discharge Energy (kWh)", "To EVAC (kWh)", "Grid Imported Energy (kWh)", "Grid Exported Energy (kWh)"],
                ["2026-04-15", "40", "15", "10", "9", "0", "1", "25"], ["Total", "40", "15", "10", "9", "0", "1", "25"]]
        out = importer.parse_daily(rows)
        self.assertEqual(list(out), ["2026-04-15"])
        self.assertEqual(out["2026-04-15"]["export"], 25)
        with self.assertRaises(ValueError):
            importer.parse_daily([["Time", "Power (W)"]])


class Money(unittest.TestCase):
    def test_dated(self):
        r = money.parse_dated("2026-07-01:119.6,2026-05-10:108.96")
        self.assertEqual(r, [("2026-05-10", 108.96), ("2026-07-01", 119.6)])
        self.assertEqual(money.dated_on(r, "2026-04-01"), 108.96)  # before the first date: the first
        self.assertEqual(money.dated_on(r, "2026-06-30"), 108.96)
        self.assertEqual(money.dated_on(r, "2026-07-01"), 119.6)
        self.assertEqual(money.dated_on(money.parse_dated("119.6"), "2026-01-01"), 119.6)
        self.assertEqual(money.parse_dated(""), [])
        self.assertIsNone(money.dated_on([], "2026-01-01"))

    def test_bill_scaling_adds_up(self):
        import sigen_collector as sc
        out = [{"day": "2026-05-%02d" % d, "bought": b, "earned": e, "import": 1, "export": 5}
               for d, b, e in [(10, 1.0, 2.0), (11, 0.5, 1.0), (12, 0.0, 0.0), (13, None, None)]]
        sc.Store._apply_bill_to(out, {"start": "2026-05-10", "end": "2026-05-12", "energy_cost": 18.94, "export_credit": 30.48})
        period = out[:3]
        self.assertAlmostEqual(sum(e["bought"] for e in period), 18.94, places=6)
        self.assertAlmostEqual(sum(e["earned"] for e in period), 30.48, places=6)
        self.assertTrue(all(e["cost_source"] == "bill" for e in period))
        self.assertIsNone(out[3]["bought"])  # no figures that day: left alone


class HourlyPricing(unittest.TestCase):
    def test_imported_day_priced_by_hour(self):
        import time as _t
        from datetime import datetime, timedelta
        import sigen_collector as sc
        st = sc.Store(Path(tempfile.mkdtemp()) / "t.db")
        tz = sc.TZ
        # yesterday from the collector: home 200 W all day, 2,200 W from 6 to 9 pm (evening-heavy)
        y = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        rows = []
        for m in range(1440):
            ts = int((y + timedelta(minutes=m)).timestamp())
            load = 2200.0 if 18 <= (y + timedelta(minutes=m)).hour < 21 else 200.0
            rows.append((ts, y.date().isoformat(), 0, load, 0, load, 50, 0, 0, 0, 0, 0, 0, None, None, None))
        st.insert(rows)
        # an imported day a month back: 10 kWh, prices 10c all day but 50c from 6 to 9 pm
        d = (y - timedelta(days=30)).date().isoformat()
        st.save_imported_days({d: {"pv": 0, "load": 10.0, "ev": 0, "charge": 0, "discharge": 0, "import": 10, "export": 0}})
        d0 = datetime.fromisoformat(d).replace(tzinfo=tz)
        iv = [(int((d0 + timedelta(minutes=5 * i)).timestamp()), 50.0 if 18 <= (d0 + timedelta(minutes=5 * i)).hour < 21 else 10.0)
              for i in range(288)]
        st.save_csv_prices(iv)
        st.save_amber_days({d: {"import_kwh": 10, "import_cost": 0, "export_kwh": 0, "export_cost": 0, "estimated": False,
                                "avg_buy_c": 15.0}}, source="csv")
        cost = st.no_solar_cost(d)
        # evening share = 3 x 2200 / (21 x 200 + 3 x 2200) = 0.611; 10 kWh: 6.11 kWh at 50c + 3.89 at 10c = $3.44
        self.assertAlmostEqual(cost, 3.44, delta=0.05)
        self.assertGreater(cost, 10 * 15.0 / 100)  # more than the flat daily average gives


class Trading(unittest.TestCase):
    def test_spike_caught(self):
        import time as _t
        import sigen_collector as sc
        import trading
        st = sc.Store(Path(tempfile.mkdtemp()) / "t.db")
        t0 = int(_t.time()) // 60 * 60 - 3 * 86400
        rows = []
        for m in range(3 * 1440):
            ts = t0 + m * 60
            spike = 2000 <= m < 2012
            # normal: home on the battery at 500 W; spike: battery sells 6 kW at $10/kWh feed-in
            batt, grid, sell = (-6500.0, -6000.0, -1000.0) if spike else (-500.0, 0.0, -5.0)
            rows.append((ts, "d", 0, 500, batt, grid, 80.0, 0, 0, 0, 0, 0, 0, 30.0, sell, None))
        st.insert(rows)
        live = {"battery": {"rated_kwh": 40.0, "cutoff_low": 10.0}}
        res = trading.trading(st, live, sc.TZ, days=5, baseload_w=400, now=t0 + 3 * 86400)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["spike_count"], 1)
        s = res["spikes"][0]
        self.assertEqual(s["minutes"], 12)
        self.assertAlmostEqual(s["earned"], 12, delta=0.1)  # 1.2 kWh at $10
        self.assertTrue(s["caught"])
        self.assertEqual(res["what_if"][0]["reserve"], 20)
        self.assertAlmostEqual(res["what_if"][0]["outage_hours"], 10.0)  # 4 kWh at 400 W


class VppCurtailment(unittest.TestCase):
    """Solar limits: the VPP reason, confirmed by the control registers or inferred."""

    def run_day(self, minute):
        """One day of minutes from minute(m) -> (pv, battery, grid, soc, sell, pv_cap, exp_cap, ems); model 6 kW all day."""
        import sigen_collector as sc
        from limits import Limits
        from registers import control_values
        st = sc.Store(Path(tempfile.mkdtemp()) / "t.db")
        day = "2026-11-02"
        t0 = int(datetime.fromisoformat(day).replace(tzinfo=sc.TZ).timestamp())
        rows = []
        for m in range(10 * 60, 14 * 60):  # 10 am to 2 pm
            pv, batt, grid, soc, sell, pvc, expc, ems = minute(m)
            rows.append((t0 + m * 60, day, pv, 1000, batt, grid, soc, 0, 0, 0, 0, 0, 0, 20.0, sell, None, pvc, expc, ems))
        st.insert(rows)
        lim = Limits(st, sc.TZ, expected=lambda h: 6000.0, export_limit_kw=5)
        lim.caps["pv_w"] = 10000
        res, modelled = lim.analyse_day(day)
        self.assertTrue(modelled)
        self.assertIsNone(control_values({"pv_max_power_kw": 0xFFFFFFFF / 1000})["pv_max_power_kw"])
        return res

    def test_confirmed_by_solar_limit(self):
        # 11:00-12:00 the VPP caps solar at 1 kW (the home's use): 60 confirmed minutes, 5 kWh short
        cap = lambda m: 11 * 60 <= m < 12 * 60
        res = self.run_day(lambda m: (1000, 0, 0, 60, -5.0, 1000, None, 6) if cap(m) else (6000, 2000, -3000, 60, -5.0, None, None, 6))
        self.assertEqual(res["vpp"]["minutes"], 60)
        self.assertEqual(res["vpp"]["confirmed"], 60)
        self.assertAlmostEqual(res["vpp"]["kwh"], 5.0, places=2)
        self.assertAlmostEqual(res["vpp"]["value"], 0.25, places=2)  # 5 kWh at 5c feed-in missed
        self.assertEqual(sum(res[r]["minutes"] for r in ("export", "negative", "inverter", "full")), 0)

    def test_inferred_when_house_only(self):
        # no limit reported, but solar held to the house with the battery idle and room in it
        res = self.run_day(lambda m: (1000, 0, 0, 60, -5.0, None, None, 6) if m < 11 * 60 else (6000, 2000, -3000, 60, -5.0, None, None, 6))
        self.assertEqual(res["vpp"]["minutes"], 60)
        self.assertEqual(res["vpp"]["confirmed"], 0)

    def test_cloud_not_counted(self):
        # cloud: solar short but the house draws from the grid, or the battery takes a smaller surplus
        res = self.run_day(lambda m: (500, 0, 500, 60, -5.0, None, None, 6) if m < 12 * 60 else (2000, 1000, 0, 60, -5.0, None, None, 6))
        self.assertEqual(res["vpp"]["minutes"], 0)

    def test_not_inferred_outside_vpp_mode(self):
        res = self.run_day(lambda m: (1000, 0, 0, 60, -5.0, None, None, 0))
        self.assertEqual(res["vpp"]["minutes"], 0)

    def test_negative_price_stays_its_own_reason(self):
        res = self.run_day(lambda m: (1000, 0, 0, 60, 3.0, 1000, None, 6))
        self.assertEqual(res["negative"]["minutes"], 240)
        self.assertEqual(res["vpp"]["minutes"], 0)

    def test_zero_export_limit_needs_idle_battery(self):
        # export capped at 0: confirmed only while the battery isn't soaking up the rest
        res = self.run_day(lambda m: (1000, 0, 0, 60, -5.0, None, 0, 0) if m < 11 * 60 else (3000, 2000, 0, 60, -5.0, None, 0, 0))
        self.assertEqual(res["vpp"]["minutes"], 60)
        self.assertEqual(res["vpp"]["confirmed"], 60)


class Forecasts(unittest.TestCase):
    def test_temperature_model(self):
        import time as _t
        from datetime import datetime, timedelta
        import sigen_collector as sc
        from outlook import Outlook
        st = sc.Store(Path(tempfile.mkdtemp()) / "t.db")
        tz = sc.TZ
        now = _t.time()
        d0 = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
        temps, rows = {}, []
        for d in range(14):
            mean_t = 22 + d  # 22..35 degrees: cooling degrees 0..11
            kwh = 12 + 1.5 * Outlook.degrees(mean_t)
            for h in range(24):
                hs = int((d0 + timedelta(days=d, hours=h)).timestamp())
                temps[hs] = mean_t
                for m in range(0, 60, 1):
                    rows.append((hs + m * 60, "d", 0, kwh * 1000 / 24, 0, 0, 50, 0, 0, 0, 0, 0, 0, None, None, None))
        st.insert(rows)
        m = Outlook(st, tz)._temp_model(now, temps)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m["b"], 1.5, delta=0.05)
        self.assertGreater(m["r"], 0.95)
        self.assertIsNone(Outlook(st, tz)._temp_model(now, {h: 20 for h in temps}))  # weather never changed

    def test_ev_plan_cheapest(self):
        import evplan
        t0 = 1_800_000_000 // 900 * 900
        rows = [{"t": t0 + i * 900, "grid_out": 2.0 if 20 <= i < 28 else 0.0} for i in range(48)]  # 8 kW spare, steps 20-27
        fc = [{"t": t0, "end": t0 + 48 * 900, "buy": 40.0, "sell": 5.0}]
        p = evplan.ev_plan(rows, fc, kwh=10, by_ts=t0 + 48 * 900, kw=7, now=t0)
        self.assertTrue(p["enabled"])
        self.assertAlmostEqual(p["kwh"], 10, places=3)
        self.assertTrue(all(t0 + 20 * 900 <= w["start"] < t0 + 28 * 900 for w in p["windows"]))
        self.assertAlmostEqual(p["avg_c"], 5.0, places=1)  # all from spare solar
        self.assertGreater(p["saving"], 0)
        short = evplan.ev_plan(rows, fc, kwh=100, by_ts=t0 + 4 * 900, kw=7, now=t0)
        self.assertGreater(short["short_kwh"], 0)


class Limits(unittest.TestCase):
    def test_ceiling(self):
        flat = [5000.0] * 60 + [3000.0 + i for i in range(500)]
        self.assertEqual(limits.Limits._ceiling(flat, 0.03), 5000.0)
        self.assertIsNone(limits.Limits._ceiling([3000.0 + i * 10 for i in range(500)], 0.03))  # no plateau


class Appliances(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(advice.parse("Dryer:2.5:1.5, bad, X:0:1"), [{"name": "Dryer", "kw": 2.5, "hours": 1.5}])
        self.assertEqual(len(advice.parse(None)), 3)  # the defaults
        self.assertEqual(advice.parse(""), [])

    def test_picks_spare_solar(self):
        t0 = 1_800_000_000 // 900 * 900
        rows = [{"t": t0 + i * 900, "soc": 50, "grid_out": 0.75 if 8 <= i < 16 else 0.0,
                 "grid_in": 0.0 if 8 <= i < 16 else 0.25} for i in range(40)]
        fc = [{"t": t0, "end": t0 + 40 * 900, "buy": 30.0, "sell": 5.0}]
        res = advice.appliance_advice(rows, {"reserve": 10}, fc, [{"name": "Dryer", "kw": 2.5, "hours": 1.0}], t0)
        best = res["appliances"][0]["best"]
        self.assertTrue(t0 + 8 * 900 <= best["start"] < t0 + 16 * 900)
        self.assertEqual(best["why"], "mostly spare solar")


class Mqtt(unittest.TestCase):
    def test_packet_length(self):
        self.assertEqual(mqtt._packet(0x30, b"x" * 10)[:2], bytes([0x30, 10]))
        self.assertEqual(mqtt._packet(0x30, b"x" * 200)[:3], bytes([0x30, 0xC8, 0x01]))  # 200 = 0x48 | 0x80, 1
        self.assertEqual(mqtt._str("ab"), b"\x00\x02ab")


class BackupPruning(unittest.TestCase):
    def test_keeps_days_then_weeks(self):
        folder = Path(tempfile.mkdtemp())
        from datetime import date, timedelta
        start = date(2026, 1, 1)
        for i in range(60):
            (folder / f"sigen-{(start + timedelta(days=i)).isoformat()}.db.gz").write_bytes(b"x")
        (folder / "other-2026-01-01.db.gz").write_bytes(b"x")  # someone else's: left alone
        b = Backups(folder / "sigen.db", folder, tz=None, keep_daily=7, keep_weekly=4)
        b.prune()
        kept = sorted(f.name for f in folder.glob("sigen-*.gz"))
        self.assertEqual(len(kept), 11)
        self.assertEqual(kept[-1], f"sigen-{(start + timedelta(days=59)).isoformat()}.db.gz")
        self.assertTrue((folder / "other-2026-01-01.db.gz").exists())



class BillsAndUploads(unittest.TestCase):
    def test_clean_bill(self):
        b = importer.clean_bill({"start": "2026-09-01", "end": "2026-09-30", "energy_cost": "$1,040.50", "export_credit": "0"})
        self.assertEqual((b["energy_cost"], b["export_credit"], b["avg_buy_c"]), (1040.5, 0.0, None))
        with self.assertRaises(ValueError) as e:
            importer.clean_bill({"start": "2026-09-30", "end": "2026-09-01", "energy_cost": "x", "export_credit": ""})
        self.assertEqual(set(e.exception.args[0]), {"end", "energy_cost", "export_credit"})
        with self.assertRaises(ValueError) as e:
            importer.clean_bill({"start": "2026-09-01", "end": "2026-09-30", "energy_cost": 1, "export_credit": 1},
                                [{"start": "2026-08-15", "end": "2026-09-01"}])
        self.assertIn("Overlaps", e.exception.args[0]["start"])

    def test_safe_name(self):
        self.assertEqual(importer.safe_name("../../Amber Usage 2026.csv"), "Amber Usage 2026.csv")
        for bad in ("evil.sh", "x.csv.exe", ""):
            with self.assertRaises(ValueError):
                importer.safe_name(bad)

    def test_newer_version(self):
        self.assertTrue(updates.newer("1.20.0", "1.19.0"))
        self.assertTrue(updates.newer("v1.19.10", "1.19.9"))
        self.assertFalse(updates.newer("1.19.0", "1.19.0"))
        self.assertFalse(updates.newer("", "1.19.0"))



class PhoneAlerts(unittest.TestCase):
    def setUp(self):
        self.sent, self.state = [], {"status": "ok", "alarms": [], "system": {"feeds": []}}
        self.file = Path(tempfile.mkdtemp()) / "notify-state.json"

    def make(self, **kw):
        from zoneinfo import ZoneInfo
        return notify.Notifier("home-energy-abcdefghijkmnpqrstu", get_state=lambda: self.state, state_file=self.file,
                               tz=ZoneInfo("Australia/Sydney"), log=lambda *a: None, post=self.sent.append, **kw)

    def test_alarm_sent_once_then_cleared_and_kept_over_restart(self):
        n, t = self.make(), 1_800_000_000
        self.state["alarms"] = [{"source": "Battery", "severity": "fault", "message": "Cell over-temperature",
                                 "register": 30605, "bit": 3}]
        n.tick(t)
        self.assertEqual(self.sent, [])           # waits two minutes in case it's a blip
        n.tick(t + 130)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["priority"], 4)
        self.assertIn("Cell over-temperature", self.sent[0]["message"])
        n.tick(t + 200)
        n2 = self.make()                          # a restart: nothing sent again
        n2.tick(t + 400); n2.tick(t + 600)
        self.assertEqual(len(self.sent), 1)
        self.state["alarms"] = []
        n2.tick(t + 700)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1]["title"].startswith("Home energy: Cleared"))
        self.assertEqual(self.sent[1]["priority"], 2)

    def test_kinds_feeds_wait_an_hour_and_storm(self):
        n, t = self.make(kinds={"feeds", "storm"}), 1_800_000_000
        self.state["alarms"] = [{"source": "Inverter", "severity": "fault", "message": "x", "register": 1, "bit": 0}]
        self.state["system"]["feeds"] = [{"name": "AEMO", "status": "error", "what": "wholesale market", "error": "timeout"}]
        self.state["storm"] = {"at": t + 20000, "until": t + 24000, "desc": "Thunderstorms", "soc": 35, "now": False}
        n.tick(t)
        self.assertEqual([m["title"] for m in self.sent], ["Home energy: Storm coming, battery low"])  # alarms are off
        n.tick(t + 3700)
        self.assertEqual(self.sent[-1]["title"], "Home energy: AEMO isn't updating")

    def test_quiet_hours_and_parsing(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime
        n = self.make(quiet=notify.parse_quiet("22:00-07:00"))
        night = datetime(2026, 11, 3, 23, 30, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
        n.send("x", "y", 3, now=night); n.send("x", "y", 4, now=night)
        self.assertEqual([m["priority"] for m in self.sent], [2, 4])
        self.assertEqual(notify.parse_weekly("Sun 18:00"), (6, 18, 0))
        self.assertIsNone(notify.parse_weekly("off"))
        self.assertEqual(notify.parse_kinds("alarms, storm, nonsense"), {"alarms", "storm"})
        for bad in ("someday 18:00", "sun 25:00"):
            with self.assertRaises(ValueError):
                notify.parse_weekly(bad)
        self.assertRegex(notify.new_topic(), r"^home-energy-[a-z2-9]{20}$")

    def test_weekly_once(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime
        n = self.make(weekly=(6, 18, 0), summary=lambda: ("Week to Saturday", "Solar 200 kWh."))
        sun = datetime(2026, 11, 8, 18, 5, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
        n.tick(sun - 600); n.tick(sun); n.tick(sun + 60)
        self.assertEqual([m["title"] for m in self.sent], ["Home energy: Week to Saturday"])



class Targets(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(targets.parse("90", 0, 100)[7], 90.0)
        t = targets.parse("12-2:95,3-11:70", 0, 100)
        self.assertEqual((t[12], t[1], t[2], t[3], t[11]), (95.0, 95.0, 95.0, 70.0, 70.0))
        self.assertEqual(targets.parse("$-50", -1e5, 1e5)[1], -50.0)
        for bad in ("120", "13-2:90", "abc"):
            with self.assertRaises(ValueError):
                targets.check(bad, "self")

    def test_progress_projects_the_month(self):
        from datetime import date

        class FakeStore:
            def monthly(self, n):
                return [{"month": "2026-11", "days": 10, "load": 200.0, "ev": 0.0, "import": 40.0,
                         "self_sufficiency": 80.0, "net": 10.0},
                        {"month": "2026-10", "days": 31, "load": 600.0, "ev": 0.0, "import": 60.0,
                         "self_sufficiency": 90.0, "net": -30.0}]

            def daily(self, n):
                return [{"day": f"2026-11-{d:02d}", "load": 20.0, "ev": 0.0, "import": 2.0, "cost": -1.0} for d in range(1, 11)]
        p = targets.progress(FakeStore(), date(2026, 11, 11), {m: 85.0 for m in range(1, 13)}, {m: 0.0 for m in range(1, 13)})
        self.assertGreater(p["self"]["projected"], 85)      # the rest of the month at 90% pulls it up
        self.assertTrue(p["self"]["on_track"])
        self.assertLess(p["cost"]["projected"], 0)          # $10 so far, $1 a day credit for 19.5 days
        self.assertEqual(p["months"][0]["self"]["met"], True)



class WhatIf(unittest.TestCase):
    def hours(self, pattern):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        t0 = int(datetime(2026, 11, 2, 0, tzinfo=tz).timestamp())  # a Monday
        return [(t0 + i * 3600, datetime.fromtimestamp(t0 + i * 3600, tz), pv, load, None, None, 0, 0)
                for i, (pv, load) in enumerate(pattern)]

    def test_battery_moves_solar_to_the_evening(self):
        h = self.hours([(0, 1)] * 10 + [(5, 1)] * 4 + [(0, 1)] * 10)   # 16 kWh spare at midday
        none = whatif.simulate(h, 0, 5, 5, 1.0, None, 1, None, "solar")
        batt = whatif.simulate(h, 10, 5, 5, 1.0, None, 1, None, "solar")
        self.assertAlmostEqual(sum(i for i, _ in none), 20)
        self.assertLess(sum(i for i, _ in batt), sum(i for i, _ in none) - 9)   # covers ~10 more hours
        self.assertAlmostEqual(sum(x for _, x in none) - sum(x for _, x in batt), 10, delta=0.01)  # emptied overnight, refilled

    def test_time_of_use_charges_in_the_cheap_hours(self):
        h = self.hours([(0, 1)] * 24)   # no solar at all
        rates = [10 if hr < 6 else 50 if 16 <= hr < 20 else 30 for hr in range(24)]
        fl = whatif.simulate(h, 10, 5, 5, 1.0, None, 1, None, "tou", rates)
        night = sum(fl[i][0] for i in range(6))
        self.assertGreater(night, 6)          # bought extra while it was cheap
        self.assertAlmostEqual(sum(fl[i][0] for i in range(16, 20)), 0)   # nothing bought in the peak
        plan = {"name": "t", "supply_c_day": 100, "usage": [{"hours": [0, 6], "c_kwh": 10}, {"hours": [16, 20], "c_kwh": 50},
                                                            {"c_kwh": 30}], "feed_in_c_kwh": 0}
        flat = whatif.simulate(h, 10, 5, 5, 1.0, None, 1, None, "solar")
        self.assertLess(whatif.price_plan(h, fl, plan, None), whatif.price_plan(h, flat, plan, None))



class GoodTime(unittest.TestCase):
    def live(self, grid_w, buy, sell, spike="none", ren=60, soc=50):
        return {"updated": 1_800_000_000, "price": {"buy": buy, "sell": sell, "spike": spike, "renewables": ren},
                "power": {"grid": grid_w}, "battery": {"soc": soc, "cutoff_low": 5}, "aemo": {}}

    def test_levels(self):
        g = lambda **kw: goodtime.good_time(self.live(**kw), [], [], [], fixed=(15, 35))
        self.assertEqual(g(grid_w=-3000, buy=25, sell=-2)["level"], "free")       # exporting, negative feed-in
        self.assertEqual(g(grid_w=500, buy=12, sell=5)["level"], "green")
        self.assertEqual(g(grid_w=500, buy=25, sell=5)["level"], "normal")
        self.assertEqual(g(grid_w=500, buy=40, sell=5)["level"], "dear")
        self.assertEqual(g(grid_w=500, buy=130, sell=90)["level"], "spike")
        self.assertEqual(g(grid_w=500, buy=40, sell=5, spike="spike")["level"], "spike")
        # green that isn't renewable enough, when green needs renewables
        r = goodtime.good_time(self.live(grid_w=500, buy=12, sell=5, ren=30), [], [], [], fixed=(15, 35),
                               green_rule="renewables", renewables_min=50)
        self.assertEqual(r["level"], "normal")

    def test_adaptive_scale_and_settings(self):
        hist = [{"buy": b} for b in list(range(10, 50)) * 3]
        green, dear = goodtime.thresholds("adaptive", (15, 35), hist)
        self.assertTrue(18 <= green <= 22 and 44 <= dear <= 47, (green, dear))
        self.assertEqual(goodtime.thresholds("adaptive", (15, 35), hist[:10]), (15, 35))  # under a day of prices
        self.assertEqual(goodtime.thresholds("fixed", (15, 35), hist), (15, 35))
        self.assertEqual(settings.validate("GOOD_TIME_FIXED", " 12, 40 "), "12,40")
        for bad in ("40,12", "1,20", "abc"):
            with self.assertRaises(ValueError):
                settings.validate("GOOD_TIME_FIXED", bad)

    def test_next_good_window(self):
        t0 = 1_800_000_000 // 900 * 900
        rows = [{"t": t0 + i * 900, "grid_in": 0.2, "grid_out": 0, "soc": 20} for i in range(16)]
        fc = [{"t": t0 + i * 900, "end": t0 + (i + 1) * 900, "buy": 40 if i < 8 or i >= 12 else 10, "sell": 5} for i in range(16)]
        live = self.live(grid_w=800, buy=40, sell=5)
        live["updated"] = t0
        r = goodtime.good_time(live, rows, fc, [], fixed=(15, 35), now=t0)
        self.assertEqual(r["level"], "dear")
        self.assertEqual((r["next_good"]["start"], r["next_good"]["end"]), (t0 + 8 * 900, t0 + 12 * 900))


class GapFill(unittest.TestCase):
    def setUp(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime
        self.tz = ZoneInfo("Australia/Sydney")
        self.t_last = int(datetime(2026, 11, 4, 21, 0, tzinfo=self.tz).timestamp())   # 9 pm
        self.now = int(datetime(2026, 11, 5, 9, 0, tzinfo=self.tz).timestamp())       # 9 am next day
        self.last = {"ts": self.t_last, "soc": 80.0, "total_pv_kwh": 1000.0, "total_load_kwh": 2000.0,
                     "total_import_kwh": 300.0, "total_export_kwh": 400.0, "total_charge_kwh": 500.0,
                     "total_discharge_kwh": 600.0}
        self.now_c = {"total_pv_kwh": 1006.0, "total_load_kwh": 2012.0, "total_import_kwh": 302.0,
                      "total_export_kwh": 401.0, "total_charge_kwh": 503.0, "total_discharge_kwh": 610.0}

    def test_totals_exact_and_split_at_midnight(self):
        rows, info = gapfill.plan(self.last, self.now_c, self.now, self.tz,
                                  daily_now={"pv": 6.0, "load": 7.5, "charge": 3.0, "discharge": 4.0},
                                  capacity_kwh=40, soc_now=60)
        self.assertEqual(info["minutes"], 12 * 60 - 1)
        self.assertTrue(info["midnight_split"])
        k = 60000.0
        tot = lambda i, day=None: sum(r[i] / k for r in rows if day is None or r[1] == day)
        self.assertAlmostEqual(tot(2), 6.0, 3)                        # solar, all of it
        self.assertAlmostEqual(tot(3), 12.0, 3)                       # home use
        self.assertAlmostEqual(tot(3, "2026-11-05"), 7.5, 3)          # after midnight: today's counter exactly
        self.assertAlmostEqual(tot(3, "2026-11-04"), 4.5, 3)
        self.assertAlmostEqual(sum(-min(0, r[4]) for r in rows) / k, 10.0, 1)   # discharged (net of charging)
        last = rows[-1]
        self.assertAlmostEqual(last[7], 1006.0, 3)                    # counters end where the inverter's are
        self.assertAlmostEqual(last[12], 610.0, 3)
        self.assertEqual(last[6], 60.0)                               # charge ends where it is now
        self.assertTrue(all(r[-1] == 1 for r in rows))                 # all marked estimated
        from datetime import datetime
        night = [r[2] for r in rows if datetime.fromtimestamp(r[0], self.tz).hour in (22, 23, 0, 1, 2, 3, 4)]
        self.assertEqual(sum(night), 0)                                # no solar at night
        ts = [r[0] for r in rows]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(b[7] >= a[7] for a, b in zip(rows, rows[1:])))   # counters only go up

    def test_not_filled(self):
        self.assertEqual(gapfill.plan(self.last, self.now_c, self.t_last + 120, self.tz)[1], "short")
        back = dict(self.now_c, total_pv_kwh=10.0)
        rows, why = gapfill.plan(self.last, back, self.now, self.tz)
        self.assertIsNone(rows)
        self.assertIn("backwards", why)
        self.assertIn("too long", gapfill.plan(self.last, self.now_c, self.t_last + 40 * 86400, self.tz)[1])


def _zip(name, text):
    import io as _io
    import zipfile as _zf
    b = _io.BytesIO()
    with _zf.ZipFile(b, "w", _zf.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return b.getvalue()


def _xlsx(rows):
    """A minimal .xlsx with the registration list's generator sheet (inline strings)."""
    import io as _io
    import zipfile as _zf
    from xml.sax.saxutils import escape
    cells = lambda r, i: "".join(f'<c r="{chr(65 + j)}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for j, v in enumerate(r))
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' +
             "".join(f'<row r="{i + 1}">{cells(r, i + 1)}</row>' for i, r in enumerate(rows)) + "</sheetData></worksheet>")
    wb = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          '<sheet name="Overview" sheetId="1" r:id="rId1"/><sheet name="PU and Scheduled Loads" sheetId="2" r:id="rId2"/>'
          "</sheets></workbook>")
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="x"/></Relationships>')
    b = _io.BytesIO()
    with _zf.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        z.writestr("xl/worksheets/sheet2.xml", sheet)
    return b.getvalue()


class AemoNemweb(unittest.TestCase):
    """The NEMWeb reader, against files laid out like AEMO's (headers copied from real ones)."""
    T = "2026/11/09 19:20:00"

    def dispatch_csv(self):
        price = ("I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP,EEP,ROP,APCFLAG,"
                 "MARKETSUSPENDEDFLAG,LASTCHANGED,PRICE_STATUS\n")
        for rid, rrp in (("NSW1", 106.44), ("QLD1", 120), ("SA1", 117.8), ("TAS1", 90), ("VIC1", 95)):
            price += f'D,DISPATCH,PRICE,5,"{self.T}",1,{rid},20261109184,0,{rrp},0,{rrp},0,0,"{self.T}",FIRM\n'
        price += f'D,DISPATCH,PRICE,5,"{self.T}",1,NSW1,20261109184,1,999,0,999,0,0,"{self.T}",FIRM\n'  # intervention run
        rs = ("I,DISPATCH,REGIONSUM,9,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,TOTALDEMAND,"
              "DISPATCHABLEGENERATION,NETINTERCHANGE,SS_SOLAR_CLEAREDMW,SS_WIND_CLEAREDMW\n"
              f'D,DISPATCH,REGIONSUM,9,"{self.T}",1,NSW1,1,0,8032.19,7271.24,-760.94,0,195.9\n'
              f'D,DISPATCH,REGIONSUM,9,"{self.T}",1,QLD1,1,0,6000,6600,600,0,100\n')
        ic = ("I,DISPATCH,INTERCONNECTORRES,3,SETTLEMENTDATE,RUNNO,INTERCONNECTORID,DISPATCHINTERVAL,INTERVENTION,"
              "METEREDMWFLOW,MWFLOW,MWLOSSES,MARGINALVALUE,VIOLATIONDEGREE,LASTCHANGED,EXPORTLIMIT,IMPORTLIMIT\n"
              f'D,DISPATCH,INTERCONNECTORRES,3,"{self.T}",1,NSW1-QLD1,1,0,-660,-663,1,0,0,"{self.T}",400.7,-1129.16\n'
              f'D,DISPATCH,INTERCONNECTORRES,3,"{self.T}",1,NSW1-SA1,1,0,-270,-269.6,1,0,0,"{self.T}",-69.9,-350\n'
              f'D,DISPATCH,INTERCONNECTORRES,3,"{self.T}",1,T-V-MNSP1,1,0,-104,-104.4,1,0,0,"{self.T}",-104.4,-317\n')
        return "C,NEMP.WORLD,DISPATCHIS\n" + price + rs + ic + "C,END OF REPORT\n"

    def predispatch_csv(self):
        h = ("I,PREDISPATCH,REGION_PRICES,2,PREDISPATCHSEQNO,RUNNO,REGIONID,PERIODID,INTERVENTION,RRP,EEP,LASTCHANGED,DATETIME\n"
             "I,PREDISPATCH,REGION_SOLUTION,9,PREDISPATCHSEQNO,RUNNO,REGIONID,PERIODID,INTERVENTION,TOTALDEMAND,DATETIME\n")
        rows = ""
        for i, (end, rrp) in enumerate((("2099/01/01 10:30:00", 118.02), ("2099/01/01 11:00:00", 143), ("2000/01/01 10:00:00", 50))):
            rows += f'D,PREDISPATCH,REGION_PRICES,2,1,1,NSW1,{i + 1:02d},0,{rrp},0,"x","{end}"\n'
            rows += f'D,PREDISPATCH,REGION_PRICES,2,1,1,QLD1,{i + 1:02d},0,1,0,"x","{end}"\n'
            rows += f'D,PREDISPATCH,REGION_SOLUTION,9,1,1,NSW1,{i + 1:02d},0,{8000 + i},"{end}"\n'
        return h + rows

    def registration(self):
        head = ["Participant", "Station Name", "Region", "Dispatch Type", "Category", "Classification",
                "Fuel Source - Primary", "Fuel Source - Descriptor", "Technology Type - Primary",
                "Technology Type - Descriptor", "Units", "Aggregation", "DUID"]
        gen = lambda duid, region, dtype, p, d: ["X", "Y", region, dtype, "Market", "Scheduled", p, d, "", "", "", "", duid]
        rows = [["NEM Registration list"], head,
                gen("COAL1", "NSW1", "Generating Unit", "Fossil", "Black Coal"),
                gen("BROWN1", "VIC1", "Generating Unit", "Fossil", "Brown Coal"),
                gen("GAS1", "NSW1", "Generating Unit", "Fossil", "Natural Gas / Diesel"),
                gen("MINE1", "QLD1", "Generating Unit", "Fossil", "Waste Coal Mine Gas"),
                gen("DSL1", "SA1", "Generating Unit", "Fossil", "Diesel"),
                gen("SOLAR1", "NSW1", "Generating Unit", "Solar", "Solar "),
                gen("WIND1", "NSW1", "Generating Unit", "Wind", "Wind"),
                gen("HYD1", "NSW1", "Generating Unit", "Hydro", "Water"),
                gen("PUMP1", "NSW1", "Load", "Hydro", "Water"),
                gen("BAT1", "NSW1", "Bidirectional Unit", "Battery Storage", "Grid"),
                gen("BIO1", "QLD1", "Generating Unit", "Renewable/ Biomass / Waste", "Bagasse")]
        rows += [gen(f"FILL{i}", "TAS1", "Generating Unit", "Hydro", "Water") for i in range(50)]
        return _xlsx(rows)

    def scada_csv(self):
        out = "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED\n"
        for duid, mw in (("COAL1", 5000), ("GAS1", 300), ("SOLAR1", 1500), ("WIND1", 700), ("HYD1", 200),
                         ("PUMP1", 150), ("BAT1", -80), ("UNKNOWN9", 10), ("BROWN1", 4000)):
            out += f'D,DISPATCH,UNIT_SCADA,1,"{self.T}",{duid},{mw},"{self.T}"\n'
        return out

    def fetcher(self):
        files = {
            "DispatchIS_Reports": ("PUBLIC_DISPATCHIS_202611091920_1.zip", self.dispatch_csv()),
            "PredispatchIS_Reports": ("PUBLIC_PREDISPATCHIS_202611091930_1.zip", self.predispatch_csv()),
            "Dispatch_SCADA": ("PUBLIC_DISPATCHSCADA_202611091920_1.zip", self.scada_csv())}
        calls = []

        def fetch(url, timeout=60):
            calls.append(url)
            if url == aemo.REGISTRATION_URL:
                return self.registration()
            for folder, (name, text) in files.items():
                if url == aemo.NEMWEB + folder + "/":
                    return (f'<pre><A HREF="/Reports/CURRENT/{folder}/PUBLIC_OLD_202611091915_0.zip">old</A>'
                            f'<A HREF="/Reports/CURRENT/{folder}/{name}">{name}</A></pre>').encode()
                if url.endswith("/" + name):
                    return _zip(name.replace(".zip", ".CSV"), text)
                if "PUBLIC_OLD" in url:
                    return _zip("x.CSV", text)
            raise aemo.AemoError("404")
        return fetch, calls

    class _Store:
        def __init__(self):
            self.rows = []

        def save_aemo(self, rows):
            self.rows += rows

        def aemo_history(self, region, hours, bucket=300):
            return []

    def test_fuel_groups(self):
        f = aemo.fuel_of
        self.assertEqual(f("Fossil", "Black Coal"), "Black coal")
        self.assertEqual(f("Fossil", "Waste Coal Mine Gas"), "Gas")
        self.assertEqual(f("Fossil", "Natural Gas / Fuel Oil"), "Gas")
        self.assertEqual(f("Fossil", "Kerosene"), "Liquid fuel")
        self.assertEqual(f("Battery Storage", "Grid", "Bidirectional Unit"), "Battery")
        self.assertEqual(f("Renewable/ Biomass / Waste and Fossil", "Bagasse and Diesel"), "Biomass")
        self.assertIsNone(f("Hydro", "Water", "Load"))

    def test_registration_list(self):
        d = aemo.parse_registration(self.registration())
        self.assertEqual(d["COAL1"], ["NSW1", "Black coal"])
        self.assertEqual(d["SOLAR1"], ["NSW1", "Solar"])
        self.assertEqual(d["PUMP1"], ["NSW1", None])
        self.assertEqual(d["BAT1"], ["NSW1", "Battery"])

    def test_poll_end_to_end(self):
        fetch, calls = self.fetcher()
        store = self._Store()
        with tempfile.TemporaryDirectory() as tmp:
            a = aemo.Aemo(store, "NSW1", log=lambda *x: None, data_dir=tmp, fetch=fetch)
            a.poll()
            s = a.state
            self.assertEqual(s["status"], "ok", s["error"])
            self.assertIsNone(s["error"])
            c = s["current"]
            self.assertEqual(c["rrp"], 106.44)            # pricing run, not the intervention run
            self.assertEqual(c["price_status"], "FIRM")
            self.assertEqual(c["demand_mw"], 8032.19)
            self.assertEqual(c["net_interchange_mw"], -760.94)
            self.assertEqual(c["semischeduled_gen_mw"], 195.9)
            self.assertFalse(c["suspended"])
            # history saved at the interval start, and the backfill read the older file too
            self.assertIn((int(aemo._nem_ts(self.T)) - 300, "NSW1", 106.44, 8032.19), store.rows)
            self.assertEqual(len(store.rows), 2)
            self.assertEqual({x["name"] for x in s["interconnectors"]}, {"NSW1-QLD1", "NSW1-SA1"})
            sa = next(x for x in s["all_interconnectors"] if x["name"] == "NSW1-SA1")
            self.assertEqual((sa["export_limit"], sa["import_limit"]), (-69.9, -350))
            self.assertEqual(set(s["region_rows"]), {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"})
            # forecast: only NSW, only future half hours, starting 30 minutes before each end
            self.assertEqual([f["rrp"] for f in s["forecast"]], [118.02, 143])
            self.assertEqual(s["forecast"][0]["end"] - s["forecast"][0]["t"], 1800)
            self.assertEqual(s["forecast"][0]["demand_mw"], 8000)
            # fuel mix: pumps and charging batteries left out, unknown generators counted
            mix = {f["fuel"]: f["mw"] for f in s["fuel"]}
            self.assertEqual(mix, {"Black coal": 5000, "Solar": 1500, "Wind": 700, "Gas": 300, "Hydro": 200})
            self.assertEqual(s["renewables_pct"], round(2400 / 7700 * 100, 1))
            self.assertEqual(s["unmatched_generators"], 1)
            self.assertEqual(s["region_mix"]["VIC1"]["top_fuel"], "Brown coal")
            snap = a.snapshot()
            self.assertEqual(snap["nem"]["demand_mw"], round(8032.19 + 6000))
            # generator list cached on disk; a second poll reads nothing new and refetches no list
            self.assertTrue((Path(tmp) / "aemo-generators.json").exists())
            n = len(calls)
            a.poll()
            self.assertEqual(a.state["status"], "ok")
            self.assertFalse([u for u in calls[n:] if u.endswith(".zip") or u == aemo.REGISTRATION_URL])
            # a fresh start reuses the saved generator list
            b = aemo.Aemo(store, "NSW1", log=lambda *x: None, data_dir=tmp, fetch=fetch)
            n = len(calls)
            b.poll()
            self.assertNotIn(aemo.REGISTRATION_URL, calls[n:])

    def test_failures_stay_separate(self):
        fetch, _ = self.fetcher()

        def broken(url, timeout=60):
            if "Dispatch_SCADA" in url:
                raise aemo.AemoError("AEMO returned 500 for Dispatch_SCADA")
            return fetch(url, timeout)
        a = aemo.Aemo(self._Store(), "NSW1", log=lambda *x: None, fetch=broken)
        a.poll()
        self.assertEqual(a.state["status"], "ok")
        self.assertIn("Dispatch_SCADA", a.state["error"])
        self.assertEqual(a.state["current"]["rrp"], 106.44)


class PageStructure(unittest.TestCase):
    def test_every_tab_sits_directly_in_its_panel(self):
        from html.parser import HTMLParser
        VOID = {"meta", "link", "input", "br", "img", "source", "hr", "col", "area", "base", "embed", "param", "track", "wbr"}

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad, self.tabs = [], [], {}

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                cls = (a.get("class") or "").split()
                if "tabpage" in cls:
                    parent = self.stack[-1] if self.stack else None
                    if not parent or parent[0] != "section" or "group" not in parent[1]:
                        self.bad.append(a.get("data-tab"))
                    group = next((x[2] for x in reversed(self.stack) if x[0] == "section"), None)
                    self.tabs.setdefault(group, []).append(a.get("data-tab"))
                if tag not in VOID:
                    self.stack.append((tag, cls, a.get("data-panel")))

            def handle_endtag(self, tag):
                while self.stack:
                    if self.stack.pop()[0] == tag:
                        break
        p = P()
        p.feed((ROOT / "web" / "index.html").read_text())
        self.assertEqual(p.bad, [])
        self.assertEqual(p.tabs["money"], ["Payback", "Plans", "Trading"])
        self.assertEqual(p.tabs["battery"], ["Now", "Health", "Over time"])


if __name__ == "__main__":
    unittest.main()

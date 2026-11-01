"""Unit tests for the parts with rules in them. Standard library only:
    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

"""Offline contract tests using published TWSE/FinMind examples, no API quota used."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from twquant.cli import import_disclosures
from twquant.ingest import adjusted_for_stock, ingest_dataset, ingest_stock_info, sync
from twquant.sources import SourceError, official_snapshot
from twquant.store import Store


class DataLayerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_2330_official_price_example_and_dedup(self):
        # TWSE monthly report 109/04/08: O=285,H=285.5,L=283,C=285,V=38698826.
        row = {"date": "2020-04-08", "stock_id": "2330", "open": 285,
               "max": 285.5, "min": 283, "close": 285,
               "Trading_Volume": 38698826, "Trading_money": 11016972354}
        for _ in range(2):
            ingest_dataset(self.store, "prices", "2330", [row], "2020-04-08T18:00:00+08:00")
        result = self.store.db.execute("SELECT close,volume_shares,published_at FROM prices").fetchone()
        self.assertEqual(tuple(result), (285, 38698826, None))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM prices").fetchone()[0], 1)

    def test_no_lookahead_from_revenue_period_or_provider_timestamp(self):
        row = {"stock_id": "2330", "date": "2019-04-01", "revenue_year": 2019,
               "revenue_month": 3, "revenue": 79721587000, "create_time": "2026-04-21"}
        ingest_dataset(self.store, "revenue", "2330", [row])
        record = self.store.db.execute("SELECT period_end,published_at,provider_observed_on FROM monthly_revenue").fetchone()
        self.assertEqual(tuple(record), ("2019-03-31", None, "2026-04-21"))

    def test_adjustment_applies_only_before_ex_date(self):
        rows = [{"stock_id": "2330", "date": day, "open": value, "max": value,
                 "min": value, "close": value, "Trading_Volume": 100}
                for day, value in (("2019-06-21", 248.5), ("2019-06-24", 242.0))]
        ingest_dataset(self.store, "prices", "2330", rows)
        ingest_dataset(self.store, "actions", "2330", [{"stock_id": "2330", "date": "2019-06-24",
            "before_price": 248.5, "after_price": 240.5,
            "stock_and_cache_dividend": 8, "stock_or_cache_dividend": "息"}])
        adjusted_for_stock(self.store, "2330")
        records = self.store.db.execute("SELECT trade_date,adjusted_close FROM price_adjusted ORDER BY trade_date").fetchall()
        self.assertAlmostEqual(records[0][1], 240.5)
        self.assertAlmostEqual(records[1][1], 242.0)

    def test_universe_and_strict_schema(self):
        info = [{"stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-09-24"},
                {"stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2026-09-24"}]
        self.assertEqual(ingest_stock_info(self.store, info), 2)
        self.assertEqual(self.store.current_codes(), ["2330"])
        with self.assertRaises(SourceError):
            ingest_dataset(self.store, "prices", "2330", [{"date": "2020-04-08", "close": 285}])

    def test_verified_disclosure_import(self):
        ingest_dataset(self.store, "financials", "2330", [{"date": "2020-03-31",
            "type": "EPS", "value": 4.51, "stock_id": "2330"}])
        csv_file = Path(self.folder.name) / "evidence.csv"
        csv_file.write_text("dataset,code,period_end,published_at,source_url\n"
            "financials,2330,2020-03-31,2020-04-16,https://example.org/official-report\n",
            encoding="utf-8")
        self.assertEqual(import_disclosures(self.store, csv_file), 1)
        self.assertEqual(self.store.db.execute("SELECT published_at FROM quarterly_financials").fetchone()[0],
                         "2020-04-16")
        ingest_dataset(self.store, "financials", "2330", [{"date": "2020-03-31",
            "type": "EPS", "value": 4.51, "stock_id": "2330"}])
        self.assertEqual(self.store.db.execute("SELECT published_at FROM quarterly_financials").fetchone()[0],
                         "2020-04-16")

    def test_resume_from_checkpoint_with_overlap(self):
        class FakeApi:
            def __init__(self):
                self.calls = []

            def fetch(self, dataset, code, start, end):
                self.calls.append((dataset, code, start, end))
                return []
        api = FakeApi()
        sync(self.store, api, ["2330"], ["prices"], date(2020, 4, 1), date(2020, 4, 30))
        sync(self.store, api, ["2330"], ["prices"], date(2020, 4, 1), date(2020, 5, 1))
        self.assertEqual(api.calls[1][2], "2020-04-20")

    def test_official_snapshot_requires_all_fields(self):
        class FakeHttp:
            def get_json(self, url):
                return [{"Date": "109/04/08", "Code": "2330", "Name": "台積電",
                         "OpeningPrice": "285", "HighestPrice": "285.50",
                         "LowestPrice": "283", "ClosingPrice": "285",
                         "TradeVolume": "38,698,826", "TradeValue": "11,016,972,354"}]
        result = official_snapshot(FakeHttp(), "twse")
        self.assertEqual((result[0]["date"], result[0]["volume"]),
                         ("2020-04-08", 38698826))
        class ChangedSchema:
            def get_json(self, url):
                return [{"Date": "109/04/08", "Code": "2330"}]
        with self.assertRaises(SourceError):
            official_snapshot(ChangedSchema(), "twse")


if __name__ == "__main__":
    unittest.main()

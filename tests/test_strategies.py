import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from twquant.store import Store
from twquant.strategies import price_frame, signals


class StrategyTest(unittest.TestCase):
    def test_public_filter_and_no_unverified_disclosure(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "test.sqlite3") as store:
            days = [date(2026, 1, 1) + timedelta(days=i) for i in range(35)]
            columns = ("code", "trade_date", "open", "high", "low", "close", "volume_shares",
                       "turnover_twd", "source", "published_at", "observed_at", "availability")
            rows = []
            for i, day in enumerate(days):
                for code, source in (("2330", "TWSE/OpenAPI"), ("2317", "FinMind/TaiwanStockPrice")):
                    price = float(100 + i)
                    rows.append((code, str(day), price, price, price, price, 1000, None,
                                 source, str(day), "2026-02-10T22:00:00+08:00", "observed_after_close"))
            store.upsert("prices", columns, rows)
            updated = list(rows[0])
            updated[9] = "2026-03-01"
            store.upsert("prices", columns, [tuple(updated)])
            first_date = store.db.execute("SELECT published_at FROM prices WHERE code='2330' AND trade_date=?",
                                          (str(days[0]),)).fetchone()[0]
            self.assertEqual(first_date, str(days[0]))
            public = price_frame(store)
            self.assertEqual(set(public.index.get_level_values("code")), {"2330"})
            trend, _ = signals(store, "均線趨勢", ma_days=20)
            self.assertTrue(trend.iloc[-1]["2330"])
            # A revenue value without a verified announcement must not enter public selections.
            store.upsert("monthly_revenue", ("code", "period_end", "revenue_twd", "published_at",
                         "provider_observed_on", "observed_at", "source"),
                         [("2330", "2026-01-31", 123, None, "2026-02-01", "2026-02-10", "FinMind/TaiwanStockMonthRevenue")])
            revenue, _ = signals(store, "營收動能", ma_days=20)
            self.assertFalse(revenue.to_numpy().any())


if __name__ == "__main__":
    unittest.main()

import unittest

import pandas as pd

from twquant.backtest import sim, walk_forward


class BacktestTest(unittest.TestCase):
    def setUp(self):
        self.days = pd.bdate_range("2026-01-05", periods=6)
        index = pd.MultiIndex.from_product([self.days, ["2330"]], names=["date", "code"])
        self.px = pd.DataFrame({"open": [10, 12, 11, 12, 13, 14],
                                "high": [11, 13, 12, 13, 14, 15],
                                "low": [9, 11, 10, 11, 12, 13],
                                "close": [10, 12, 11, 12, 13, 14],
                                "volume_shares": [1000] * 6}, index=index)
        self.flags = pd.DataFrame({"can_buy": [True] * 6, "can_sell": [True] * 6}, index=index)
        self.pos = pd.DataFrame({"2330": [True, True, False, False, False, False]}, index=self.days)

    def test_signal_next_session_and_fee_tax(self):
        res = sim(self.pos, self.px, resample="D", eligibility=self.flags,
                  initial_cash=1200, slippage=0, fee_discount=0, sell_tax=.003)
        self.assertEqual(list(res.orders.date), [self.days[1], self.days[3]])
        self.assertEqual(list(res.orders.side), ["buy", "sell"])
        self.assertAlmostEqual(res.equity.iloc[-1], 1196.4)

    def test_blocked_exit_keeps_exposure(self):
        self.flags.loc[(self.days[3], "2330"), "can_sell"] = False
        res = sim(self.pos, self.px, resample="D", eligibility=self.flags,
                  initial_cash=1200, slippage=0, fee_discount=0, sell_tax=0)
        self.assertEqual(list(res.orders.date), [self.days[1], self.days[4]])

    def test_unknown_flags_and_missing_codes_rejected(self):
        flags = self.flags.astype(object).copy()
        flags.iloc[0, 0] = None
        with self.assertRaises(ValueError):
            sim(self.pos, self.px, eligibility=flags)
        with self.assertRaises(ValueError):
            sim(pd.DataFrame({"9999": [True] * 6}, index=self.days), self.px,
                eligibility=self.flags)

    def test_walk_forward_excludes_overlap(self):
        train, test = walk_forward(self.pos, "2026-01-08")
        self.assertTrue((train.index < "2026-01-08").all())
        self.assertTrue((test.index >= "2026-01-08").all())


if __name__ == "__main__":
    unittest.main()

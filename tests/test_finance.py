import math
import unittest

from eira_harness.demo import sample_csv
from eira_harness.finance import backtest, markdown_report, read_prices
from eira_harness.security import HarnessError


def prices(values):
    return "date,close\n" + "\n".join(f"2025-01-{i+1:02d},{value}" for i, value in enumerate(values))


class FinanceTests(unittest.TestCase):
    def test_signal_is_lagged_and_final_position_liquidated(self):
        result = backtest(prices([10, 11, 12, 20, 30]), fast=1, slow=2, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["trades"][0]["date"], "2025-01-03")
        self.assertEqual(result["trades"][0]["fill"], 12)
        self.assertEqual(result["trades"][-1]["reason"], "end_of_data")
        self.assertAlmostEqual(result["metrics"]["final_equity"], 25_000)

    def test_price_on_signal_execution_day_does_not_change_signal(self):
        result = backtest(prices([10, 11, 1, 2, 3]), fast=1, slow=2, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["trades"][0]["date"], "2025-01-03")
        self.assertEqual(result["trades"][0]["side"], "buy")

    def test_flat_prices_make_no_trades(self):
        result = backtest(prices([10] * 10), fast=1, slow=2)
        self.assertEqual(result["metrics"]["orders"], 0)
        self.assertEqual(result["metrics"]["total_return"], 0)
        self.assertIsNone(result["metrics"]["sharpe_zero_risk_free"])

    def test_costs_reduce_return(self):
        raw = prices([10, 11, 12, 13, 14, 15])
        free = backtest(raw, fast=1, slow=2, fee_bps=0, slippage_bps=0)
        costly = backtest(raw, fast=1, slow=2, fee_bps=20, slippage_bps=20)
        self.assertLess(costly["metrics"]["total_return"], free["metrics"]["total_return"])
        self.assertGreater(costly["metrics"]["fees_paid"], 0)

    def test_exposure_limits_capital(self):
        result = backtest(prices([10, 11, 12, 20, 30]), fast=1, slow=2,
                          exposure=.25, fee_bps=0, slippage_bps=0)
        buy = result["trades"][0]
        self.assertAlmostEqual(buy["quantity"] * buy["fill"], 2500)
        self.assertAlmostEqual(result["metrics"]["final_equity"], 13750)

    def test_drawdown_halts_future_entries(self):
        result = backtest(prices([10, 11, 12, 6, 8, 10, 12, 14]), fast=1, slow=2,
                          max_drawdown=.1, fee_bps=0, slippage_bps=0)
        self.assertTrue(result["metrics"]["risk_stop_triggered"])
        self.assertEqual(result["metrics"]["orders"], 2)
        self.assertEqual(result["trades"][-1]["reason"], "drawdown_stop")
        self.assertGreater(result["metrics"]["max_drawdown"], .1)

    def test_invalid_csv_is_rejected(self):
        for raw in ["x,y\n1,2", prices([10, "NaN", 12]), prices([10, "inf", 12]),
                    prices([10, -1, 12]), prices([10, 0, 12]), prices([10, 1e300, 12]),
                    "date,close\n2025-01-01,10\n2025-01-01,11\n2025-01-02,12"]:
            with self.subTest(raw=raw), self.assertRaises(HarnessError):
                read_prices(raw)

    def test_signal_exit_fees_latch_drawdown_stop(self):
        result = backtest(prices([10, 11, 12, 12, 11.95, 11.9, 12, 13, 14, 15]),
                          fast=1, slow=2, capital=10000, fee_bps=1000,
                          slippage_bps=0, max_drawdown=.18)
        self.assertGreater(result["metrics"]["max_drawdown"], .18)
        self.assertTrue(result["metrics"]["risk_stop_triggered"])
        self.assertEqual(result["metrics"]["orders"], 2)
        self.assertEqual(result["trades"][-1]["reason"], "sma_signal")
        self.assertEqual(result["trades"][-1]["date"], "2025-01-05")
        self.assertEqual(len({row["equity"] for row in result["equity_curve"][4:]}), 1)

    def test_invalid_parameters_are_rejected(self):
        for kwargs in [{"fast": 30}, {"slow": 180}, {"capital": float("nan")},
                       {"exposure": 1.1}, {"fee_bps": -1}, {"fast": True},
                       {"max_drawdown": 0}, {"periods_per_year": 1000}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(HarnessError):
                backtest(sample_csv(), **kwargs)

    def test_reproducibility_and_report(self):
        result = backtest(sample_csv())
        self.assertEqual(result, backtest(sample_csv()))
        self.assertEqual(len(result["data_sha256"]), 64)
        self.assertEqual(len(result["equity_curve"]), 180)
        self.assertIn("Data SHA-256", markdown_report(result))
        self.assertTrue(math.isfinite(result["metrics"]["final_equity"]))


if __name__ == "__main__":
    unittest.main()

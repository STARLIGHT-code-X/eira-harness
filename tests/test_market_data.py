import json
from datetime import date
import unittest
from unittest.mock import patch

from eira_harness import security
from eira_harness.market_data import fetch_prices
from eira_harness.security import HarnessError


class MarketDataTests(unittest.TestCase):
    def decode(self, raw):
        return json.loads(raw)

    def test_alpha_vantage_uses_compact_daily_endpoint_and_sorts(self):
        payload = {"Time Series (Daily)": {
            "2025-01-02": {"4. close": "11.25"},
            "2025-01-01": {"4. close": "10.00"},
            "2026-09-13": {"4. close": "12.00"},
        }}
        with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "secret-key-value"}), \
             patch("eira_harness.market_data.request_bytes", return_value=(200, {}, json.dumps(payload).encode())) as request, \
             patch.object(security, "bounded_json_loads", side_effect=self.decode, create=True), \
             patch("eira_harness.market_data._today_utc", return_value=date(2026, 9, 13)):
            result = fetch_prices("alphavantage", "IBM")
        request.assert_called_once()
        url = request.call_args.args[0]
        self.assertIn("function=TIME_SERIES_DAILY", url)
        self.assertIn("outputsize=compact", url)
        self.assertNotIn("secret-key-value", result)
        self.assertEqual(result["csv"], "date,close\n2025-01-01,10.00\n2025-01-02,11.25\n")

    def test_coinbase_daily_candles_are_bounded_and_current_day_is_excluded(self):
        # Coinbase candle fields are [time, low, high, open, close, volume].
        payload = [[1735776000, 9, 12, 10, "11", 100],
                   [1735862400, 10, 13, 11, "12", 100],
                   [1789257600, 10, 13, 11, "99", 100]]
        with patch("eira_harness.market_data.request_bytes", return_value=(200, {}, json.dumps(payload).encode())) as request, \
             patch.object(security, "bounded_json_loads", side_effect=self.decode, create=True), \
             patch("eira_harness.market_data._today_utc", return_value=date(2026, 9, 13)):
            result = fetch_prices("coinbase", "btc-usd")
        url = request.call_args.args[0]
        self.assertIn("products/BTC-USD/candles", url)
        self.assertIn("granularity=86400", url)
        self.assertEqual(result["symbol"], "BTC-USD")
        self.assertIn("2025-01-03,12", result["csv"])
        self.assertNotIn("99", result["csv"])

    def test_key_missing_and_unknown_options_fail_safely(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HarnessError):
                fetch_prices("alphavantage", "IBM")
        with self.assertRaises(HarnessError):
            fetch_prices("coinbase", "BTC-USD", endpoint="https://evil.example")

    def test_provider_error_does_not_echo_response(self):
        with patch("eira_harness.market_data.request_bytes", side_effect=RuntimeError("secret-key-value")):
            with self.assertRaises(HarnessError) as raised:
                fetch_prices("coinbase", "BTC-USD")
        self.assertNotIn("secret-key-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

# Daily market data

`eira_harness.market_data.fetch_prices(source, symbol, **kwargs)` provides a
small read-only bridge to two public daily data endpoints. It returns a mapping
with a `date,close` CSV string, canonical `source` and `symbol`, a UTC
`fetched_at` timestamp, and provider caveats. The CSV is sorted oldest to
newest and is suitable for `finance.backtest`.

The supported sources are:

| Source | Symbol example | Endpoint behavior |
|---|---|---|
| `alphavantage` | `IBM` | Alpha Vantage `TIME_SERIES_DAILY`, `outputsize=compact`; at most 100 bars |
| `coinbase` | `BTC-USD` | Coinbase Exchange product candles with `granularity=86400`; at most 300 bars |

Alpha Vantage requires `ALPHAVANTAGE_API_KEY` in the process environment. The
key is used only in the provider request and is excluded from returned metadata
and user-facing errors. Coinbase Exchange daily candles are public and require
no key. Calls are built from the approved source and symbol values; callers do
not supply an endpoint or interval.

Both adapters discard the current UTC calendar day because it may still be an
incomplete bar. Coinbase crypto candles are UTC-based, including their daily
boundary; this can differ from an exchange or reporting timezone used by other
data sources. The optional `limit` can further reduce output but cannot exceed
the provider bound (100 for Alpha Vantage or 300 for Coinbase). No adapter
places trades or connects to an account.

Provider responses and transport reads are bounded to 1 MB. Returned prices are
validated as finite positive values in the range accepted by the backtester.
Provider outages, malformed responses, and rate limits produce a safe generic
`HarnessError` without echoing response content.

Alpha Vantage supplies raw, unadjusted closes; stock splits and dividends need separate treatment before interpreting a backtest. Coinbase may omit intervals with no trades, so check calendar gaps before annualizing results. Eira does not fill missing days or redistribute provider datasets. Account plans, rate limits, availability, and permitted data use remain subject to each source's terms.

Official API references: [Alpha Vantage daily time series](https://www.alphavantage.co/documentation/#daily) and [Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).

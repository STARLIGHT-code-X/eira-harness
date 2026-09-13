"""Small, read-only daily price adapters for approved public providers.

The adapters intentionally expose a narrow interface. They do not accept a URL,
place orders, or retain provider responses. Alpha Vantage uses its daily compact
endpoint (100 bars); Coinbase Exchange uses daily candles (at most 300 bars).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from urllib.parse import quote, urlencode

from .network import request_bytes
from .security import HarnessError


_ALPHAVANTAGE = "alphavantage"
_COINBASE = "coinbase"
_MAX_BARS = {_ALPHAVANTAGE: 100, _COINBASE: 300}
_AV_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$")
_COINBASE_SYMBOL = re.compile(r"^[A-Za-z0-9]+-[A-Za-z0-9]+$")


def _validate_request(source: str, symbol: str, limit) -> tuple[str, str, int]:
    if not isinstance(source, str) or source.lower() not in _MAX_BARS:
        raise HarnessError("Unsupported market-data source.")
    source = source.lower()
    if not isinstance(symbol, str) or len(symbol) > 64:
        raise HarnessError("Invalid market-data symbol.")
    if source == _ALPHAVANTAGE:
        valid_symbol = _AV_SYMBOL.fullmatch(symbol)
    else:
        valid_symbol = _COINBASE_SYMBOL.fullmatch(symbol)
    if not valid_symbol:
        raise HarnessError("Invalid market-data symbol.")
    if source == _COINBASE:
        symbol = symbol.upper()
    if limit is None:
        limit = _MAX_BARS[source]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BARS[source]:
        raise HarnessError("Market-data limit exceeds the provider bound.")
    return source, symbol, limit


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _close_text(value) -> str:
    """Validate and canonicalize a provider close without float rounding."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError()
    raw = str(value).strip()
    if not raw or len(raw) > 128:
        raise ValueError()
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError() from None
    if not parsed.is_finite() or not Decimal("1e-12") <= parsed <= Decimal("1e12"):
        raise ValueError()
    # Fixed-point output avoids exponent syntax and keeps the CSV accepted by
    # finance.read_prices. It also prevents provider strings from injecting CSV.
    return format(parsed, "f")


def _csv(rows: list[tuple[date, str]], limit: int) -> str:
    unique: dict[date, str] = {}
    for stamp, close in rows:
        if stamp in unique:
            raise HarnessError("Market-data provider returned duplicate daily bars.")
        unique[stamp] = close
    ordered = sorted(unique.items())[-limit:]
    if not ordered:
        raise HarnessError("Market-data provider returned no completed daily bars.")
    return "date,close\n" + "\n".join(f"{stamp.isoformat()},{close}" for stamp, close in ordered) + "\n"


def _request(url: str):
    try:
        result = request_bytes(url, method="GET", body=None,
                               headers={"Accept": "application/json", "User-Agent": "EiraHarness/0.2"},
                               timeout=15, max_bytes=1_000_000, public_only=True)
        status, headers, body = result
        if type(status) is not int or status != 200 or not isinstance(headers, dict) or not isinstance(body, bytes):
            raise ValueError()
        if len(body) > 1_000_000:
            raise ValueError()
        return body
    except Exception:
        # Provider/network details may contain query strings or response data.
        raise HarnessError("Market-data request failed.") from None


def _parse_alpha_vantage(body: bytes, limit: int) -> str:
    try:
        # This bounded helper is provided by the security boundary. Importing it
        # through the module keeps this adapter compatible during package startup.
        from . import security
        payload = security.bounded_json_loads(body)
        series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
        if not isinstance(series, dict):
            raise ValueError()
        today = _today_utc()
        rows = []
        for raw_date, values in series.items():
            if not isinstance(raw_date, str):
                raise ValueError()
            stamp = date.fromisoformat(raw_date)
            if stamp.isoformat() != raw_date or stamp >= today:
                continue
            if not isinstance(values, dict) or "4. close" not in values:
                raise ValueError()
            rows.append((stamp, _close_text(values["4. close"])))
        return _csv(rows, limit)
    except Exception:
        raise HarnessError("Alpha Vantage returned malformed daily data.") from None


def _parse_coinbase(body: bytes, limit: int) -> str:
    try:
        from . import security
        payload = security.bounded_json_loads(body)
        if not isinstance(payload, list):
            raise ValueError()
        today = _today_utc()
        rows = []
        for candle in payload:
            if not isinstance(candle, (list, tuple)) or len(candle) != 6:
                raise ValueError()
            raw_timestamp = candle[0]
            if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)):
                raise ValueError()
            if isinstance(raw_timestamp, float) and not raw_timestamp.is_integer():
                raise ValueError()
            if int(raw_timestamp) % 86400:
                raise ValueError()
            stamp = datetime.fromtimestamp(int(raw_timestamp), timezone.utc).date()
            if stamp >= today:
                continue
            rows.append((stamp, _close_text(candle[4])))
        return _csv(rows, limit)
    except Exception:
        raise HarnessError("Coinbase returned malformed daily data.") from None


def fetch_prices(source: str, symbol: str, **kwargs) -> dict:
    """Fetch completed daily bars and return a finance-compatible CSV result.

    ``limit`` is the only optional argument. The interval is fixed to daily and
    provider URLs are constructed internally, so callers cannot redirect this
    function to an arbitrary endpoint.
    """
    if set(kwargs) - {"limit"}:
        raise HarnessError("Unsupported market-data options.")
    source, symbol, limit = _validate_request(source, symbol, kwargs.get("limit"))
    if source == _ALPHAVANTAGE:
        key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
        if not isinstance(key, str) or not key:
            raise HarnessError("ALPHAVANTAGE_API_KEY is required for Alpha Vantage data.")
        query = urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol,
                            "outputsize": "compact", "apikey": key})
        body = _request("https://www.alphavantage.co/query?" + query)
        csv_text = _parse_alpha_vantage(body, limit)
        caveats = ["Raw, unadjusted closes: splits and dividends are not corrected; review corporate actions before backtesting.",
                   "Alpha Vantage compact daily data is bounded to its latest 100 provider bars.",
                   "The current UTC calendar day is excluded while its daily bar may be incomplete."]
    else:
        encoded_symbol = quote(symbol, safe="")
        body = _request("https://api.exchange.coinbase.com/products/" + encoded_symbol +
                        "/candles?" + urlencode({"granularity": 86400}))
        csv_text = _parse_coinbase(body, limit)
        caveats = ["Historical candles may have gaps; missing days are not fabricated or filled.",
                   "Coinbase Exchange daily candles are bounded to 300 bars per request.",
                   "Crypto daily bars use UTC calendar boundaries; the current UTC day is excluded while incomplete."]
    return {"csv": csv_text, "source": source, "symbol": symbol,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "caveats": caveats}

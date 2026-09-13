"""Deterministic long/cash backtesting. Signals at t-1 execute at close t."""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import math
import statistics

from .security import HarnessError


def _safe_csv_rows(reader):
    try:
        yield from reader
    except csv.Error as exc:
        raise HarnessError("Malformed CSV input.") from exc


def _parse_price_rows(text: str) -> list[tuple[str, Decimal]]:
    """Parse a date/close CSV without DictReader's silent data loss.

    Named columns beyond date and close are accepted for provider exports. Every
    physical row still has to have the same number of fields, and duplicate
    header names are rejected instead of allowing DictReader to overwrite one.
    Decimal values are retained for the signal calculation; converting to float
    before calculating a moving average can erase small, meaningful differences.
    """
    if not isinstance(text, str):
        raise HarnessError("CSV needs date and close columns (ISO YYYY-MM-DD, positive prices).")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
    except (csv.Error, TypeError, ValueError) as exc:
        raise HarnessError("CSV needs date and close columns (ISO YYYY-MM-DD, positive prices).") from exc
    if not header or any(not isinstance(name, str) or not name for name in header):
        raise HarnessError("CSV needs date and close columns (ISO YYYY-MM-DD, positive prices).")
    if len({name.casefold() for name in header}) != len(header):
        raise HarnessError("CSV headers must be unique.")
    try:
        date_column = header.index("date")
        close_column = header.index("close")
    except ValueError as exc:
        raise HarnessError("CSV needs date and close columns (ISO YYYY-MM-DD, positive prices).") from exc

    rows: list[tuple[str, Decimal]] = []
    previous = None
    for number, row in enumerate(_safe_csv_rows(reader), 2):
        # csv.reader emits [] for a blank physical line; DictReader ignored it.
        if not row:
            continue
        if len(rows) >= 50_000:
            raise HarnessError("CSV is limited to 50,000 daily bars.")
        if len(row) != len(header):
            raise HarnessError(f"Invalid CSV row {number}: field count must match the header.")
        try:
            raw_date = row[date_column]
            stamp = date.fromisoformat(raw_date)
            if stamp.isoformat() != raw_date:
                raise ValueError()
            raw_price = row[close_column].strip()
            # A lexical bound keeps Decimal work bounded for hostile inputs while
            # retaining ample precision for ordinary provider exports.
            if len(raw_price) > 128:
                raise ValueError()
            price = Decimal(raw_price)
            if not price.is_finite() or not Decimal("1e-12") <= price <= Decimal("1e12"):
                raise ValueError()
            if previous is not None and stamp <= previous:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError, IndexError) as exc:
            raise HarnessError(f"Invalid CSV row {number}: dates must increase strictly and prices must be finite, between 1e-12 and 1e12.") from exc
        previous = stamp
        rows.append((stamp.isoformat(), price))
    if len(rows) < 3:
        raise HarnessError("At least three price rows are required.")
    return rows


def read_prices(text: str) -> list[tuple[str, float]]:
    return [(stamp, float(price)) for stamp, price in _parse_price_rows(text)]


def _finite_number(value, label: str) -> float:
    """Validate numbers without allowing math.isfinite to overflow on huge ints."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessError("Backtest parameters must be finite numbers.")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        raise HarnessError("Backtest parameters must be finite numbers.") from None
    if not math.isfinite(converted):
        raise HarnessError("Backtest parameters must be finite numbers.")
    return converted


def backtest(text: str, fast: int = 10, slow: int = 30, capital: float = 10_000,
             fee_bps: float = 10, slippage_bps: float = 5, exposure: float = 1.0,
             max_drawdown: float = 0.20, periods_per_year: int = 252) -> dict:
    decimal_rows = _parse_price_rows(text)
    rows = [(stamp, float(price)) for stamp, price in decimal_rows]
    if type(fast) is not int or type(slow) is not int or not 1 <= fast < slow < len(rows):
        raise HarnessError("Windows must satisfy 1 <= fast < slow < number of rows.")
    if type(periods_per_year) is not int or not 1 <= periods_per_year <= 366:
        raise HarnessError("periods_per_year must be an integer from 1 to 366 for daily data.")
    capital = _finite_number(capital, "capital")
    fee_bps = _finite_number(fee_bps, "fee_bps")
    slippage_bps = _finite_number(slippage_bps, "slippage_bps")
    exposure = _finite_number(exposure, "exposure")
    max_drawdown = _finite_number(max_drawdown, "max_drawdown")
    if not (0 < capital <= 1e12 and 0 <= fee_bps <= 1000 and 0 <= slippage_bps <= 1000
            and 0 < exposure <= 1 and 0 < max_drawdown <= 1):
        raise HarnessError("Invalid capital, costs, exposure, or drawdown limit.")
    fee, slip = fee_bps / 10_000, slippage_bps / 10_000
    cash, quantity, peak = float(capital), 0.0, float(capital)
    curve, trades = [], []
    halted = False
    worst_drawdown = 0.0
    closes = [row[1] for row in rows]
    decimal_closes = [row[1] for row in decimal_rows]
    fast_sum = Decimal(0)
    slow_sum = Decimal(0)

    def buy(stamp, price):
        nonlocal cash, quantity
        fill = price * (1 + slip)
        quantity = cash * exposure / (fill * (1 + fee))
        cost = quantity * fill
        cash -= cost * (1 + fee)
        trades.append({"date": stamp, "side": "buy", "quantity": quantity,
                       "fill": fill, "fee": cost * fee, "reason": "sma_signal"})

    def sell(stamp, price, reason):
        nonlocal cash, quantity
        fill = price * (1 - slip)
        proceeds = quantity * fill
        cash += proceeds * (1 - fee)
        trades.append({"date": stamp, "side": "sell", "quantity": quantity,
                       "fill": fill, "fee": proceeds * fee, "reason": reason})
        quantity = 0.0

    # Decimal rolling sums keep SMA comparisons stable and linear in the number
    # of bars. The local precision is deliberately bounded; prices themselves
    # are bounded to 128 characters above.
    precision = 160
    with localcontext() as decimal_context:
        decimal_context.prec = precision
        for i, (stamp, price) in enumerate(rows):
            if i:
                fast_sum += decimal_closes[i - 1]
                slow_sum += decimal_closes[i - 1]
                if i - fast - 1 >= 0:
                    fast_sum -= decimal_closes[i - fast - 1]
                if i - slow - 1 >= 0:
                    slow_sum -= decimal_closes[i - slow - 1]

            marked = cash + quantity * price
            if not math.isfinite(marked):
                raise HarnessError("Simulation exceeded the supported numeric range.")
            peak = max(peak, marked)
            if quantity and 1 - marked / peak >= max_drawdown:
                sell(stamp, price, "drawdown_stop")
                halted = True
            # The current row is deliberately excluded from the signal.
            if i >= slow and not halted and i < len(rows) - 1:
                fast_mean = fast_sum / fast
                slow_mean = slow_sum / slow
                if fast_mean > slow_mean and quantity == 0:
                    buy(stamp, price)
                elif fast_mean <= slow_mean and quantity > 0:
                    sell(stamp, price, "sma_signal")
            if i == len(rows) - 1 and quantity:
                sell(stamp, price, "end_of_data")
            equity = cash + quantity * price
            if not math.isfinite(equity):
                raise HarnessError("Simulation exceeded the supported numeric range.")
            peak = max(peak, equity)
            drawdown = 1 - equity / peak
            worst_drawdown = max(worst_drawdown, drawdown)
            # Exit fees can breach the limit after a signal has already sold the
            # position. Latch the stop even when already in cash to prevent reentry.
            if drawdown >= max_drawdown:
                if quantity:
                    sell(stamp, price, "drawdown_stop")
                halted = True
                equity = cash
                drawdown = 1 - equity / peak
                worst_drawdown = max(worst_drawdown, drawdown)
            # Keep full precision in the source curve. Rounded values here used
            # to feed returns and could create zero denominators for tiny capital.
            curve.append({"date": stamp, "equity": equity, "drawdown": drawdown})
    returns = [curve[i]["equity"] / curve[i-1]["equity"] - 1 for i in range(1, len(curve))
               if curve[i-1]["equity"] != 0]
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0
    sharpe = statistics.mean(returns) / deviation * math.sqrt(periods_per_year) if deviation > 1e-12 else None
    benchmark_quantity = capital / (closes[0] * (1 + slip) * (1 + fee))
    benchmark_end = benchmark_quantity * closes[-1] * (1 - slip) * (1 - fee)
    final = curve[-1]["equity"]
    return {
        "strategy": "Long/cash SMA crossover", "data_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "parameters": {"fast": fast, "slow": slow, "capital": capital, "fee_bps": fee_bps,
                       "slippage_bps": slippage_bps, "exposure": exposure,
                       "max_drawdown": max_drawdown, "periods_per_year": periods_per_year},
        "period": {"start": rows[0][0], "end": rows[-1][0], "bars": len(rows)},
        "metrics": {"final_equity": round(final, 2), "total_return": final / capital - 1,
                    "buy_hold_return": benchmark_end / capital - 1,
                    "max_drawdown": worst_drawdown, "sharpe_zero_risk_free": sharpe,
                    "orders": len(trades), "round_trips": sum(t["side"] == "sell" for t in trades),
                    "fees_paid": sum(t["fee"] for t in trades), "risk_stop_triggered": halted},
        "assumptions": ["Daily bars; signal from completed bars executes at the next close.",
                        "Long/cash only; fractional units; no borrowing, taxes, dividends, or funding.",
                        "Fixed fee and adverse slippage on each fill; final position liquidated.",
                        "Close-only drawdown stop can exceed its threshold after gaps and costs.",
                        "Buy-and-hold benchmark uses 100% exposure and the same transaction costs.",
                        "Historical or synthetic results do not establish future performance."],
        "equity_curve": curve, "trades": trades,
    }


def markdown_report(result: dict) -> str:
    m, p = result["metrics"], result["parameters"]
    sharpe = m["sharpe_zero_risk_free"]
    return "\n".join([
        "# Eira backtest", "", f"{result['strategy']} · SMA {p['fast']}/{p['slow']}", "",
        f"Period: {result['period']['start']} to {result['period']['end']} ({result['period']['bars']} bars)", "",
        "| Metric | Result |", "|---|---:|",
        f"| Final equity | {m['final_equity']:,.2f} |",
        f"| Strategy return | {m['total_return']:.2%} |",
        f"| Buy and hold | {m['buy_hold_return']:.2%} |",
        f"| Maximum drawdown | {m['max_drawdown']:.2%} |",
        f"| Annualized Sharpe (zero risk-free) | {f'{sharpe:.3f}' if sharpe is not None else 'N/A'} |",
        f"| Round trips | {m['round_trips']} |", f"| Fees | {m['fees_paid']:.2f} |",
        f"| Drawdown stop triggered | {m['risk_stop_triggered']} |", "",
        "## Assumptions", "", *[f"- {item}" for item in result["assumptions"]], "",
        "## Reproducibility", "", f"Data SHA-256: `{result['data_sha256']}`", "",
        "```json", __import__("json").dumps(p, indent=2), "```", "",
        "## Orders", "", "| Date | Side | Quantity | Fill | Fee | Reason |",
        "|---|---|---:|---:|---:|---|",
        *[f"| {t['date']} | {t['side']} | {t['quantity']:.6f} | {t['fill']:.4f} | {t['fee']:.4f} | {t['reason']} |"
          for t in result["trades"]], "",
    ])

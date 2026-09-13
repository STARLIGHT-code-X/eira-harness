# Eira backtest

Long/cash SMA crossover · SMA 5/20

Period: 2025-01-01 to 2025-06-29 (180 bars)

| Metric | Result |
|---|---:|
| Final equity | 12,213.50 |
| Strategy return | 22.13% |
| Buy and hold | 13.19% |
| Maximum drawdown | 5.93% |
| Annualized Sharpe (zero risk-free) | 3.836 |
| Round trips | 4 |
| Fees | 85.04 |
| Drawdown stop triggered | False |

## Assumptions

- Daily bars; signal from completed bars executes at the next close.
- Long/cash only; fractional units; no borrowing, taxes, dividends, or funding.
- Fixed fee and adverse slippage on each fill; final position liquidated.
- Close-only drawdown stop can exceed its threshold after gaps and costs.
- Buy-and-hold benchmark uses 100% exposure and the same transaction costs.
- Historical or synthetic results do not establish future performance.

## Reproducibility

Data SHA-256: `3422325fe140dfbf18353835d7d387a5e71111c4e707849b699822f772dde59e`

```json
{
  "fast": 5,
  "slow": 20,
  "capital": 10000,
  "fee_bps": 10.0,
  "slippage_bps": 5.0,
  "exposure": 1,
  "max_drawdown": 0.2,
  "periods_per_year": 365
}
```

## Orders

| Date | Side | Quantity | Fill | Fee | Reason |
|---|---|---:|---:|---:|---|
| 2025-01-21 | buy | 92.466109 | 108.0397 | 9.9900 | sma_signal |
| 2025-01-24 | sell | 92.466109 | 103.7363 | 9.5921 | sma_signal |
| 2025-02-16 | buy | 95.053133 | 100.7113 | 9.5729 | sma_signal |
| 2025-03-14 | sell | 95.053133 | 110.0004 | 10.4559 | sma_signal |
| 2025-04-07 | buy | 101.385939 | 102.9234 | 10.4350 | sma_signal |
| 2025-05-05 | sell | 101.385939 | 112.3804 | 11.3938 | sma_signal |
| 2025-05-28 | buy | 103.155782 | 110.2316 | 11.3710 | sma_signal |
| 2025-06-23 | sell | 103.155782 | 118.5171 | 12.2257 | sma_signal |

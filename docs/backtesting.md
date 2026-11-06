# KR-05 — Binance BTCUSDT historical backtesting

## Purpose

Evaluate whether Kronos hourly close forecasts can support a **hypothetical spot long-or-cash** rule after economic costs. This is **not** a trading bot or evidence of profitability.

## Strict signal timing

For each target candle at timestamp `t`, the Kronos forecast only receives candles before `t`. We compare the predicted close to the **last observed close**. The signal is determined without examining the target candle.

- If predicted gain is **above 20 basis points**, simulate buying at the target's open and selling at its close.
- Otherwise, remain in cash for that candle.
- No short selling, borrowing, leverage or carried overnight positions.
- This is a one-candle model evaluation, not an implementable exchange order flow; fills at exactly the next candle's open are optimistic because inference and network latency exist.
- Each simulated buy and sell incurs 10 bps per side in fees, half of 2 bps assumed total spread on each side, and 3 bps per side slippage. These are configurable **assumptions**, not verified exchange costs or quotes.
- Initial hypothetical capital is 10,000 USDT. No real currency is used.
- Compare strategy net return with zero-interest cash and one-time-buy/hold over the same evaluation period. Compute max drawdown, trade count, and profitable-trade fraction.

## Real-model research pipeline

Workflow `KR-05 Binance cost-aware backtest` fetches the SHA-256-manifest-verified Binance dataset from `market-data`, runs Kronos-mini chronological inference and a previous-close forecast benchmark, then simulates the long-or-cash rule. A detailed trade ledger and forecast record are retained as temporary GitHub artifacts.

Run locally (using matching verified dataset and model output):

```bash
python -m research.backtest \
  --csv /path/to/BTCUSDT_1h.csv \
  --forecast-json research/output/kr05_predictions.json \
  --fee-bps 10 --spread-bps 2 --slippage-bps 3 --edge-bps 20 \
  --output research/output/kr05_backtest.json
```

The forecast JSON must match the source dataset SHA-256 and BTCUSDT/1h metadata. Target candle close and preceding close are checked against the verified dataset, and duplicate or out-of-order forecast timestamps are rejected.

## Longer historical data

A separate `KR-05 Binance extended history backfill` GitHub workflow obtains 90 days of **closed** Binance spot hourly candles, validates contiguity, and stores them under `market-data/datasets/BTCUSDT_1h_research.csv` with its SHA-256 manifest. This is isolated from the daily `BTCUSDT_1h.csv` collection. The longer dataset is a prerequisite for stronger walk-forward testing, not itself proof of robust strategy performance.

## Limitations and required next checks

1. A handful of adjacent predictions is **not** statistical evidence of an edge; test hundreds of chronological holdouts over varied market conditions.
2. Compare multiple economic costs and avoid optimizing the threshold using held-out results.
3. Evaluate signal stability, data quality, market impact, gaps, execution delay, and latency before a trading integration.
4. Compare against buy-and-hold and cash, not only forecast MAE.
5. Real Binance order execution and credentials are strictly out of scope. Live trading remains disabled.

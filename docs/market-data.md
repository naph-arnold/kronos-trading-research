# KR-03 — Historical market data ingestion

A **read-only public REST** market data collector is provided by `research.market_data`. It requires no API key and cannot send trade orders.

## Run locally

```bash
python -m research.market_data --symbol BTCUSDT --interval 1h \
  --start 2026-10-01T00:00:00Z --end 2026-10-08T00:00:00Z \
  --output research/output/BTCUSDT_1h.csv
python -m pytest -q tests/test_market_data.py
```

## Run from GitHub

Choose **Actions → Download historical market data → Run workflow**, select the development branch, BTCUSDT or ETHUSDT, an interval, and 1-30 days. GitHub stores the validated CSV as a downloadable artifact for 14 days. This manual workflow is intentionally not a recurring download job until an appropriate durable dataset store and monitor are selected.

## Validation and scope

- Only **closed** candles are saved. All timestamps are UTC, aligned to the selected interval.
- Candles are strictly sequential; missing, duplicate, non-progressing, out-of-order, partial, or out-of-range results cause failure.
- Nonfinite/negative data, invalid OHLC bounds, and unexpected responses fail closed.
- Pages are requested in a fixed UTC interval using the official Binance public spot `GET /api/v3/klines` endpoint with maximum 1000 records per request.
- CSV writes use a temporary file and atomic replace, avoiding partial datasets after failed runs.
- Supported fixed-width intervals: 1m, 5m, 15m, 1h, 4h, 1d. This is **spot data**, not futures data; no exchange orders or API keys are used.

Data is stored in ignored `research/output/`, not committed to Git. GitHub artifacts have limited retention, so results intended for reproducible research must be preserved separately with file hashes and experiment metadata. Binance access may be region-restricted or temporarily unavailable; remote download success must be verified in Actions. Missing data should not be fabricated.

Source: [Binance Spot API — Kline/Candlestick data](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data).

## Next

Add manifest/checksums, durable object storage, incremental ingestion and automated alerting before unattended scheduling. Feed a validated static dataset into chronological walk-forward backtesting; **never** mix future values into model inputs.

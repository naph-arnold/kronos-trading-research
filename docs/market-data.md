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

## Incremental storage and integrity manifests

Use `python -m research.datasets --symbol BTCUSDT --interval 1h --start 2026-10-01T00:00:00Z --end 2026-10-08T00:00:00Z --output research/output/BTCUSDT_1h.csv` to create a SHA-256-verifiable CSV and JSON manifest. Run it again with a later end timestamp to fetch **only missing completed candles**. Missing or corrupted files, inconsistent manifests, mismatched market settings, and gaps are rejected; nothing is silently repaired.

The GitHub download workflow now emits both the CSV and manifest. Each GitHub Actions run begins on an ephemeral machine, so successive workflow runs still fetch a new dataset rather than incrementally extending the prior artifact. Reliable **cross-run** incremental ingestion requires durable storage (e.g., a secured object store and checkpoint state). The current implementation provides incremental updates on persistent local volumes only; no continuous schedule is activated.

## Durable automated Coinbase BTC/USD ingestion

The `persistent-market-data.yml` workflow now uses public [Coinbase Exchange historical candles](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles). Binance returned HTTP 451 from GitHub-hosted runners, so the unattended BTC/USD path intentionally does **not** rely on Binance.

On a development-branch push affecting the workflow or `research/`, the job verifies the provider, reloads the `market-data` branch, and appends only new complete hourly candles into `datasets/BTCUSD_1h.csv` and its SHA-256 manifest. Successful updates are committed to the public `market-data` branch using the workflow's scoped `GITHUB_TOKEN` (no external secrets). Every run validates existing data before appending. The first live test successfully saved 168 hourly candles.

The workflow also declares a daily UTC schedule of **02:17**, corresponding to approximately **05:17 in Kenya**. **GitHub schedules trigger only from the default branch**, so the schedule does not become active until this workflow is merged to `master`. GitHub may delay or skip scheduled starts; monitor run history. Data on the public branch is intentionally publicly readable; never store account data or exchange credentials there.

The provider's `amount` field is zero because public Coinbase Exchange candles supply OHLCV without quote turnover; it is not a measured zero trading value. The stored history is exchange-specific BTC/USD spot data, not Binance BTC/USDT; do not merge the two into one model training series without an explicit data normalization and provenance strategy. The implementation currently has no external alerting, backup beyond Git history, or self-healing of data gaps. These are operational limits, not reasons to relax validation.

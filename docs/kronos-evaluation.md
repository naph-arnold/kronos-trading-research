# KR-04: Binance/Kronos-mini one-step forecast evaluation

The [Kronos-mini](https://huggingface.co/NeoQuasar/Kronos-mini) model predicts **hourly BTC/USDT spot close prices** using verified Binance OHLC data from the repository's separate `market-data` branch.

## Verification and methodology

- Run `python -m research.evaluate_kronos --csv /path/to/BTCUSDT_1h.csv --symbol BTCUSDT --interval 1h --lookback 128 --evaluations 8 --output research/output/report.json`.
- The paired `.csv.json` SHA-256 manifest is validated before loading. Data must be from `binance_spot`.
- Each predicted closing price at index `t` uses only the preceding `lookback` candles: `[t-lookback, t)`. The held-out target at `t` is not passed into the model.
- Baseline: the immediately preceding candle's close. Metric: mean absolute forecast error (USDT). Equal target timestamps are evaluated for both methods.
- The model is loaded on CPU. Pretrained model files are fetched from Hugging Face on the first run.
- The GitHub Action `Kronos Binance forecast evaluation` runs on changes to the inference code on its development branch and allows manual runs. Its JSON output is retained as a GitHub Actions artifact for 14 days.

## Initial proof-of-execution

An actual GitHub-hosted run with 168 hourly candles, 128 lookback, and 8 held-out single-step predictions reported Kronos-mini MAE approximately **143.23 USDT**, versus previous-close MAE **154.26 USDT**. This tiny, temporally adjacent sample is **not** credible evidence of future profitability, nor statistically sufficient evidence of forecasting superiority. No trading strategy, trades, fees, spreads or portfolio risks were evaluated.

## Next evidence standard

Gather substantially longer Binance history; record model/tokenizer exact model revisions and environment; repeat walk-forward evaluations over different regimes and forecast horizons; report directional accuracy, baseline uncertainty, transaction costs, and out-of-sample dispersion. Keep real-money execution disabled.

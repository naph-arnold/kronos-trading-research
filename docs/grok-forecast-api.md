# Kronos read-only prediction API for Grok and other agents

This is an **integration boundary**, not a Grok connection or a deployed internet service. It exposes verified Binance BTCUSDT spot hourly forecasts as structured JSON. Grok's function-calling integration can call a separate trusted adapter which in turn calls this API. No Grok credential is needed by Kronos; no Binance trading credentials or order routes are present.

## Preconditions

Install Kronos runtime dependencies with Python 3.11 (`requirements.txt`). Download/update validated Binance market data, preserving its `.csv.json` integrity manifest. The service **requires fresh closed candles** (at most two hours behind the latest fully completed hourly boundary) and refuses a stale or modified dataset.

Set a locally generated secret token in your shell, **never in source code or GitHub**:

```bash
export KRONOS_API_TOKEN="replace-with-a-random-32+-character-secret"
python -m research.forecast_http --dataset /absolute/path/BTCUSDT_1h.csv --host 127.0.0.1 --port 8765
```

Example request from the same machine:

```bash
curl --fail --request POST http://127.0.0.1:8765/v1/forecast \
  -H "Authorization: Bearer ${KRONOS_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"horizon": 1}'
```

`GET /healthz` returns only the service status; `POST /v1/forecast` is the only forecasting route. Requests cannot choose arbitrary model files, dataset paths, symbols, execution modes, brokers or order parameters. All other routes return 404. The HTTP listener is limited to loopback addresses; it is not a production-hardened public API.

## JSON response contract

The endpoint returns schema-versioned JSON with `mode: research_only`, `trading_enabled: false`, `source`, `symbol`, `interval`, `model`, `tokenizer`, `sampling`, `lookback`, `horizon`, `last_closed_candle_timestamp`, `dataset_sha256`, `last_observed_close`, `forecast` and a `warning`. Forecast entries contain predicted open/high/low/close and future UTC timestamps. There is **no buy/sell recommendation**. Confidence scores and tradable probabilities are not claimed.

Currently the provider is Binance BTCUSDT spot, 1h candles, `NeoQuasar/Kronos-mini` with its matching `Kronos-Tokenizer-2k`, deterministic sampling parameters and 1–24 future hourly predictions.

## Grok integration pattern

1. Configure Grok function calling with an explicitly bounded `get_kronos_forecast(horizon)` tool schema.
2. Implement the trusted function handler outside Grok: authenticate to this local API, validate JSON `schema_version`, confirm `trading_enabled=false`, `source=binance_spot` and `dataset_sha256`, and return **analysis-only** fields.
3. Grok can summarize forecasts, compare them with baseline and backtest evidence, and prepare human-readable reports.
4. Grok must **not** receive a tool that places real orders as part of this integration. Risk approval and any future broker interface remain independent and disabled.

This module does not configure an xAI API key, call an xAI endpoint, expose a public URL or establish live Grok communication. For remote agent access, use an authenticated TLS proxy with rate limits and strict access controls after a separate security review; do not bind this prototype directly to the public internet.

Official references: [Kronos GitHub](https://github.com/shiyu-coder/Kronos), [xAI function calling](https://docs.x.ai/developers/tools/function-calling), [Binance market-data-only API](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md).

# TradingView alert ingress for the six-agent Kronos research desk

This is a **receiver implementation and chart-alert template**, not an active TradingView integration. It cannot place orders, trigger paper fills, or connect directly to a Grok bot. The only persistent side effect is storing eligible research alerts in a SQLite inbox; the six-role handoff will be added after authenticated provider integration.

## TradingView constraints and authentication

TradingView's [webhook documentation](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) says alerts are delivered as HTTPS POSTs, only public ports **80/443** are allowed, and receivers must respond within approximately **three seconds**. TradingView requires account 2FA for webhook alerts. [Their security documentation](https://www.tradingview.com/support/solutions/43000722015-using-credentials-for-webhooks/) advises against passwords or login credentials in webhook URLs or alert bodies.

TradingView cannot supply our internal `X-Trusted-Ingress` header directly. **Do not point TradingView to the local Python listener.** The required production pattern is:

1. TradingView calls a public `https://your-domain.example/v1/tradingview/alerts` endpoint.
2. A separately deployed, trusted HTTPS edge authenticates the webhook source (prefer TradingView's [documented certificate-based webhook authentication](https://www.tradingview.com/support/solutions/43000680459-webhook-authentication/) through a correctly configured TLS termination and verified mTLS/certificate validation, with source IP allowlisting as defense in depth). A plain IP match is not a replacement for proper authentication. The edge must remove any incoming `X-Trusted-Ingress` and inject its own private header when forwarding to loopback.
3. The local listener accepts only loopback traffic bearing that private header; it validates JSON schema, BTCUSDT on BINANCE 1h, completed-bar age and positive close.
4. SQLite durably deduplicates by the exchange/symbol/interval/event/bar-start key. Accepted records stay `research_pending`. No automatic forwarding or order is performed.
5. A future authenticated worker independently verifies Binance candles at the timestamp before giving the signal to Scout, Chart, Risk and Skeptic. Treat TradingView's JSON as **untrusted** until verified.

**No public endpoint, TLS certificate, external identity-verifying ingress, provider account, or deployed worker has been set up yet.** Exposing this service unprotected to the internet is unsafe.

## Local receiver (for internal tests, not direct TradingView connection)

Generate a unique high-entropy `TRUSTED_INGRESS_TOKEN` and set it only on the trusted edge and local environment. Never commit tokens, include them in Pine Script or send them in TradingView alerts.

```bash
export TRUSTED_INGRESS_TOKEN="GENERATE_A_UNIQUE_RANDOM_32_OR_MORE_CHARACTER_TOKEN"
python -m research.tradingview_ingress --db ./runtime/research_alerts.sqlite --host 127.0.0.1 --port 8766
```

`POST /v1/tradingview/alerts` is only for the trusted reverse proxy. `GET /healthz` reports process status, not exchange connectivity. HTTP responses: 202 on first queued alert; 200 for exact duplicates; 400 invalid/stale signals; 401 missing trusted proxy authentication; 415 non-JSON; 503 storage failure. This prototype is single-host SQLite storage, not a high-availability queue.

The local research inbox may be read by a future consumer using `AlertInbox(path).list_pending()`. Reading does **not** make a signal eligible for simulated or live execution.

## TradingView chart and alerts

- Use the checked-in `tradingview/kronos_research_alerts.pine` (Pine Script v6), open a **BINANCE:BTCUSDT** hourly chart, and add it as an indicator.
- Create a bullish crossover alert and a bearish crossover alert separately; select **Once Per Bar Close**. Confirm symbol/timeframe in the chart because alert messages intentionally label BTCUSDT/1h and the receiver rejects other labels.
- The indicator produces JSON with TradingView's `{{time}}` (bar opening timestamp) and `{{close}}`. It is *only an example technical trigger*, not a profitable signal.
- Once and only once the verified HTTPS edge is deployed, configure its actual URL under TradingView's alert Webhook URL action. Webhook alerts need 2FA. Observe delivery health and failed alert log entries.
- The endpoint rejects signals for unfinished candles and alerts arriving more than two hours after a candle closes. This is intentionally strict: delays are not silently treated as current opportunities.

## Hard safety boundaries

- The Pine indicator never sends a buy/sell execution instruction.
- TradingView cannot approve any risk checks or bypass the Skeptic.
- The current six-agent decision gate defaults to simulation disabled and kill switch enabled.
- **The last 240-forecast Kronos strategy test returned -9.98% under assumed costs. Do not enable real funds or assume these EMA events have an edge.**
- No automatic 24/7 Grok discussion or deployed public webhook has been established as part of this code change.

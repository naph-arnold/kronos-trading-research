# Kronos Trading Research — Dual-Market Demo Architecture

Status: **Design approved, implementation not verified** (2026-11-06).

## Fixed product decisions
- Markets: XAUUSD via MetaTrader 5 and BTC/USDT via Binance, isolated initially.
- Operation: **automated demo trading** only. No live order permissions or fallback.
- Risk: **at most 0.5% of current account equity intended loss per new trade**, based on stop distance and contract specifications; gaps, slippage and failures can exceed it.
- Core: Python orchestrator; Kronos forecasts are research inputs, not automatic trade authorization.
- AI: six role-based Grok reviewers (Scout, News, Chart, Kronos Analyst, Skeptic, Coordinator); AI consensus cannot bypass deterministic checks. See *AI architecture* below.
- TradingView: optional signal/alert ingestion; verify authenticity, timestamp freshness, deduplicate and never authorize solely on alerts.
- Independent per-market account state, orders, model performance and kill switches.

## AI architecture (authoritative)
| Component | Provider | Role | Authority |
| --- | --- | --- | --- |
| Scout, News, Chart, Kronos Analyst, Skeptic, Coordinator | **Official xAI Grok API** (`https://api.x.ai`, scoped `XAI_API_KEY` stored outside Git) | Reasoning/review agents producing versioned, schema-validated advisory outputs | Advisory only |
| Kronos (`model/`) | Local Kronos foundation model | Financial time-series forecasting | Advisory only |
| TradingView | Webhook alerts | Optional signal/alert input | Advisory only |
| Deterministic risk engine (`trading/risk.py`, `trading/gate.py`) | Python code, no AI | Sizing, limits, vetoes | **Sole approval authority** |
| Claude Code | Anthropic (development tooling) | Writes, tests and documents this repository | **None at runtime** — not part of the trading decision path |

Rules: no other LLM provider acts as a trading agent; Grok agents consume Kronos output rather than forecasting prices themselves; all AI outputs are recorded in the audit trail as `advisory` inputs and are never passed to the risk engine; when Grok is unavailable the system degrades to no-trade or non-AI baselines, never to relaxed limits.

## Logical flow
Market data (MT5, Binance, TradingView) -> normalization and freshness validation -> strategy-specific candidate signal -> Kronos/Grok enrichment and adversarial review -> deterministic risk checks -> *separate* demo execution adapters -> order reconciliation and audit -> monitoring.

### Gold adapter
MT5 broker demo credentials, XAUUSD instrument metadata, tick value/size, lot steps, session calendar, spread, stop level and filling rules. Python may coordinate signals; a minimal MQL5 Expert Advisor or MT5 Python adapter executes and reconciles demo orders. Never assume that the MQL5 Strategy Tester runs external AI calls; test MQL5 behavior separately and replay AI decisions offline.

### Crypto adapter
Binance Spot Testnet for BTC/USDT only initially, with testnet-specific keys and endpoints. Fetch symbol filters (tick size, step size, minimum notional), fee model and lot sizes, and reconcile fills and balances. Spot mode is long/flat, not leveraged shorting. Handle testnet resets. Never use production credentials during demo mode.

## Risk contract (per account)
1. Validate demo environment, signal age, allowed symbol, market state, feed freshness, order and position reconciliation, and no active circuit breaker.
2. Compute equity-based risk budget: `risk_budget = account_equity * 0.005`. For MT5, translate stop distance and contract specifications into lot size; for Binance spot, calculate stop-distance exposure and exchange filters. Round **down** and reject if minimum size would exceed risk.
3. Reject if stop is missing/invalid, cost estimates push risk beyond budget, free margin/balance is inadequate, duplicate signal/order exists, or exchange protections cannot be enforced.
4. Initial *proposed* additional guardrails, to validate: 1.5% daily equity loss halt and 1% aggregate open stop-risk cap per account; also impose limits on concurrent positions, stale quotes, maximum spread/slippage and connection failures.
5. No live execution: `EXECUTION_MODE=demo` and live credentials not available to the process. Fail closed on any disagreement.
6. Realized loss can exceed planned risk due to gaps, partial fills, slippage and stop failures.

## Strategy boundaries
- XAUUSD: GoldPilot V1 (trend/pullback), order-block retest, liquidity sweep/structure shift, opening-range breakout — hypotheses until independently verified.
- BTC/USDT: Kronos-derived signals with trend filter and separate non-AI baselines; previous poor results must not be promoted on unverified AI claims.
- Keep each strategy versioned, isolated, measurable, and subject to out-of-sample testing and net-cost evaluation.
- Six agents are analysis roles, **not** six distinct profitable strategies.

## Operational security and reliability
- Store secrets outside Git, use testnet/demo-only scoped keys, no withdrawal privileges, rotate leaked credentials.
- Every order has idempotency key, pre-trade snapshot, signal provenance, agent/model version, risk evaluation, acknowledgment, fill reconciliation and immutable audit record.
- Circuit breaker on stale data, dropped feed, duplicated orders, desynchronization, unexpected exposure, or disabled emergency stop.
- Monitoring shows strategy P&L net of fees/slippage, drawdown, order rejects, latency and system health.
- Continuous operation means infrastructure availability, not guaranteed 24/7 gold market availability.

See [Implementation roadmap](ROADMAP.md).

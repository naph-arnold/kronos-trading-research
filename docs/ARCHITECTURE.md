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

## Market-calendar intelligence (Phase 2)
```
providers (official/licensed, attributed)        market-specific rules              enforcement
 FOMC (federalreserve.gov) ─┐                    XAUUSD: USD high ±30m,             risk.evaluate(event_guard=
 BEA (GDP, PCE)            ─┤  CalendarService   central bank -60/+90m,              combine_guards(
 ECB decisions             ─┼─► EventStore ──────► required series FOMC/CPI/NFP/      EventBlackout, MarketHealthMonitor))
 operator/licensed file    ─┤  (revisions,       GDP/PCE                            + PreTradeGate preconditions
   (CPI, NFP schedules)    ─┘   refresh/failure, BTCUSDT: USD high -15/+30m,        (verified spec, verified XAUUSD
 BLS API values (actual/prev)    contradictions) required FOMC/CPI; 24/7 sessions     sessions, mandatory guard)
 CME gold rules + broker closures ──► TradingCalendar (template → rule_based → broker_verified)
 venue status / maintenance / quotes ──► MarketHealthMonitor (per market)
```
- Deterministic, provider-attributed data only: CME Globex gold holidays/early closes (rule-based library), official
  Federal Reserve FOMC calendar, BEA release schedule, ECB meeting calendar, BLS API values, operator/broker closure
  notices, and licensed economic-calendar feeds once chosen. CPI/NFP *schedules* currently require a licensed provider
  or operator file (the BLS schedule website refuses automated access); until configured, required-series coverage
  fails closed.
- Shared event store with revision history; market-specific rules (XAUUSD and BTCUSDT have separate calendars,
  blackout rules and breakers).
- Closures from all sources are unioned (most restrictive wins). The risk engine's `event_guard` blocks new entries
  around high-impact events and fails closed when required calendar data is stale. AI agents may read the calendar
  but cannot open a closed market or shorten a blackout.
- Venue connectors in Phase 2 are read-only (MT5 allowlist proxy; Binance GET-only path allowlist).
- Crypto is never blocked merely because traditional markets are closed; it has its own blackout rules and health
  monitor (exchange status, maintenance, spreads, data gaps).

## Grok News Agent integration plan (Phase 4)
1. Adapter implementing `trading.agents.news.NewsAgentPort` calls the official xAI Grok API with read-only context:
   upcoming/recent events from `EventStore`, market health flags, and the Kronos forecast summary.
2. Output must pass `parse_news_assessment` (schema v1; known event IDs only; no controlled fields; freshness).
3. Assessments go to the Coordinator and the audit log as advisory inputs. Optionally (operator setting) an
   `avoid_new_entries` recommendation adds a temporary restriction via `advisory_guard`; it can never remove one.
4. On timeout/invalid output the adapter raises `NewsAgentUnavailable`; verified closures, blackouts and risk limits
   continue unchanged.

## Research validation protocol (Phase 2C)
- Data partitions are explicit: training (diagnostics, sensitivity, baselines), a holdout that may be evaluated once,
  and genuinely unseen forward data for final validation. A holdout once inspected is never reused for selection.
- Every strategy is compared per trade with random-entry baselines using the same exits, costs, risk engine and
  long/short mix, and with a simple non-AI trend baseline; beating unbiased random entries in a trending market is
  not evidence of skill.
- Cost model: verified tick value, tick-sampled spreads (bar `spread` is a minimum), verified swap; commission and
  slippage are labelled estimates with stress scenarios.
- Forward tests are pre-registered (frozen parameters, criteria, checkpoints) and run in shadow mode (read-only data,
  simulated fills, no order calls) until Phase 3 is explicitly authorized.

## Shadow forward testing (Phase 2D)
- `trading/shadow/` is a simulation engine with **no order-routing code** (a structural test rejects order calls). Market
  data arrives only through the read-only MT5 allowlist proxy; the account must report the demo trade mode.
- Event order is strict: a bar-close event at C precedes ticks stamped ≥ C; signals fill at the first tick ≥ C.
  Entries need a fill tick processed within 30 s of real time; catch-up after downtime manages open simulated
  positions but rejects entries ("system not live").
- Every simulated entry passes the unchanged `trading.risk.evaluate` plus the pre-trade gate's preconditions
  (broker-verified sessions, fresh spec, mandatory calendar/market-health guard, durable duplicate claims), per
  hypothetical book (strategy × equity × commission scenario) with its own daily-loss ledger.
- State (bars, signals, every decision, positions, trades, equity marks, events, heartbeats) lives in one SQLite
  file; strategy state is rebuilt deterministically from stored bars and must reproduce recorded signals.
- Pre-registration is enforced in code: config ↔ pre-registration file hash, parameter hashes and risk fraction.
  Details: [phase2d-shadow-forward-test.md](phase2d-shadow-forward-test.md).

## BTC/USDT shadow pipeline (Phase 2E)
- Public market data only: REST klines/exchangeInfo/time/bookTicker (`data-api.binance.vision`) and the receive-only
  WebSocket `bookTicker` stream (`data-stream.binance.vision`) with reconnection, REST fallback, gap recording and
  server-time-corrected UTC stamps. No credentials.
- Same shadow engine as XAUUSD (`EngineSettings.market`/`interval`), separate process, worktree, state, breaker,
  daily ledger, calendar store and reports. Spot is long/flat; fees per side on notional; no swap.
- Phase 3 preparation (`trading/execution_prep/`): order intents, filter validation, reconciliation and testnet
  reset detection with no network code; submission raises until Phase 3 is authorized.
  Details: [phase2e-binance-shadow.md](phase2e-binance-shadow.md).

## Multi-timeframe research framework (research only)
- `trading/mtf/` builds higher timeframes from execution candles (UTC-aligned buckets) and exposes a higher-timeframe
  candle only after it has closed. All structure features are formally defined and causal; prefix-invariance tests
  guard against look-ahead.
- Strategies emit candidates. The simulator sends every entry through the unchanged `trading.risk.evaluate`
  (0.5% / 1% aggregate / daily 1.5% latch, sessions, blackouts, venue filters). Research halts can only add
  restrictions. Hybrid conflicts are resolved deterministically under one account's limits.
- Advisory inputs (Kronos, six Grok agents on the official xAI API, TradingView) are recorded only. The simulator
  and risk engine cannot receive them, and live Grok calls are not activated.
  Details: [mtf-hybrid-research.md](mtf-hybrid-research.md).

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

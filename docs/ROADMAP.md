# Dual-Market Automated Demo Trading — Implementation Roadmap

Approved 2026-11-06. This is a plan, **not evidence that milestones are complete**.

## Definition of done
Two isolated pipelines reliably place and reconcile simulated/testnet orders based on independently tested strategies, with risk budget capped at 0.5% intended loss per trade. Live trading remains disabled. Metrics and audit logs allow a strategy-by-strategy go/no-go decision.

| Phase | Scope | Completion gate |
| --- | --- | --- |
| 0 — Inventory and baseline | Audit current fork, Python environment, existing scripts, tests, historical datasets, assumptions, and previous results. Preserve upstream Kronos. | Reproducible environment; documented gaps and dependency versions. |
| 1 — Shared foundations | Canonical OHLCV/bar timestamps, data validation, strategy signal schema, account/position state, structured logs, replay harness and deterministic risk module. | Unit tests for size rounding, 0.5% cap, duplicates, stale feeds and fail-closed behavior. |
| 2A — XAUUSD isolated research | MT5 demo account, broker symbol metadata and history; baseline backtests for GoldPilot, order blocks, liquidity sweeps and opening-range breakout, with realistic spread/slippage. | Out-of-sample results and costs recorded for each strategy; no AI dependence. |
| 2B — BTC/USDT isolated research | Binance Spot Testnet account, historical crypto data, order filters; Kronos forecast baseline versus trend/breakout baselines. | No leakage, walk-forward results and net trading cost estimates; no unvalidated model promotion. |
| 2C-R — Strategy validation and robustness (added 2026-11-06) | Training-only diagnostics, parameter sensitivity, random/trend baselines, verified vs estimated costs (tick spreads, swap), minimum-lot eligibility, pre-registered forward test. | Strategy-by-strategy evidence; only strategies beating matched random baselines are forward-tested; forward test is shadow-only (no orders) and pre-registered. |
| 2D — XAUUSD shadow forward test (added 2026-11-07) | Read-only live MT5 data, frozen ORB New York vs H1 trend and matched random controls, unchanged risk engine, simulated fills/costs from live ticks, 10,000 and 3,000 USD books, persistent restart-safe state, reports against the pre-registered criteria. | No orders possible; pre-registered criteria evaluated only at the final checkpoint (≥ 100 trades and ≥ 2027-05-09); uptime and data interruptions reported. |
| 2C — Market-calendar intelligence (added 2026-11-06) | Automated official calendars (FOMC, BEA, ECB, BLS values), CME gold holidays, broker closures, event store, deterministic blackouts, market-health guards, Grok News Agent contract. | Verified closures enforced; stale/contradictory/missing calendar data rejects entries; calendar works without Grok. |
| 2E — Binance independent shadow validation | Finish and verify the Python BTCUSDT read-only shadow runner independently from frozen XAUUSD NY ORB, with persisted simulated accounts, market data freshness and research controls. | Both Python shadow pipelines demonstrably run with no venue order route; record market-by-market provenance, simulated results, risk checks and interruptions. |
| 3 — Demo execution adapter evaluation (requires separate approval) | Compare terminal-backed Python MT5 demo adapter to optional native MQL5 EA for MT5 Strategy Tester, execution and reliability advantages, per ADR-001. Binance Spot Testnet demo adapter separately; idempotency and reconciliation. | Document measurable MQL5 justification before adding an EA; only if Phase 3 demo execution is authorized, verify demo orders, partial fills/rejections, risk veto and no live credentials. |
| 4 — AI intelligence integration | Scout, News, Chart, Kronos Analyst, Skeptic and Coordinator; TradingView webhook verification/freshness/deduplication. | Agent outputs versioned; deterministic risk veto cannot be overridden; graceful degradation when AI unavailable. |
| 5 — Continuous demo validation | Deploy monitored services with dashboard, daily risk halts, alerting, health checks and audit export. | Multiple market conditions observed; uptime/error metrics, drawdowns and costs reported separately by market. |
| 6 — Review only (no automatic live rollout) | Compare strategy variants to non-AI baselines, analyze robustness and operational failures. | Explicit separate authorization required for any live-capital design or rollout. |

## Implementation language and platform decision
See [ADR-001: Python central engine, MQL5 only if justified](adr/001-python-first-optional-mql5.md). Phases 2A/2B/2D/2E are Python-based research and shadow testing; MQL5 is **not** a Phase 2 dependency. Preserve frozen gold forward-test parameters while building Binance shadow. No execution changes without separate approval.

## Dependency order
Start with Phase 0, shared foundations and risk tests. Phase 2A and 2B can proceed in parallel **after** shared signal schemas stabilize. Do not connect automatic execution until simulator tests pass. Integrate AI after baseline strategies are measurable to avoid attributing improvements to the wrong component.

## Acceptance tests (mandatory)
- Reject oversized or under-minimum orders instead of rounding upward over 0.5%.
- Correct position size for XAUUSD contract and tick value; handle margin, spread and minimum stops.
- Correct Binance filters, fee deductions, partial fills, balance locking and testnet reset/reconciliation.
- Daily loss and aggregate exposure breakers (initially proposed 1.5% and 1% per account) trigger and block new orders.
- Reject stale market data, missed TradingView webhook, unauthorized payload, duplicate events and missing stop orders.
- Disable demo order placement safely when broker/exchange state is unknown, even if all AI agents vote yes.
- Verify **live orders are structurally impossible** in demo deployment.
- Backtest time alignment, no lookahead, market hours, fees, spread, funding where relevant, slippage and realistic stop execution.
- Audit every decision; collect net P&L, max drawdown, win rate, profit factor, exposure, latency, cancellations and rejections.

## Immediate next implementation ticket
Audit existing repository layout and identify any already implemented adapters, strategy files and tests. Then implement shared risk engine and simulator tests before adding more strategies or agent prompts.

Sources: upstream Kronos repository https://github.com/shiyu-coder/Kronos ; MT5 Python integration https://www.mql5.com/en/docs/python_metatrader5 ; MT5 tester https://www.metatrader5.com/en/terminal/help/algotrading/testing ; Binance Spot Testnet https://testnet.binance.vision/ .

# Kronos Trading Bot — Verified Progress

Updated: 2026-11-06

## Approved decisions
- XAUUSD via MetaTrader 5 demo and BTC/USDT via Binance Spot Testnet.
- Automated demo trading only; live execution strictly disabled.
- Maximum intended risk per new trade: 0.5% account equity.

## Verified documentation
- [x] Architecture and roadmap drafted in PR #9.
- [x] CLAUDE.md session instructions drafted in PR #9.
- [ ] PR #9 merged into default branch. (Local `master` contains the docs commits `0000ec2`..`98f1c20` and is 4 commits ahead of `origin/master`; not pushed by Claude.)
- [x] Local Claude Code checkout includes the new docs.
- [x] AI architecture made explicit in `CLAUDE.md` and `docs/ARCHITECTURE.md`: six agents on the official xAI Grok API, Kronos as forecaster, Claude Code as development tool only, no AI bypass of deterministic risk (Phase 1 branch).

## Technical implementation
- [x] Phase 0: Inventory existing repository and confirm actual baseline and tests (2026-11-06, see below).
- [x] Phase 1: Shared schema, risk engine, test suite and data validation — implemented and verified locally on Python 3.11 (CI pins) and 3.14; pull request open for review (see milestones below). Not merged.
- [ ] Phase 2A: XAUUSD strategy research and MT5 demo data. **Code complete and tested with fakes/synthetic data; blocked on an MT5 demo terminal — no XAUUSD market results yet.**
- [x] Phase 2B: BTC/USDT strategy research — walk-forward + holdout results recorded (no robust edge; all experimental). Testnet account/fee verification pending testnet keys.
- [ ] Phase 3: Independent automated demo execution adapters.
- [ ] Phase 4: Kronos, six Grok agents and TradingView integrations. (Grok News Agent *contract* and approved role names done in Phase 2; no Grok API integration yet.)
- [ ] Phase 5: Monitored continuous demo validation.

---

## Milestone: Phase 0 — inventory and baseline (2026-11-06)

Branch: `feat/phase1-risk-foundations` (from local `master` @ `98f1c20`).

### Environment
- Host: Windows 11, Python 3.14.3 (only interpreter installed). Local `.venv` (git-ignored) created with
  `requirements-dev.txt` (pytest 8.3.5, ruff 0.11.13, pip-audit 2.9.0) plus unpinned numpy 2.5.4 / pandas 3.1.1
  because the CI pins (numpy 1.26.4) have no Python 3.14 wheels.
- CI (`.github/workflows/ci.yml`) uses Python 3.11 with numpy 1.26.4 / pandas 2.2.2. Local results on 3.14 are
  therefore *not* identical to CI's environment.
- PyTorch is not installed locally; upstream model regression test (`tests/test_kronos_regression.py`) was **not run**.

### Baseline test result
- `python -m pytest -q tests --ignore=tests/test_kronos_regression.py` → **82 passed** (3.9 s).
- `ruff check research tests` → all checks passed.

### What already exists (verified in source + tests) — do not rebuild
| Area | Files | Scope / limits |
| --- | --- | --- |
| Upstream Kronos model | `model/`, `finetune/`, `finetune_csv/`, `examples/`, `webui/` | Preserved upstream. |
| Binance public OHLCV ingestion | `research/market_data.py`, `research/datasets.py`, `research/coinbase_data.py` | Read-only public data API; UTC alignment, closed-candle, OHLC consistency, continuity checks; SHA-256 manifest. BTC only; no XAUUSD/MT5 data. |
| Kronos forecast evaluation | `research/evaluate_kronos.py` | Offline evaluation vs naive baseline. |
| Cost-aware BTC backtest | `research/backtest.py` | One-bar long/cash simulation. Recorded KR-05 result: **-9.98%** vs buy-hold -4.77% (`docs/backtesting.md`) — not a validated strategy. |
| Forecast API | `research/forecast_service.py`, `research/forecast_http.py` | Loopback, token-protected, read-only. |
| Six-agent desk | `research/six_agent_desk.py`, `research/agent_roles.py` | Paper-only veto contract and hash-chained JSONL audit (`append_audit`/`verify_audit`). Roles are `scout, news, chart, strategist, risk, skeptic` — **differs** from ARCHITECTURE.md (`Kronos Analyst`, `Coordinator`); reconcile in Phase 4. Its `PaperLimits` is BTC-only, float-based, notional-based; not the per-account risk contract. |
| TradingView ingress | `research/tradingview_ingress.py`, `tradingview/*.pine` | Schema/freshness/dedup (SQLite) for BTCUSDT 1h EMA alerts; research-only. |

### Gaps against ROADMAP (missing at baseline)
- No shared canonical bar type usable by both markets (existing validation is Binance-row/dict specific; no session-gap handling for XAUUSD).
- No strategy signal schema shared across markets.
- No account/position state model, instrument specification model (MT5 contract/tick/lot; Binance filters).
- No deterministic per-account risk engine implementing the 0.5% equity budget with round-down sizing, cost inclusion, 1.5% daily halt, 1% aggregate stop-risk, margin/balance, duplicate, staleness checks.
- No demo-only execution-mode guard (“live structurally impossible”), no circuit breaker / kill switch shared module.
- No structured logging, no replay harness for strategy signals through the risk engine.
- No MT5 integration of any kind; no Binance Spot Testnet (order) integration; no execution adapters (correct for this phase).

---

## Milestone: Phase 1 — shared foundations and deterministic risk (2026-11-06)

Branch: `feat/phase1-risk-foundations`, **uncommitted working tree** (awaiting review). Design notes: `docs/risk-engine.md`.

### Added
- `trading/` package: `common`, `config` (demo-only guard), `marketdata` (canonical UTC bars/quotes, validation, freshness),
  `signals` (schema v1, dedup/idempotency keys), `instruments` (MT5 XAUUSD + Binance BTCUSDT specs), `state`
  (account/position snapshots), `risk` (deterministic engine), `ledger` (SQLite duplicates), `breaker` (circuit
  breaker + kill file), `audit` (structured logs; reuses the existing hash-chain from `research/six_agent_desk.py`),
  `gate` (risk → claim → mandatory audit), `replay` (no-lookahead harness).
- Tests: `tests/test_trading_risk.py` (70), `tests/test_trading_gate.py` (27), `tests/test_trading_marketdata.py` (17),
  `tests/test_trading_replay.py` (7), shared fixtures `tests/trading_helpers.py`.
- CI (`.github/workflows/ci.yml`) now compiles, lints and runs the new tests. `.env.example` documents `EXECUTION_MODE=demo`.
- No order routing, broker connections or credentials were added. No existing module was modified.

### Verified results (local, Windows, Python 3.14.3)
- `.venv/Scripts/python -m pytest -q tests --ignore=tests/test_kronos_regression.py` → **203 passed** (82 baseline + 121 new).
- CI-equivalent commands from `ci.yml` (compileall, ruff file list, pytest file list) → pass; pytest **199 passed**
  (CI list excludes the pre-existing `tests/test_coinbase_data.py`).
- `ruff check research tests trading` → all checks passed.

### ROADMAP Phase 1 gate coverage
| Required test | Evidence |
| --- | --- |
| Size rounding (down only) | `test_xauusd_size_uses_tick_value_spread_slippage_and_commission`, `test_binance_size_respects_step_size_fees_and_slippage`, `test_size_capped_at_venue_maximum_rounded_down` |
| 0.5% cap | parametrized `test_*_size_never_exceeds_half_percent_of_equity`, `test_risk_fraction_above_hard_cap_is_rejected`, `test_under_minimum_lot_is_rejected_not_rounded_up`, `test_costs_alone_can_push_minimum_size_over_budget` |
| Duplicates | `test_duplicate_signal_id_and_same_bar_signal_rejected`, `test_duplicate_ledger_survives_restart` |
| Stale feeds | `test_stale_signal_rejected`, `test_stale_quote_rejected`, `test_stale_account_snapshot_rejected`, `test_feed_freshness`, `test_weekend_gap_makes_signal_stale` |
| Fail-closed | `test_garbage_inputs_never_raise_and_reject`, `test_unverified_environment_rejected`, `test_unknown_broker_state_blocks_even_if_all_agents_approve`, `test_audit_failure_rejects_and_trips_breaker`, `test_open_position_without_known_stop_blocks_new_orders` |
| Daily 1.5% / aggregate 1% breakers | `test_daily_loss_halt_at_one_point_five_percent`, `test_aggregate_open_stop_risk_cap_shrinks_then_blocks`, `test_daily_loss_halt_blocks_after_losses_within_a_day` (replay) |
| Live structurally impossible (for this phase) | `test_execution_mode_has_no_live_member`, `test_non_demo_environments_fail_closed`, `test_mt5_non_demo_account_modes_rejected`, `test_trading_package_contains_no_order_routing` |

### Not verified / blockers
- Not run on CI's Python 3.11 / numpy 1.26.4 (no 3.11 interpreter locally); code targets py310 syntax.
- Upstream model regression (`tests/test_kronos_regression.py`) not run (PyTorch not installed).
- Instrument values in tests are illustrative, **not** taken from a real MT5 broker or Binance exchangeInfo.
- Remaining Phase 1 items: persistent breaker state; per-adapter definition of `day_start_equity`; MT5 server-time → UTC
  conversion and XAUUSD session calendar (needed before Phase 2A); migrating `research/backtest.py` onto `trading.replay`
  is optional and was not done to avoid changing recorded KR-05 results.
- Role names differ between `research/six_agent_desk.py` and ARCHITECTURE.md (Phase 4 item).

### Next action
Review this branch; on approval, commit and run CI. Then finish remaining Phase 1 items (session calendar/UTC conversion,
persistent breaker) before Phase 2A/2B data work. No execution adapters until simulator tests pass and review is done.

---

## Milestone: Phase 1 completion — sessions, persistence, daily accounting (2026-11-06)

Branch: `feat/phase1-risk-foundations` (stacked on PR #9's docs branch), commits `43d0e84`, `1686299`, `325bada`.
Pull request: [#10](https://github.com/naph-arnold/kronos-trading-research/pull/10), opened for review; **not merged**.

### Added / changed
- `trading/clock.py`: MT5 server-time → UTC (`ServerTimeZone`, NY-close UTC+2/+3 with US DST, fixed offsets),
  conversion of MT5 `time`/`time_msc`, rejection of ambiguous/non-existent DST-switch server times, and
  `verify_server_offset` against a fresh tick (fails closed on mismatch or stale tick).
- `trading/sessions.py`: server-time `TradingCalendar`, UTC holiday/early-close closures, open/close buffers,
  `XAUUSD_NY_CLOSE_TEMPLATE`, `BTCUSDT_ALWAYS_OPEN`, `validate_session_bars` (bars inside sessions; gaps explained).
- `trading/breaker.py`: breaker trips/resets persisted atomically to JSON; restart keeps trips; corrupt/foreign
  state ⇒ tripped until a manual, operator-attributed reset.
- `trading/daily.py`: persisted `DailyLossLedger` (definitions in `docs/risk-engine.md`): server-day boundary for
  MT5, UTC day for Binance, conservative day-start equity, cash-flow adjustment, latched 1.5% halt, chronological
  observations.
- `trading/risk.py`: XAUUSD requires a session calendar (missing ⇒ reject); closed session/buffers ⇒ reject;
  latched daily halt ⇒ reject. 0.5% hard ceiling and round-down sizing unchanged.
- `trading/gate.py`: requires a `DailyLossLedger` (must share market and halt threshold with limits); always
  overwrites adapter-supplied day-start equity; ledger errors ⇒ reject. `trading/replay.py` uses the same ledger and
  calendar (XAUUSD replay without calendar raises).
- `CLAUDE.md`, `docs/ARCHITECTURE.md`: authoritative AI architecture section. `.env.example`: `XAI_API_KEY` placeholder.
- `.gitignore`: fixed a pre-existing line containing literal `
` sequences (its `data/raw/`, `data/processed/`,
  `research/output/`, `*.csv.tmp` patterns were not effective); added `.venv311/`.
- CI lists the new test files. No existing `research/` module was modified.

### Verified results
| Check | Environment | Result |
| --- | --- | --- |
| Full fast suite `pytest -q tests --ignore=tests/test_kronos_regression.py` | Python 3.11.18, numpy 1.26.4, pandas 2.2.2 (CI pins) | **250 passed** |
| Same | Python 3.14.3, numpy 2.5.4, pandas 3.1.1 | **250 passed** |
| CI `fast-checks` commands (compileall, ruff file list, pytest file list) | Python 3.11.18 | pass; pytest **246 passed** (list excludes pre-existing `test_coinbase_data.py`) |
| `pip-audit -r requirements.txt` | Python 3.11.18 | No known vulnerabilities found |
| Upstream model regression `tests/test_kronos_regression.py` | Python 3.11.18, torch 2.5.1+cpu, Kronos-small from Hugging Face | **4 passed** (94.8 s) |
| `ruff check research tests trading` | ruff 0.11.13 | All checks passed |
| GitHub Actions `Research CI` / `fast-checks` (manual dispatch on the branch, since CI triggers only on PRs to `master`) | ubuntu-latest, Python 3.11 | **pass** — ruff clean, **246 passed**, pip-audit clean ([run 42258475473](https://github.com/naph-arnold/kronos-trading-research/actions/runs/42258475473)); `model-regression` job skipped (opt-in input) |

Test files: risk 75, gate/demo-guard/AI-path 31, market data/signals 17, replay 10, clock/sessions 20,
breaker/daily 15 (168 new; 82 pre-existing research tests unchanged and passing).

### Risk controls covered by automated tests
0.5% cap and round-down sizing (parametrized over equities/stops, both markets); under-minimum and cost-driven
rejection; MT5 tick value, commission, margin, stops level, tick grid; Binance step size, fees, min notional, free
balance, long-only; stale/future signal, quote, snapshot; spread and entry deviation; duplicates (incl. restart);
unreconciled/non-demo account even with unanimous AI approval; breaker, kill file and persisted trips; latched 1.5%
daily halt (engine, gate, replay); 1% aggregate stop-risk; unknown-stop positions; pending orders; XAUUSD session,
daily break, weekend, buffers, holidays; MT5 server-time DST handling; demo-only environment; no order routing and
no AI provider imports in `trading/`.

### Remaining limitations (carried into later phases)
- Server time zone and session template are **configuration to confirm per broker** (contract spec + live demo
  tick via `verify_server_offset`); no built-in holiday calendar.
- Instrument specs in tests are illustrative; real values must come from MT5 `symbol_info`/`order_calc_margin`
  and Binance `exchangeInfo`/account commission (Phase 2A/2B/3).
- Breaker and daily ledger are single-process per market (no cross-process locking).
- Unrecorded deposits would mask losses in daily accounting; adapters must record cash flows/testnet resets.
- Planned risk is not a loss bound (gaps/slippage/partial fills; demonstrated by a replay test).
- Role names in `research/six_agent_desk.py` (`strategist`, `risk`) differ from the six Grok roles in
  ARCHITECTURE.md (`Kronos Analyst`, `Coordinator`); the Grok API adapter does not exist yet (Phase 4).
- CI `pull_request` trigger covers only `master`/`develop/trading-system`, so stacked PRs need a manual dispatch.

### Next development phase
Phase 2A/2B data foundations (parallel, no AI dependence): MT5 demo connection read-only (symbol metadata,
server-offset verification, UTC history with `validate_session_bars`) for XAUUSD; Binance Spot Testnet read-only
metadata/filters and balances for BTCUSDT; then baseline non-AI strategy backtests through `trading.replay` with
out-of-sample splits and net costs. No execution adapters (Phase 3) until reviewed.

---

## Milestone: Phase 2 — market research pipelines (2026-11-06)

Branch: `feat/phase2-market-research`, stacked on `feat/phase1-risk-foundations` (PR #10, itself stacked on #9).
Neither #9 nor #10 was merged at branch time. Code commit `9c3f1aa`, docs `d8a24b2`. Pull request:
[#11](https://github.com/naph-arnold/kronos-trading-research/pull/11) (open, not merged). Details and full tables: `docs/phase2-research.md`.

### Added
- Read-only connectors: `trading/connectors/mt5.py` (allowlist proxy, demo enforcement, symbol/spec discovery with
  verification status, server-time verification, UTC history with raw server times and spreads) and
  `trading/connectors/binance.py` (public data + Spot Testnet signed GET account; path allowlist; no credentials logged).
- Strategies: XAUUSD GoldPilot V1, Order Block Retest, Liquidity Sweep + MSS, Opening Range Breakout (London/NY);
  BTCUSDT Kronos forecast, trend following, breakout, Kronos-filtered trend, buy-and-hold benchmark. No prior
  GoldPilot implementation existed in the repository or its history.
- `trading/indicators.py`, `trading/evaluation.py`, replay extensions (HistoryView, spread model, time exits,
  gross/cost/net, exposure, event guard), EU DST rule and London/New York zones in `trading/clock.py`.
- Market calendar (`trading/marketcal/`): CME gold holidays/early closes, broker closures, official FOMC parser,
  revisioned event store, deterministic news blackouts wired into `trading.risk.evaluate`.
- Runners/configs: `research/phase2_btcusdt.py`, `research/phase2_xauusd.py`, `research/configs/`.
- `requirements-trading.txt` (pandas_market_calendars 5.5.0, MetaTrader5 5.0.6231 on Windows); CI installs/audits it.

### Verified results
| Check | Environment | Result |
| --- | --- | --- |
| Full fast suite | Python 3.11.18, numpy 1.26.4, pandas 2.2.2 | **313 passed** |
| Full fast suite | Python 3.14.3, pandas 3.1.1 | **313 passed** |
| CI fast-checks command list | Python 3.11.18 | **309 passed**; ruff clean |
| `pip-audit -r requirements-trading.txt` | 3.11 | No known vulnerabilities |
| BTCUSDT research run (twice, identical) | 3.11, commit `9c3f1aa` | See table below |
| GitHub Actions `fast-checks` (manual dispatch) | ubuntu-latest, Python 3.11 | **pass** — 309 passed, ruff clean, pip-audit clean for both requirement files ([run 42264691412](https://github.com/naph-arnold/kronos-trading-research/actions/runs/42264691412)) |

BTCUSDT holdout (2026-05-24 → 2026-11-06, 3,964 h; fee assumed 0.10%/side): trend +0.78% (12 trades),
breakout +1.11% (31), Kronos-mini −20.15% (247; +3.13% gross), trend+Kronos filter −0.18% (7), buy-and-hold −1.13%
(DD 25%). Walk-forward OOS (12 windows): trend −6.33%, breakout −15.45%. Kronos-mini holdout MAE 213.9 vs naive
187.9. **No robust edge; all strategies experimental.**

### Blockers / decisions needed
- **MT5:** no terminal on this machine; Phase 2A data, spec verification and XAUUSD backtests require an MT5 demo
  account (steps in `docs/phase2-research.md`). Broker commission must be supplied from broker documentation.
- **Binance testnet keys:** absent; fee rate is assumed until `/api/v3/account` commission rates are read.
- **Economic calendar provider:** CPI/NFP/GDP schedules and forecast/actual values need a licensed provider or
  operator-supplied files (BLS returned HTTP 403 to automated requests; not bypassed).
- **Instructions truncated** at Section 9.3; blackout behaviour implemented as interpreted; handling of open
  positions around events and any later sections still to be confirmed.

### Next step
Operator provides an MT5 demo terminal (and optionally testnet keys and a calendar provider decision); then run
Phase 2A discovery/download/backtest and testnet verification. Phase 3 execution adapters remain out of scope until
reviewed.

---

## Milestone: Phase 2 completion pass — Sections 9–16 (2026-11-06)

Branch `feat/phase2-market-research` (PR #11). Resumed after an interrupted session: verified that
`trading/marketcal/official.py` and the BEA/ECB/BLS fixtures were complete; fixed the missing
`EconomicEvent.series/reference_period` fields and a BEA title filter (regional "GDP by County" excluded).
Code commit `a8259e7`.

### Component status
Legend: **Completed** (implemented + tests) · **Verified** (also confirmed against live/real data) ·
**Partially implemented** · **Blocked** (external dependency) · **Not started**.

| Component | Status | Evidence |
| --- | --- | --- |
| Phase 1 risk controls (0.5% cap, daily 1.5% halt, 1% aggregate, breakers, duplicates, audit, demo-only) | Verified (tests, CI) | unchanged; all Phase 1 tests pass |
| MT5 read-only connector, spec discovery, server-time/UTC, history ingestion | Completed (mock MT5 only) | `tests/test_phase2_connectors.py` |
| MT5 live demo connectivity, broker specs, XAUUSD history | **Blocked** — no MT5 terminal/demo account | integration test skipped: `(-10003, 'IPC initialize failed, MetaTrader 5 x64 not found')` |
| XAUUSD trading-session calendar (template → CME rules → broker_verified) | Completed; broker verification Blocked | `test_calendar_verification_levels`, CME holiday tests |
| Four XAUUSD strategies, independently backtestable | Completed (synthetic data only) | `tests/test_phase2_strategies.py`, `tests/test_phase2_runners.py` |
| XAUUSD out-of-sample reports | **Blocked** (needs MT5 history) | — |
| Binance public connector + BTCUSDT metadata | Verified (live) | integration: exchangeInfo, bookTicker, server time passed |
| Binance Spot Testnet public filters | Verified (live; no differences vs production) | integration test passed |
| Binance testnet account / fee verification | **Blocked** — no testnet keys | integration test skipped |
| BTC historical data, Kronos vs non-AI comparison, costs, walk-forward | Verified (real data, reproduced twice) | `research/output/phase2_btcusdt.json`, `docs/phase2-research.md` |
| Official calendar ingestion: FOMC, BEA (GDP/PCE), ECB, BLS values | Verified (live) + recorded fixtures | integration: 4 sources passed; `tests/test_phase2_calendar_service.py` |
| CPI/NFP release *schedules* | **Blocked** — BLS schedule pages return 403 to automated clients; licensed provider or operator file needed | fail-closed coverage test |
| CME gold holidays/early closes; broker closure overrides | Completed | `tests/test_phase2_marketcal.py` |
| Event store (revisions), refresh/failure tracking, contradictions, staleness | Completed | calendar-service tests |
| News blackouts (configurable, tested, entries only) + buffer sensitivity | Completed | blackout tests; research-period sensitivity table |
| Market-health monitor (status, maintenance, spreads, gaps, stale data, unverified session changes) | Completed (fed by connectors in Phase 3) | health tests |
| Gate: verified/recent specs, mandatory guard, broker-verified XAUUSD sessions | Completed | gate tests |
| Grok News Agent contract; six approved roles in research desk | Completed (contract only) | `tests/test_phase2_news_agent.py`, `tests/test_six_agent_desk.py` |
| Grok API integration (six agents) | Not started (Phase 4) | — |
| Execution adapters / order placement | Not started (Phase 3; not authorized) | structural tests forbid order paths |

### Test results (2026-11-06)
| Run | Environment | Result |
| --- | --- | --- |
| Baseline before changes | Python 3.11.18 | 313 passed |
| Default suite after changes (`-m 'not integration'`) | Python 3.11.18, numpy 1.26.4, pandas 2.2.2 | **355 passed, 8 deselected** |
| CI fast-checks command list | Python 3.11.18 | **351 passed**; ruff clean |
| Live integration (`-m integration`) | Python 3.11.18, network | **6 passed, 2 skipped (blocked: MT5 terminal; testnet keys)** — reported separately, not counted above |
| BTC research rerun | commit `a8259e7` | holdout/walk-forward identical to `9c3f1aa` |
| Secret / order-path scan of Phase 2 diff | `git diff e54e903..HEAD` | no credentials; order tokens only in tests asserting they are unreachable |

### Remaining external dependencies
1. MT5 demo terminal + account (and broker commission per lot) — unblocks Phase 2A data, spec verification,
   `broker_verified` sessions and XAUUSD OOS reports.
2. Binance Spot Testnet API key/secret (read-only use) — unblocks fee verification.
3. Economic-calendar provider decision (licensed vendor or operator-maintained official schedule files) for CPI/NFP.

### Next implementation milestone
Operator supplies (1)–(3); run `research.phase2_xauusd discover/download/backtest`, promote the XAUUSD calendar to
`broker_verified`, configure the CPI/NFP provider, re-run both pipelines and review. Phase 3 (demo execution adapters)
only after explicit authorization.

---

## Update policy
Only mark items done when code, tests or verifiable operational evidence exists. For every milestone add date, branch/commit, command/test results, blockers, and next step. Do not treat plans or historical conversation claims as verified implementation.

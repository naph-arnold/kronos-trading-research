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
- [ ] Phase 2A: XAUUSD strategy research and MT5 demo data.
- [ ] Phase 2B: BTC/USDT strategy research and Binance Spot Testnet data.
- [ ] Phase 3: Independent automated demo execution adapters.
- [ ] Phase 4: Kronos, six Grok agents and TradingView integrations.
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

Branch: `feat/phase1-risk-foundations` (stacked on PR #9's docs branch). Pull request opened for review; **not merged**.

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
- Not yet run on GitHub Actions; local evidence only until the PR's CI completes.

### Next development phase
Phase 2A/2B data foundations (parallel, no AI dependence): MT5 demo connection read-only (symbol metadata,
server-offset verification, UTC history with `validate_session_bars`) for XAUUSD; Binance Spot Testnet read-only
metadata/filters and balances for BTCUSDT; then baseline non-AI strategy backtests through `trading.replay` with
out-of-sample splits and net costs. No execution adapters (Phase 3) until reviewed.

---

## Update policy
Only mark items done when code, tests or verifiable operational evidence exists. For every milestone add date, branch/commit, command/test results, blockers, and next step. Do not treat plans or historical conversation claims as verified implementation.

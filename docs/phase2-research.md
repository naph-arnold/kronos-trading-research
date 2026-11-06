# Phase 2 — Market research pipelines (2A XAUUSD, 2B BTCUSDT)

Status 2026-11-06. **Research only. No execution, no order endpoints, no live credentials.** A positive
backtest does not authorize demo execution (Phase 3 requires separate review).

## What exists

| Component | Module | Notes |
| --- | --- | --- |
| MT5 read-only connector | `trading/connectors/mt5.py` | Allowlist proxy: only read functions reachable. Demo trade mode enforced (else shutdown). Symbol discovery (exact, suffixed, or unique XAU/USD; ambiguity fails). Spec discovery collects contract size, tick size/value (incl. loss tick value), lot min/max/step, base/profit/margin currency, order types, filling modes, stops/freeze levels, current spread, trade mode, margin per lot (`order_calc_margin`), server/company. Missing or contradictory fields keep the spec **unverified** and `to_instrument_spec()` refuses it. Commission is not exposed by MT5 and must be supplied from broker documentation. |
| MT5 time handling | `trading/clock.py`, connector `history()` | Server wall time → UTC via configured `ServerTimeZone`; raw `server_time` preserved per bar; ambiguous DST timestamps reject the whole request; offset verified against a fresh tick (`verify_server_time`) and independently by session structure (`compare_with_bars`). |
| Binance read-only | `trading/connectors/binance.py` | Public market-data host (no credentials ever sent) and Spot Testnet (signed **GET** `/api/v3/account` only). Path allowlist, no POST/DELETE. Filters (PRICE_FILTER, LOT_SIZE, NOTIONAL), precision, status, order types, fee source (`assumed` until testnet commission rates are read). Keys never logged, repr'd or returned. |
| Strategies | `trading/strategies/xauusd.py`, `crypto.py` | Explicit rules in docstrings; causal (prefix-invariance tests); standardized `TradeSignal` with params hash and context provenance. |
| Evaluation | `trading/evaluation.py` | Reserved final holdout; walk-forward parameter selection on train only; `TradingWindow` (warm-up without signalling); metrics (net/gross, annualized ≥90 days, drawdown, win rate, PF, avg trade, avg R, costs, exposure, Sharpe/Sortino/Calmar); causal regime labels; conservative classification (never "execution-ready"). |
| Replay additions | `trading/replay.py` | `HistoryView` (future bars unreachable), per-bar spread model (MT5 recorded spreads), time exits, gross/cost/net per trade, exposure, `event_guard`. |
| Market calendar | `trading/marketcal/` | CME Globex gold holidays/early closes; broker closure overrides; official FOMC calendar; revisioned event store; deterministic news blackouts in the risk engine. |
| Runners | `research/phase2_btcusdt.py`, `research/phase2_xauusd.py`, `research/configs/` | Config-driven, dataset SHA-256 pinned, config SHA-256 and git commit recorded in the report. |

## Validation protocol (both pipelines)
1. Final `holdout_fraction` of bars is untouched until one final evaluation.
2. Research bars → walk-forward: grid search on each training window (minimum trade count), apply the winner to the following test window; report compounded OOS return, profitable windows and the average train–test gap (overfitting indicator).
3. Holdout parameters are chosen on all research bars only.
4. Costs: spread (per-bar model), adverse slippage on entry and stop, fees/commission, stop-first intrabar resolution, gap fills at the open, time exits at the close minus spread/slippage.
5. Every signal passes the Phase 1 risk engine (≤0.5% planned risk, round-down sizing, daily halt, aggregate cap, sessions, event blackouts).
6. Kronos forecasts must carry the dataset SHA-256 and may not overlap the research period.

Guarded failure modes with tests: lookahead (prefix invariance for every strategy, `HistoryView`, swing confirmation delay, resampler emits only completed hours), leakage (forecast/holdout overlap check, TradingWindow), timestamp alignment (server-time conversion, DST ambiguity), unrealistic fills (stop-first, gaps), missing costs (gross vs net), wrong contract specs (unverified specs refused), overfitting (walk-forward + untouched holdout, number of configurations reported). Survivorship bias is not applicable to single-instrument XAUUSD/BTCUSDT series.

## Phase 2B results — BTCUSDT 1h (Binance public data)

Run: `python -m research.phase2_btcusdt --config research/configs/phase2_btcusdt.json --output research/output/phase2_btcusdt.json`
(reproduced at commits `9c3f1aa` and `a8259e7` with identical holdout/walk-forward results). First commit `9c3f1aa`, config SHA-256 `5e77bfa0…`, dataset SHA-256
`d45a535d…` (26,424 candles, 2023-11-01 → 2026-11-06). Research bars 22,460; holdout 3,964 bars
(2026-05-24 20:00 → 2026-11-06 00:00 UTC). Costs: taker fee 0.10% per side (**assumed** — testnet commission not
verified), spread 1 bp, slippage allowance 10 USDT on entry and stop. Exchange filters from live public
`exchangeInfo` (tick 0.01, step 0.00001, min notional 5). FOMC blackout active (56 official meetings loaded).

**Holdout (single untouched period, ~5.5 months):**

| Strategy | Net % | Gross % | Max DD % | Trades | Win % | PF | Sharpe | Exposure % | Costs (USDT) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Trend following (EMA 50/200, ADX, 2.5 ATR stop, 3R) | +0.78 | +1.73 | 2.01 | 12 | 33.3 | 1.19 | 0.48 | 11.5 | 95 |
| Breakout (48-bar, volume ×2, 2.5 ATR stop, 2R) | +1.11 | +3.75 | 3.45 | 31 | 41.9 | 1.12 | 0.48 | 24.6 | 264 |
| Kronos-mini forecast (20 bps edge, EMA200 filter, 1-bar hold) | **−20.15** | +3.13 | 20.15 | 247 | 21.9 | 0.28 | −9.31 | 5.7 | 2,328 |
| Trend + Kronos filter | −0.18 | +0.42 | 1.79 | 7 | 28.6 | 0.93 | −0.13 | 7.5 | 61 |
| Buy-and-hold (fully invested, not risk-sized) | −1.13 | −0.89 | 25.02 | 1 | — | — | 0.15 | 100 | 23 |

**Walk-forward (research period, 12 windows of 2 months, 6-month training):**

| Strategy | Configurations | OOS compounded | Profitable windows | OOS trades | Avg train−test gap |
| --- | --- | --- | --- | --- | --- |
| Trend following | 24 | **−6.33%** | 6 / 12 | 65 | 2.58 pp |
| Breakout | 18 | **−15.45%** | 3 / 12 | 180 | 3.32 pp |

**Event-buffer sensitivity (research period only, FOMC events only; parameters selected on this same period, so
these are in-sample sensitivities, not OOS evidence)** — commit `a8259e7`:

| Blackout (before/after FOMC) | Trend net % (trades, blocked) | Breakout net % (trades, blocked) |
| --- | --- | --- |
| none | +2.11 (61, 0) | −3.01 (205, 0) |
| −15/+30 min (BTC default) | +2.11 (61, 0) | −3.01 (205, 1) |
| −30/+60 min | +2.11 (61, 0) | −2.03 (203, 3) |
| −60/+120 min | +2.11 (61, 0) | −2.03 (203, 4) |

FOMC-only blackouts affect very few hourly signals; CPI/NFP buffers cannot be evaluated historically until a
schedule provider is configured. Buffers therefore stay configurable defaults, not tuned values.

**Kronos forecast quality on the holdout:** MAE 213.9 USDT vs naive previous-close 187.9 USDT —
Kronos-mini was *worse* than the naive baseline over 3,964 one-step forecasts (consistent with KR-04/KR-05).
As a trading signal it was profitable before costs (+3.1% gross) but one-bar holding made fees and slippage
dominate (−20.2% net). As a filter it removed trend entries without improving them (7 vs 12 trades, −0.18%).

**Interpretation.** The small positive holdout results for trend/breakout are not supported by the walk-forward
evidence (both negative OOS over 2.5 years, train–test degradation present, trades few). No strategy shows a
robust net edge; all remain **experimental**. Nothing here supports Phase 3 promotion. Kronos parameters were
pre-registered (KR-05's 20 bps), not tuned; Kronos was evaluated on the holdout only because forecasts for the
research period were not generated (walk-forward Kronos evaluation is a possible next step, ~40 min CPU).

## Phase 2A status — XAUUSD (blocked on a demo terminal)

**No XAUUSD results exist.** This machine has no MetaTrader 5 terminal (`MetaTrader5.initialize()` →
`(-10003, 'IPC initialize failed, MetaTrader 5 x64 not found')`). Code paths are verified with a fake MT5 module
and SYNTHETIC data only; synthetic results are never reported as market evidence. To proceed (operator):
1. Install MT5, open a **demo** account with an XAUUSD broker, log in (or set `MT5_DEMO_LOGIN/PASSWORD/SERVER`,
   `MT5_TERMINAL_PATH` in an untracked `.env`).
2. `python -m research.phase2_xauusd discover --commission-per-lot <from broker docs> --output data/processed/mt5_xauusd_discovery.json`
   while the market is open (fresh-tick server-time verification).
3. `python -m research.phase2_xauusd download --start … --end … --output data/raw/mt5_xauusd_m15.csv`; review
   `calendar_check` in the manifest (bars in closed time / missing bars in open time = broker schedule or offset mismatch).
4. Copy `research/configs/phase2_xauusd.example.json`, adjust, run `backtest`. The runner refuses unverified specs.

## Market-calendar intelligence (Section 9)

### Provider evaluation (9.1)

Access checks on 2026-11-06 used an honest client identifier (`kronos-trading-research/0.2`); no access controls were
bypassed. "Live" = verified by the integration tests in `tests/test_integration_live.py` (6 passed, 2 blocked-skipped).

| Source | Endpoint(s) | Coverage | XAUUSD relevance | Update frequency | Pricing / rate limits | Reliability | History | Licence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `pandas_market_calendars` CMEGlobex_Gold | Python library (offline rules) | CME Globex gold sessions, holidays, early closes; future years | High (spot gold follows COMEX closures; brokers may differ) | Library releases | Free / none | Deterministic, no network | Back to 2000s and forward | MIT | **Implemented, verified offline** |
| Federal Reserve FOMC calendar | HTML `federalreserve.gov/monetarypolicy/fomccalendars.htm` | FOMC meetings (≈5 years incl. next year), SEP flags | Very high | When the Fed updates (dates set ~1 year ahead) | Free / no published limit (polite polling) | Official; layout change → parser fails closed | ~2021→2027 on page | US government work (public domain) | **Implemented, live-verified** |
| BEA release schedule | HTML `bea.gov/news/schedule` | Upcoming BEA releases (headline GDP, Personal Income & Outlays/PCE) | High | Weekly-ish; lists next ~6 weeks | Free | Official; year inferred from retrieval date | Upcoming only (historical schedule not on page) | Public domain | **Implemented, live-verified** |
| ECB meeting calendar | HTML `ecb.europa.eu/press/calendars/mgcgc/...` | Governing Council meetings to ~2028 | Medium (EUR; USD drives gold) | When ECB publishes | Free | Official | Future + recent | Reproduction permitted with source | **Implemented, live-verified**; not required by default rules |
| BLS Public Data API | JSON `api.bls.gov/publicAPI/v2/timeseries/data/{series}` | CPI, payroll *values* (actual/previous) | Very high | Monthly on release | Free; unregistered v2 ≈25 queries/day, registered key higher | Official | Decades | Public domain | **Implemented (values only), live-verified** |
| BLS release schedule pages | HTML/ICS on `bls.gov/schedule` | CPI, Employment Situation (NFP) dates | Very high | Annual + revisions | Free | Returned HTTP 403 to automated requests | — | Public domain | **Blocked** (not bypassed) |
| Trading Economics | REST API | Global calendar with forecast/actual/previous, importance | High | Real time | Paid (listings cite ~US$199/month); limits per plan | Commercial SLA | Yes | Commercial licence | Candidate — needs operator decision/key |
| Financial Modeling Prep | REST API | Economic calendar with estimate/actual/impact | High | Frequent | Paid tiers (listings conflict US$29–100+/month) | Commercial | Yes | Commercial licence | Candidate |
| Finnhub | REST API | Economic calendar | High | Frequent | Premium-only endpoint | Commercial | Yes | Commercial licence | Lower priority |
| Operator structured file | JSON via `structured_file_provider` / `load_events_file` | Anything with attribution | — | Manual | — | As good as the source | — | Per source | **Implemented** (stopgap for CPI/NFP schedules) |

Vendor pricing is from third-party listings found on 2026-11-06 (e.g. [Trading Economics API](https://www.tradingeconomics.com/api),
[FMP on Datarade](https://datarade.ai/data-products/economic-calendar-api-350-indicators-financial-modeling-prep),
[Finance Calendar comparison](https://www.financecalendar.com/?p=2927)) and must be confirmed on vendor pages.

**Decision needed (operator):** license one vendor for CPI/NFP (and forecast values), or supply official schedules as
attributed JSON. Until a provider covers `US_CPI`/`US_NFP`, the default XAUUSD guard rejects all new entries
(BTCUSDT needs `FOMC_DECISION` + `US_CPI`) — this is intentional fail-closed behaviour.

### Ingestion and data model (9.1/9.2)
`CalendarService.refresh()` runs every provider, checks attribution and series trust, upserts into `EventStore`
(revision history per event), records refresh time or failure (last good data kept). Each event records name,
country, currency, scheduled UTC time, impact, previous/forecast/actual when available, provider, retrieval time,
last update, status, series and reference period. Contradictions (same series, different providers, times
disagreeing by >1 min within 3 days) reject entries until resolved.

### News-related safeguards (9.3)
| Safeguard | Implementation | Tests |
| --- | --- | --- |
| Block new XAUUSD entries before selected events | `EventBlackout` rules: USD high-impact −30/+30 min; central bank −60/+90 min (configurable `BlackoutRule`) | `test_blackout_window_rules_and_currency_filter`, `test_central_bank_events_get_wider_window_for_gold` |
| Post-announcement restriction | `after` buffer per rule | same |
| Spread threshold | `RiskLimits.max_spread` + `MarketHealthMonitor` abnormal-spread multiple | `test_wide_spread_rejected`, `test_health_detects_...abnormal_spread` |
| Stale calendar rejection | per-provider max age; missing required series | `test_guard_fails_closed_without_cpi_nfp_schedule_provider_then_allows`, `test_stale_or_missing_calendar_fails_closed...` |
| Valid broker session information | gate requires `TradingCalendar.verification == "broker_verified"` for XAUUSD | `test_gate_requires_market_guard_and_broker_verified_sessions`, `test_calendar_verification_levels` |
| Buffers tested, not assumed optimal | configurable; research-period sensitivity in the BTC runner (below) | `test_guard_...`, runner report |
| Existing positions keep protective rules | guards gate *entries* only; replay keeps managing stops | `test_open_position_keeps_stop_management_during_blackout` |

### Cryptocurrency behaviour (9.4)
BTCUSDT uses a 24/7 calendar (never blocked by TradFi holidays: `test_btc_not_blocked_by_traditional_market_holidays`),
its own blackout rules (USD high-impact −15/+30 min; requires FOMC and CPI coverage) and its own
`MarketHealthMonitor` (Binance `exchangeInfo` status, maintenance windows from announcements, spreads, data gaps).
Geopolitical developments are interpreted by the Grok News Agent (advisory only); exchange maintenance is entered as a
`VenueWindow` with source and reference (Binance's announcement page is not a structured API).

### Disruption detection (9.5)
`MarketHealthMonitor` flags/rejects: unknown or stale venue status, suspension/close-only/halt, maintenance windows,
unverified session-change notices, no or stale market data, gaps in price updates, abnormal spreads. Markets are
isolated (`test_markets_are_isolated`). Any guard exception becomes a rejection (`test_guard_errors_fail_closed`).

### Grok News Agent (Section 10)
Contract only (`trading/agents/news.py`); see the integration plan in `docs/ARCHITECTURE.md`. Tests:
`tests/test_phase2_news_agent.py` (controlled fields rejected, unknown events rejected, non-Grok provider rejected,
advisory can only add restrictions, blackouts enforced while Grok is unavailable). The research six-agent desk now
uses the approved roles: scout, news, chart, kronos_analyst, skeptic, coordinator.

## Limitations
- BTC fee is assumed (no testnet keys configured); spread model is a 1 bp assumption, not observed order-book data.
- A single 5.5-month holdout and 12 walk-forward windows are small samples; 24/18 configurations per strategy were tried (multiple-testing risk).
- Bar-level fills: no partial fills, queue position, latency or order-book depth. Spot only (no funding).
- XAUUSD calendar template and server offset must be confirmed per broker; CME holidays are a proxy for broker schedules.
- Regime labels (EMA200 trend × ATR% vs rolling median) are coarse.

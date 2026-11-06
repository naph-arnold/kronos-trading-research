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

## Phase 2A results — XAUUSD on MetaQuotes-Demo (MT5, read-only)

All data below was retrieved on 2026-11-06 from the user's running MT5 terminal through `Mt5ReadOnlyConnector`
(allowlist proxy; no order functions reachable). No orders were placed, modified or cancelled.

### Account and terminal (verified)
| Item | Value |
| --- | --- |
| Server / company | MetaQuotes-Demo / MetaQuotes Ltd. |
| Account trade mode | 0 = **demo** (verified; non-demo would disconnect) |
| Currency / equity / leverage | USD / 3,000.00 / 1:100 |
| Terminal build / algo trading | 6306 / disabled in terminal (irrelevant: read-only) |

### XAUUSD contract specification (broker-reported, cross-checked)
| Field | Value | Status |
| --- | --- | --- |
| Symbol / path | `XAUUSD` / `Metals\XAUUSD` | verified (exact name) |
| Contract size | 100 oz | verified |
| Tick size / point / digits | 0.01 / 0.01 / 2 | verified |
| Tick value (loss) | **reported 0.1 USD — inconsistent**; terminal P&L (`order_calc_profit`) and 100 × 0.01 both give **1.00 USD/lot/tick** | resolved conservatively: **1.00 used** (larger loss per tick) |
| Volume min / max / step | 0.01 / 100 / 0.01 lots | verified |
| Currencies (base / profit / margin) | XAU / USD / USD | verified |
| Margin for 1 lot at ~4,075 | 4,076.53 USD (`order_calc_margin`) | verified |
| Stops level / freeze level | 0 / 0 points | verified |
| Order types | market, limit, stop, stop-limit, SL, TP, close-by | verified |
| Filling modes | FOK, IOC | verified |
| Trade mode | full | verified |
| Live spread at retrieval | 13 points (0.13 USD), floating | observed |
| Commission per lot | **unavailable** — MT5 does not expose it | **unverified** → scenarios 0 and 7 USD/lot round trip |

### Server time (verified, with a correction)
- Fresh-tick check (2026-11-06 18:57 UTC): server offset **UTC+2**.
- The previous default assumption (NY-close US-DST, `ny_close`) was **wrong for this broker**: converting 98,250 M15
  bars put 304 bars outside New York gold hours, all in US/EU DST-mismatch weeks. **EET with EU DST (`eet_eu`)** puts
  only 2 there. All data is converted with `eet_eu`; raw server timestamps are preserved per bar.

### Historical data (verified, SHA-256 pinned; files are git-ignored)
| Dataset | Bars | UTC range | SHA-256 |
| --- | --- | --- | --- |
| M15 (`data/raw/mt5_xauusd_15m.csv`) | 98,250 | 2022-09-01 00:00 → 2026-11-06 18:45 | `46ff39a2e08c…` |
| H1 (`data/raw/mt5_xauusd_1h.csv`) | 24,588 | 2022-09-01 00:00 → 2026-11-06 18:00 | `0cef2b86ab61…` |

Terminal limits: M15 depth is capped by the terminal's `maxbars` = 100,000 (earliest M15 bar 2022-08-05); H1 history
exists back to 2009, D1 to 2004. Requests are chunked (30 days) and require the symbol to be selected.

**Observed spreads (M15 bar spread, i.e. the bar's minimum):** median 0.07, p90 0.17, p99 0.33, max 2.81 USD.
Backtests floor the per-bar spread at 0.15 USD (live spread observed 0.13).

### Timestamp, gap and session validation
Against New York gold market hours + CME holiday/early-close rules (M15): 49 bars printed while the calendar was
closed and 760 bars missing while it was open. Categorized:
- **Bars while closed (49):** broker kept trading briefly after CME holiday early closes (e.g. Labor Day 17:00–18:15 UTC)
  and in the 2026 March DST-mismatch week. Calendar is stricter than the broker → conservative.
- **Broker session change in 2026:** from ~April 2026 the broker closes XAUUSD at **server 23:00** (daily break
  server 23:00–01:00, i.e. 16:00–18:00 New York), one hour earlier than the market close used through 2025
  (≈20 missing bars/month from April 2026). Detected from data; Section 9.5 "unverified session change".
- **Broker holidays/early closes:** 24 Dec 2024/2025 from 18:45 UTC, 4 Jul 2025, Memorial/Labor Day afternoons.
- **Data gaps:** 2025-07-02/04 (~66 bars), 2026-02-26 (~17 bars), 2025-01-07 (~11 bars), 2025-10-23 (~7 bars).

**Broker-verified calendar (for the live gate):** NY market hours + CME holidays, intersected with the broker's
observed server-time sessions (Mon–Fri 01:00–23:00 EET) over the last 8 weeks. Recent check: **0 bars missing while
the calendar says open** (3,537 bars), 20 printed while it says closed (the mismatch-week Sunday reopen, stricter is
allowed) → `broker_verified`. Backtests use NY hours + CME rules; broker bars are the ground truth for availability.

### Risk-engine compatibility (live quote, unchanged 0.5% rule)
Equity 3,000 USD → budget 15 USD per trade. Loss per lot = (stop distance + 2 × 0.10 slippage + spread) × 100 +
commission. Examples at ask 4,072.83: stop 3 USD → 0.04 lots (planned 12.80); stop 8 → 0.01 (8.20); stop 15 or 30 →
**rejected** (0.01 lot would risk 15.20 / 30.20 > 15). The `PreTradeGate` refuses this spec (commission unverified).

### Strategy backtests (M15, equity 3,000 USD, commission scenario 0, spread floor 0.15, slippage 0.10/side)
Run: `python -m research.phase2_xauusd backtest --config research/configs/phase2_xauusd_metaquotes_demo.json --output
research/output/phase2_xauusd_metaquotes_demo.json` (config SHA-256 `e3db10bf23c7…`, 32.5 min). Holdout = last 20%
(19,650 bars, 2025-12-30 → 2026-11-06), untouched until final evaluation; walk-forward on the first 78,600 bars
(10 windows: ~9-month train, ~3-month test). FOMC blackouts active.

| Strategy | Holdout net % | Gross % | Max DD % | Trades | Win % | PF | Avg R | Min-lot rejections | With 7 USD/lot | WF OOS (profitable windows) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GoldPilot V1 | **−8.65** | −7.02 | 9.57 | 151 | 30.5 | 0.80 | −0.11 | 579 | −9.78 | −9.86% (4/10) |
| Order Block Retest | **−2.64** | −1.63 | 6.43 | 75 | 32.0 | 0.87 | −0.07 | 100 | −2.86 | −10.20% (3/10) |
| Liquidity Sweep + MSS | +0.38 | +0.60 | 1.84 | 20 | 35.0 | 1.07 | +0.03 | 227 | +0.33 | −7.27% (4/10) |
| ORB London | **−5.35** | −4.47 | 6.44 | 81 | 32.1 | 0.70 | −0.18 | 120 | −6.46 | −8.61% (4/10) |
| ORB New York | +1.68 | +2.05 | 3.08 | 32 | 50.0 | 1.27 | +0.10 | 163 | +0.87 | +6.29% (5/10) |

**Account-size sensitivity (holdout only, same selected parameters, 0.5% risk unchanged)** —
`research/phase2_xauusd_equity_sensitivity.py`:

| Strategy | 3,000 USD | 10,000 USD | 25,000 USD |
| --- | --- | --- | --- |
| GoldPilot V1 | −8.65% (151 trades) | −9.57% (281) | −9.28% (287) |
| Order Block Retest | −2.64% (75) | −1.86% (158) | −3.59% (158) |
| Liquidity Sweep + MSS | +0.38% (20) | −7.55% (137) | −3.55% (140) |
| ORB London | −5.35% (81) | −12.62% (193) | −12.36% (200) |
| ORB New York | +1.68% (32) | −1.89% (183) | −2.48% (195) |

**Interpretation.** At 3,000 USD the 0.01-lot minimum rejects most signals (only tight-stop setups survive), which
produced the two small positive holdout results. When the strategies can take (almost) all their signals, every one
is negative on the holdout. Walk-forward OOS is negative for four of five; ORB New York is positive (+6.29%) but only
5/10 windows profitable and negative on the holdout once unconstrained. **No XAUUSD strategy shows a robust edge; all
remain experimental.** GoldPilot V1's losses concentrate in downtrending high-volatility regimes.

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

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
(reproduced twice with identical results). Commit `9c3f1aa`, config SHA-256 `5e77bfa0…`, dataset SHA-256
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

### Provider evaluation

| Source | Coverage | XAUUSD relevance | Updates | Cost / licence | Decision |
| --- | --- | --- | --- | --- | --- |
| `pandas_market_calendars` CMEGlobex_Gold | CME gold holidays, early closes, session hours (rule-based, incl. future years) | High (spot gold follows COMEX/Globex closures; broker may differ) | Library releases | Free, MIT | **Implemented** as default holiday source; broker overrides take precedence (union) |
| Federal Reserve FOMC calendar page | FOMC meetings (current + next year, history) | Very high | Fed updates page; dates fixed a year ahead | Free, US government work | **Implemented** (`trading/marketcal/fomc.py`); statement at 14:00 ET |
| BLS release schedule (CPI, NFP) | Official US labour/inflation release dates | Very high | Annual schedule + revisions | Free, public domain | **Not integrated**: automated requests from this environment received HTTP 403; we did not attempt to bypass it. Options: operator-exported file via `load_events_file`, or a licensed vendor |
| BEA release schedule (GDP, PCE) | Official dates | High | Annual | Free | Not yet evaluated in code |
| Trading Economics API | Global calendar, forecast/actual/previous, importance | High | Real time | Paid (third-party listings cite ~US$199/month; verify) | Candidate for forecast/actual values |
| Financial Modeling Prep | Economic calendar with estimate/actual/impact | High | Frequent | Paid tiers (listings conflict: US$29–100+/month; verify) | Candidate |
| Finnhub | Economic calendar | High | Frequent | Premium-only endpoint | Lower priority |

Vendor pricing above comes from third-party listings found on 2026-11-06 (e.g. [Trading Economics API](https://www.tradingeconomics.com/api),
[FMP listing on Datarade](https://datarade.ai/data-products/economic-calendar-api-350-indicators-financial-modeling-prep),
[Finance Calendar comparison](https://www.financecalendar.com/?p=2927)) and must be confirmed on vendor pages before purchase.

**Decision needed (operator):** choose and license a provider for CPI/NFP/GDP schedules with forecast/actual
values, or supply official schedules as structured files. Until then only FOMC events are automated.

### Safeguards implemented (Section 9.3 — instructions were truncated; interpretation)
`EventBlackout` (risk-engine `event_guard`) blocks **new entries** around matching events:
XAUUSD: USD high-impact ±30 min; central-bank (FOMC) −60/+90 min. BTCUSDT: USD high-impact −15/+30 min.
Unconfirmed-time events block the whole UTC day; stale/missing required calendars fail closed; guard errors fail
closed. Rules are configuration; AI output cannot alter closures or blackouts (closures are unioned, events come
only from the provider-attributed store with revision history). Handling of *open* positions around events
(e.g. flattening) is not implemented and awaits the remainder of the Section 9.3 instructions.

## Limitations
- BTC fee is assumed (no testnet keys configured); spread model is a 1 bp assumption, not observed order-book data.
- A single 5.5-month holdout and 12 walk-forward windows are small samples; 24/18 configurations per strategy were tried (multiple-testing risk).
- Bar-level fills: no partial fills, queue position, latency or order-book depth. Spot only (no funding).
- XAUUSD calendar template and server offset must be confirmed per broker; CME holidays are a proxy for broker schedules.
- Regime labels (EMA200 trend × ATR% vs rolling median) are coarse.

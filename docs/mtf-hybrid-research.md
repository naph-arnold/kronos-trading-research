# Multi-timeframe hybrid research — Strategy A (selective), B (scalping), C (hybrid)

Status 2026-11-08. **Research only.** No orders, no change to the running XAUUSD/BTC shadow runners, their
frozen parameters, pre-registrations, state or simulated accounts. Branch `research/mtf-hybrid` (worktree
`../kronos-mtf-research`), based on PR #14.

> The "Claude Code Implementation Brief — Multi-Timeframe Hybrid Trading Framework" referred to in the request
> was **not attached** to the conversation. This work follows the requirements stated in the request itself.
> Anything specific to the brief (exact entry patterns, the 60/40 mechanics, weekly-halt values) is therefore
> implemented as explicitly labelled, configurable research choices.

## 1. Framework (`trading/mtf/`)
- **Frames** (`frames.py`):
  - Higher timeframes are built from the execution candles by UTC-aligned buckets (4H buckets at 00/04/08… UTC).
  - The still-forming bucket is dropped, and buckets with missing candles are flagged `partial`.
  - `align()` maps each execution candle to the latest higher-timeframe candle **closed** at or before its close.
- **Features** (`features.py`), all formally defined and causal:
  - EMA and Wilder ATR.
  - Fractal swings with k bars each side, usable only after their k-bar confirmation lag.
  - Structure trend: +1 for higher highs and higher lows, −1 for lower highs and lower lows.
  - Pullback to EMA ± d·ATR, and an edge-triggered breakout.
- **Strategies** (`strategies.py`):
  - **A, selective (4H→1H→15M→5M):**
    - 4H direction: close vs EMA50 plus slope.
    - 1H structure trend plus close vs EMA20.
    - 15M pullback to EMA20 + d·ATR15 while above EMA50.
    - 5M breakout trigger.
    - Stop below the 15M pullback low.
    - Planned reward-to-risk **after estimated costs ≥ 2.0**, checked at the simulated fill.
    - Time limit 12 h.
  - **B, scalping (1H→15M→5M→1M):** the same chain one level lower. Net reward-to-risk ≥ **1.5**, time limit
    60 min.
  - **Baselines:**
    - Matched random entries: same frequency, long share, stop distance in ATR units, reward-to-risk and time
      limit.
    - A simple 1H EMA50/200 trend rule.
- **Simulator** (`simulator.py`):
  - Every entry passes the unchanged `trading.risk.evaluate`, with:
    - 0.5% intended risk on current equity, round-down sizing, minimum-lot rejection
    - 1% aggregate open risk, spread cap, session calendar and FOMC blackout (XAUUSD)
    - spot long-only (BTC)
  - Sizing uses actual equity, stop distance and the verified instrument specification.
  - **Daily 1.5% halt** is latched on the worst intraday mark from completed candles only.
  - **Research halts** can only *add* restrictions: weekly loss 3%, three consecutive losses, two entries per
    session.
  - **Fills:** next candle open on the executable side plus slippage; stop checked first; gaps fill at the open;
    time exit.
  - MT5 candles are bid prices; Binance candles are treated as mid.
  - Costs are explicit: spread, slippage, fees/commission, and swap (XAUUSD).
- **Hybrid:** A and B candidates in one account under shared limits.
  - **Priority:** A wins ties.
  - **Exposure:** no position opposite an open one; one position per strategy.
  - **Variants:**
    - `hybrid_2pos`: up to 2 positions within the 1% aggregate.
    - `hybrid_1pos`.
    - `scalpcap40`: B ≤ 40% of entries. This is an experimental reading of the "60/40 mix", not an allocation.
- **Advisory** (`trading/agents/mtf_advisory.py`, the only package allowed to name AI providers): recorded Kronos forecasts and inactive Grok agents (official xAI API only;
  live calls raise `GrokNotActivated`). The simulator and risk engine have no parameter that accepts advice.
- **Data tools:**
  - `research/binance_bulk.py`: official Binance bulk archives, each verified against its published SHA-256.
  - MT5 history comes from the existing read-only downloader.

## 2. Data and discipline
| Market | Execution data | Span used | Notes |
| --- | --- | --- | --- |
| BTC/USDT | 5m (bulk, 89 files) | 2019-01-01 → 2026-05-24 20:00 | Phase 2B holdout and later **excluded** |
| BTC/USDT | 1m (bulk, 41 files) | 2023-01-01 → 2026-05-24 20:00 | 1,785,280 candles |
| XAUUSD | M5 (MT5) | development 2025-06-02 → 2025-12-30; exploratory 2025-12-30 → 2026-11-06 | terminal cap 100,000 bars |
| XAUUSD | M1 (MT5) | 2026-07-27 → 2026-11-06 (exploratory only) | entirely inside the period examined in Phase 2A |

**Selection:** rolling walk-forward. Parameters are chosen on each training window (BTC 6 months, XAUUSD 3 or 1
month), then evaluated once on the next window (BTC 2 months, XAUUSD 1 month). There are 12 grid configurations
per strategy, and 20 matched random seeds per run.

**Costs:**

| Market | Scenario | Spread | Slippage | Fees / commission |
| --- | --- | --- | --- | --- |
| BTC | base | 0.01 USDT (live touch) | 2 USDT/side | taker 0.1% per side |
| BTC | stress | 1 bp of price | 10 USDT | 0.1% |
| BTC | low-fee | as base | as base | 0.075% |
| XAUUSD | base | tick-measured New York-hour p50 | 0.10 | commission 0 |
| XAUUSD | stress | tick-measured p90 | 0.30 | 7 USD/lot round trip |

XAUUSD swap is verified from `symbol_info`. Commission and fees remain **assumptions**.

## 3. Results — BTC/USDT (out-of-sample walk-forward)
| Run | Trades | Net % | Net mean R (95% CI) | Gross R | Cost R | Max DD % | Beats matched random | Configurations profitable | Stress net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A selective, 5m (41 windows) | 531 | −32.0 | −0.142 (−0.237, −0.047) | +0.006 | 0.148 | 33.2 | 11/20 | 0/12 | −21.3 |
| A selective, 1m (17 windows) | 130 | −4.1 | −0.061 (−0.244, +0.119) | +0.072 | 0.133 | 5.3 | 9/20 | 3/12 | −6.2 |
| B scalping, 1m (17 windows) | 316 | −20.6 | −0.145 (−0.215, −0.075) | +0.016 | 0.161 | 20.8 | 9/20 | 0/12 | −16.6 |
| C hybrid, 2 positions | 446 | −23.8 | −0.120 | | | 24.3 | | | |
| C hybrid, scalp cap 40% | 225 | −10.7 | −0.099 | | | 11.4 | | | |
| C hybrid, 1 position | 383 | −25.3 | −0.151 | | | 25.4 | | | |
| Trend baseline (1H EMA cross), 2019–2026 | 181 | +10.5 | +0.115 (−0.078, +0.292) | +0.208 | 0.094 | 5.8 | | | |

- **Costs decide the result.** Taker fees of 0.1% per side cost about 0.15 R per trade, while the entries carry
  almost no edge before costs (+0.006 to +0.07 R).
- **The cost filter admits the worst trades when fees fall.** Lowering fees to 0.075% made results *worse*
  (A: 759 trades, −0.172 R). The "≥ 2:1 after costs" filter then admits more tight-stop trades, and those are
  the ones costs dominate.
- **No subgroup rescues it.** No year, session or regime is consistently positive. A was positive only in 2025.
- **Research halts reduce losses but create no edge.** Three consecutive losses cut B's loss from −20.6% to
  −17.0%; the weekly and session caps changed little.
- **Minimum order size is not a constraint** (Binance 5 USDT notional), even at 3,000 USDT.

## 4. Results — XAUUSD (data-limited)
| Run | Trades | Net % | Net mean R (95% CI) | Gross R | Cost R | Max DD % | Beats matched random | Configurations profitable | Stress net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A selective, 5m — **development** 2025-06 → 2025-12 (4 windows) | 82 | +17.7 | +0.447 (+0.119, +0.796) | +0.480 | 0.033 | 2.6 | 20/20 | 12/12 | +14.0 |
| A, **frozen** development parameters on 2025-12-30 → 2026-11-06 (exploratory) | 277 | +13.0 | +0.109 (−0.070, +0.290) | +0.131 | 0.023 | | 20/20 (random median +0.020) | — | +8.0 (+0.061 R) |
| A, walk-forward within 2026 (exploratory) | 202 | +14.3 | +0.144 (−0.082, +0.396) | +0.171 | 0.027 | 9.0 | 17/20 | 12/12 | +11.1 |
| B scalping, 1m (exploratory, Jul–Nov 2026) | 535 | −7.9 | −0.029 (−0.125, +0.070) | +0.027 | 0.055 | 18.8 | 11/20 | 0/12 | −21.8 |
| A on 1m (exploratory) | 51 | +0.7 | −0.007 | | | 8.8 | 1/20 | 11/12 | −1.0 |
| C hybrid, 2 positions | 537 | −0.3 | +0.002 | | | 21.4 | | | |
| C hybrid, scalp cap 40% | 87 | +5.9 | +0.131 | | | 6.3 | | | |
| C hybrid, 1 position | 482 | −7.2 | −0.033 | | | 18.7 | | | |

- **A is the only configuration with supporting evidence.** In development it was strongly positive across every
  parameter setting.
- **With frozen parameters on the later period:**
  - It stayed positive (+0.11 R per trade, +13% at 0.5% risk) and beat all 20 matched random runs.
  - It was positive on longs and shorts and in all four regimes.
  - It survived stress costs (+8%).
  - But the edge **decayed** from +0.45 to +0.11 R, and the **confidence interval includes zero**.
  - That period was examined in Phase 2A for other strategies, so it is **not fresh evidence**.
- **Causality holds on real data:** candidates computed on truncated real XAUUSD data are identical to the
  full-data candidates (checked at three cut points).
- **B scalping has no edge after costs.** Gross +0.03 R against costs of 0.055 R; 0/12 settings profitable.
- **The hybrid dilutes A with B** and is worse than A alone, except when the scalp cap effectively removes B.
- **Minimum lot at 3,000 USD:** A lost about half its signals (81 minimum-lot rejections in development, 304 in
  2026). 10,000 USD avoids this, at the same 0.5% risk.

## 5. Verdicts
| Configuration | BTC/USDT | XAUUSD |
| --- | --- | --- |
| A selective | **No credible advantage** (net negative, CI excludes 0, no better than random) | **Promising but unproven**: positive in development and with frozen parameters; CI includes 0; small, data-limited, partly examined period |
| B scalping | **No advantage** (costs > gross edge) | **No advantage** (costs > gross edge) |
| C hybrid | Not justified (B has no edge; worse than A alone) | Not justified (B dilutes A) |

**Recommendation:** do not deploy any of these. XAUUSD Strategy A deserves a **pre-registered shadow forward
test**, just as NY ORB got in Phase 2D:
1. Freeze parameters RR 2.5, pull_atr 0.25, confirm_bars 6, and define criteria before fresh data.
2. Run it independently of the existing frozen experiments (separate state; no change to the running runners).

Before that, if possible, raise the MT5 terminal's "Max bars in chart" (a terminal setting the operator controls)
so M5 history before 2025-06 can be checked as additional, previously unexamined data.

## 6. Milestone status
| Step | Status |
| --- | --- |
| 1 Multi-timeframe analysis engine | **Implemented, verified** (prefix-invariance tests on synthetic and real data) |
| 2–3 Selective strategy and validation | **Implemented**; BTC rejected; XAUUSD experimental (positive, unproven) |
| 4–5 Scalping and cost analysis | **Implemented**; rejected on both markets (costs exceed gross edge) |
| 6 Hybrid and conflict management | **Implemented**; not justified by evidence |
| 7 Combined comparison | **Completed** (same 1m windows: A-only, B-only, three hybrid variants) |
| 8 Tests, reports, documentation | **Completed** (`tests/test_mtf_framework.py`, this document; JSON/MD outputs in `research/output/mtf/`, git-ignored) |
| Blocked | The implementation brief was not attached. XAUUSD M1/M5 depth is capped by the terminal setting. No order-book or liquidity data for scalping fills (best-quote and bar-level simulation only) |

## 7. Limitations
- Fills are simulated on candles: no queue position, partial fills or order-book depth. That favours scalping, so
  its negative result is likely optimistic.
- The XAUUSD spread uses the tick-measured hour-of-day profile (one week of ticks), not the spread at each
  historical moment.
- BTC research applies no event blackout (consistent with Phase 2E); XAUUSD applies FOMC blackouts.
- Drawdown is measured on the realized-equity curve. The daily halt uses intraday marks; reported drawdown does
  not.
- The walk-forward windows reuse 2019–2026 BTC data that Phase 2E also used to research other (1H) strategies;
  the Phase 2B holdout stays excluded.

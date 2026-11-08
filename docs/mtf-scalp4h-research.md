# 4H-led scalping (scalp4h-v1): research report

Research only. No orders of any kind (live, demo or testnet). The running XAUUSD and BTC/USDT shadow runners,
their frozen strategies, pre-registrations, state and accounts were not touched.

## Why
In the original scalper (Strategy B, `trading/mtf/strategies.py::scalp_candidates`), the 1H timeframe set
direction. The requested hierarchy makes completed 4H candles the primary directional bias for both strategy
families, with 1H used only to confirm structure and momentum.

- **Selective (Strategy A):** already used 4H direction → 1H structure → 15M setup → 5M confirmation, so it is
  unchanged.
- **Scalping:** a new, separately versioned strategy, `scalp4h-v1` (tag `B4H`, `trading/mtf/scalp4h.py`). The
  H1-led scalper is unmodified and serves as the baseline.

## Definition (all inputs are completed candles)
| Level | Rule |
| --- | --- |
| 4H bias (mandatory) | C4 > EMA50 and EMA50 rising over 3 candles (mirror for shorts); same as Strategy A |
| 1H structure + momentum | fractal structure trend (k=2) agrees, and C1 is on the same side of EMA20 |
| 15M context | C15 on the trend side of EMA20 |
| 5M setup | pullback to EMA20 ± pull_atr × ATR5 within 3 candles |
| 1M entry | breakout of the previous trigger_bars highs/lows, above/below EMA20 (edge-triggered) |
| Stop / target | 5M pullback extreme ± 0.1 ATR5; target rr × risk; net RR after costs ≥ 1.5; 60-minute time limit |

The 15M/5M/1M rules, stops, targets, time limit, parameter grid, walk-forward windows, cost scenarios and risk
engine are identical to the baseline, so the comparison isolates the directional hierarchy.

Three variants per market:
- **B:** the existing H1-led baseline, rerun.
- **B4H_only:** 4H bias only, 1H skipped. This ablation isolates the 4H filter.
- **B4H:** 4H bias plus 1H alignment, the proposed hierarchy.

Per-market configurations are `research/configs/mtf_scalp4h_v1_{btcusdt,xauusd}.json`.

Risk is unchanged: 0.5% maximum intended risk per trade, a 1.5% daily halt and 1% aggregate open risk, all
enforced by the unchanged `trading.risk.evaluate`.

## Integrity
- **Reproduction:** the baseline rerun reproduced the original results exactly. BTC: 316 trades, −20.57%,
  −0.1447 R. XAU: 535 trades, −7.891%, −0.029 R.
- **Original outputs preserved:** copied to `research/output/mtf/original_2026-11-08/` with SHA-256 sums, and the
  checksums were verified after the runs. New outputs are in `research/output/mtf/scalp4h_v1/`.
- **Causality:** prefix-invariance tests and completed-4H checks are in `tests/test_mtf_scalp4h.py` (9 tests).

## Results (out-of-sample walk-forward, net of costs, 10,000 account)

### BTC/USDT (spot, long/flat; 1m, 2023-01 → 2026-05-24; 17 test windows; Phase 2B holdout excluded)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Win % | Max DD % | Profitable windows | vs random | Grid profitable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B (H1-led) | 316 | −20.6 | −0.145 (−0.215, −0.075) | 0.016 | 0.161 | 35.4 | 20.8 | 1/17 | 9/20 | 0/12 |
| B4H_only | 358 | −26.1 | −0.168 (−0.239, −0.097) | 0.017 | 0.185 | 34.1 | 25.7 | 1/17 | 7/20 | 0/12 |
| B4H (4H+1H) | 290 | −24.6 | −0.194 (−0.275, −0.107) | 0.029 | 0.223 | 31.7 | 24.6 | 6/17 | 19/20 | 0/12 |

- B4H minus B: −0.049 R (bootstrap CI −0.156, +0.061).
- **Gross edge:** the 4H+1H filter slightly raises gross R (0.016 → 0.029). Selection then favours tighter
  stops, so costs rise from 0.16 R to 0.22 R and net R gets worse.
- **Random comparison:** B4H beats 19/20 matched random runs, but those runs average −0.236 R. The filter is
  slightly better than random entries with the same tight stops, which are themselves cost-dominated; it is not
  profitable.
- **Years and regimes:** every year from 2023 to 2026 is negative, and so is every regime. Stress and low-fee
  scenarios are negative too.
- **Research halts:** they reduce losses but none turns the result positive.
- **Eligibility:** no minimum-size rejections at a 3,000 account.

### XAUUSD (MT5 1m, 2026-07-27 → 2026-11-06; 2 test windows), exploratory only
The terminal caps history at 100,000 bars, so the M1 data lies entirely in the period already examined in Phase 2A.

| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Win % | Max DD % | Profitable windows | vs random | Grid profitable | Stress net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B (H1-led) | 535 | −7.9 | −0.029 (−0.125, +0.070) | 0.027 | 0.055 | 40.2 | 18.8 | 0/2 | 11/20 | 0/12 | −21.8 |
| B4H_only | 468 | −2.1 | −0.005 (−0.115, +0.114) | 0.050 | 0.055 | 40.0 | 14.7 | 1/2 | 8/20 | 0/12 | −11.3 |
| B4H (4H+1H) | 163 | +3.2 | +0.048 (−0.166, +0.236) | 0.102 | 0.053 | 37.4 | 9.0 | 2/2 | 14/20 | 3/12 | −4.7 |

- B4H minus B: +0.077 R (CI −0.145, +0.302).
- **Direction:** the direction of change is favourable. Gross R rises from 0.027 to 0.102, drawdown roughly
  halves, and trades drop by 70%.
- **Why it is not evidence:**
  - Every confidence interval includes zero.
  - There are only 2 test windows and about 3 months of data.
  - Only 3 of 12 grid settings are profitable.
  - The result is negative under stress costs.
  - It beats only 14 of 20 random runs.
  - The gains come entirely from down-trending regimes (139 trades, about +0.15 R); up regimes lose about −0.57 R
    over 24 trades. That indicates dependence on a single directional phase, not a robust hierarchy effect.

## Verdict
| Question | BTC/USDT | XAUUSD (exploratory) |
| --- | --- | --- |
| Net expectancy improved? | No (worse: −0.145 → −0.194 R) | Point estimate improved (−0.029 → +0.048 R); not significant |
| Drawdown improved? | No (20.8% → 24.6%) | Yes (18.8% → 9.0%), on 2 windows |
| Trade quality (gross R) improved? | Slightly (0.016 → 0.029), consumed by higher cost R | Yes (0.027 → 0.102) |
| Consistency improved? | More profitable windows (1 → 6 of 17), but every year negative | 2/2 windows; regime-dependent |
| Does requiring 1H alignment on top of 4H help? | No (−0.026 R vs 4H-only) | Point estimate +0.054 R; CI includes 0 |

- **BTC/USDT:** the 4H-led hierarchy does not improve scalping after costs, and scalping remains rejected.
- **XAUUSD:** a weak, unproven improvement on a small, previously examined sample. It justifies no use. At most it
  justifies retesting on unexamined data if MT5 "Max bars in chart" is raised, or a pre-registered shadow test
  with approval.

## Reproduce
    python -m research.mtf_scalp4h_research --market xauusd --workers 6
    python -m research.mtf_scalp4h_research --market btcusdt --workers 6

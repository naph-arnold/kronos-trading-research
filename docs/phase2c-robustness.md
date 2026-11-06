# Phase 2C — XAUUSD strategy validation and robustness

Status 2026-11-06. Research only; no orders; strategy rules and the 0.5% risk fraction unchanged; Phase 2A-selected
parameters used throughout. Reproduce: `python -m research.phase2c_robustness ticks` (read-only MT5) then
`python -m research.phase2c_robustness run` (config `research/configs/phase2c_xauusd.json`).

## Data discipline
| Partition | Range (UTC) | Use |
| --- | --- | --- |
| Training | 2022-09-01 → 2025-12-30 (78,600 M15 bars) | All Phase 2C diagnostics, parameter sensitivity, baselines, cost stress |
| Previously seen holdout | 2025-12-30 → 2026-11-06 (19,650 bars) | Evaluated once in Phase 2A; **not used for any new choice** (cited descriptively only) |
| Genuinely unseen | Forward data from 2026-11-09 | Pre-registered forward test (`research/forward_test/phase2c_xauusd_preregistration.json`) |

Older M15 history is **not available**: the terminal's `maxbars` (100,000) caps M15 at 2022-08; requests for
2018–2022 return no bars. No unseen *historical* M15 period exists on this terminal.

## Costs: verified vs estimated
| Component | Status | Value / source |
| --- | --- | --- |
| Tick value | **Verified** | 1.00 USD per 0.01 per lot (terminal P&L engine; broker field 0.1 is wrong) |
| Spread | **Verified (sampled)** | 2,100,084 ticks over 7 days (2026-10-30 → 11-06): p10 0.13, **p50 0.17**, mean 0.24, p90 0.45, p99 0.59, max 60.58 USD. Widest around the broker's daily close (NY 16:00 p50 0.38) and reopen (NY 18:00 p90 0.73). The M15 bar `spread` field is the bar *minimum* (median 0.07 historically). Model: base = max(bar field, 0.17); stress = max(bar field, 0.24) |
| Swap (overnight) | **Verified** (`symbol_info`) | long −12.6, short −4.6 points per lot per night (1 point = 1.00 USD/lot), triple on Wednesday; applied post hoc |
| Commission | **Unverified (estimated)** | Not exposed by MT5; no account trade history. Scenarios 0 and 7 USD/lot round trip. Verification requires broker documentation or one explicitly authorized demo trade |
| Slippage | **Estimated** | 0.10 USD/side base, 0.30 stress (no fills exist to measure it) |

MetaQuotes-Demo is MetaQuotes' test server; its costs may not represent the intended production broker.

## Strategy-by-strategy findings (training, 25,000 USD so the minimum lot does not filter signals; base costs)
| Strategy | Net % | Gross % | After swap % | Max DD % | Trades/month | Mean R (95% CI) | Long share | Beats long-matched random seeds | Configs profitable | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GoldPilot V1 | −15.1 | +18.6 | −17.0 | 32.0 | 26.8 | −0.028 (−0.109, +0.051) | 67% | 12/20 | 3/18 | Reject as is |
| Order Block Retest | −6.2 | +17.4 | −7.2 | 18.0 | 15.1 | −0.017 (−0.127, +0.084) | 66% | 13/20 | 0/9 | Reject |
| Liquidity Sweep + MSS | −7.9 | +5.5 | −9.5 | 14.8 | 14.7 | −0.023 (−0.129, +0.088) | 53% | 14/20 | 1/9 | Reject |
| ORB London | −21.1 | +1.0 | −21.1 | 22.7 | 19.4 | −0.061 (−0.127, +0.005) | 54% | 18/20 | 0/18 | Reject |
| **ORB New York** | −5.5 | +9.5 | −5.5 | 13.6 | 18.1 | −0.013 (−0.078, +0.060) | 51% | **20/20** | 1/18 | **Forward-test only** |

Baselines (same training period, costs, risk engine): random entries with identical exits — 1.5 ATR / 2R: median
−0.112 R/trade (unbiased), −0.068 (67% long); 1.0 ATR / 1.5R / 16-bar time exit: −0.148 (unbiased), −0.140 (51% long).
H1 EMA 50/200 crossover: +0.9% (94 trades, mean R +0.02, CI −0.23…+0.30). Buy-and-hold (unsized, fully invested):
+155% — 2022–2025 was a strong gold bull market.

Cost stress (spread floor 0.24, slippage 0.30, commission 7 USD/lot): every strategy falls to −19% … −47%
(GoldPilot −47.0, Order Block −28.0, Sweep −19.3, ORB London −40.7, ORB New York −20.9).

### 1. Why GoldPilot V1, Order Block Retest and ORB London performed poorly
- **Costs exceed the gross edge.** GoldPilot and Order Block are clearly positive before costs (+18.6% / +17.4%)
  but costs average 0.08 R per trade at median stops of ~4.6–5.0 USD; spread + slippage (~0.37 USD round trip) is a
  large fraction of such tight stops. Stops < 5 USD lose in every strategy (GoldPilot −0.091 R, Order Block −0.084 R).
- **Edge is mostly trend exposure.** Longs make money and shorts lose in every strategy (GoldPilot longs +0.053 R,
  shorts −0.195 R); against random entries with the *same long bias* GoldPilot and Order Block beat only 12–13 of 20
  seeds. Losses concentrate in down-trend regimes (GoldPilot down-highvol −0.181 R, down-lowvol −0.214 R).
- **Fragile parameters.** GoldPilot: 3/18 configurations profitable, median −27.7%; the best (+33.7%: ADX 15,
  2.5 ATR stop, 3R) earned ~83% of its profit in 2025's rally. Order Block: 0/9 profitable.
- **ORB London has no edge in any slice:** negative in each training year (2022–2025), 0/18 configurations profitable,
  gross ≈ 0, significantly negative at 3,000 USD (t = −2.21, CI −0.153…−0.009).

### 2. ORB New York and Liquidity Sweep: positives and inconsistencies
- Their Phase 2A holdout gains at 3,000 USD came from the **minimum-lot filter** (only 16% / 8% of 2026 signals were
  tradable), not from the strategies: on training data the tight-stop trades that survive that filter are the
  *worst* (ORB NY stops < 5 USD −0.131 R; Sweep −0.064 R). At 10,000/25,000 USD both are negative on the holdout.
- **ORB New York** is the only strategy whose entries carry information beyond random: it beats all 20 random-entry
  seeds with the same exit structure and long share (per trade −0.013 R vs median −0.140 R), with +0.03 R gross per
  trade. Costs (~0.044 R/trade) erase it. Year-to-year results alternate (2022 +, 2023 −, 2024 −, 2025 +); only 1/18
  nearby configurations is profitable; Phase 2A walk-forward +6.3% (5/10 windows). Evidence: weak but non-zero.
- **Liquidity Sweep** positive results are side- and session-specific (longs +0.057 R, Asia +0.043 R) while shorts
  (−0.115 R), London (−0.112 R) and high volatility (−0.091 R) lose — a pattern consistent with trend beta, not a
  stable edge; 14/20 random seeds beaten; 1/9 configurations profitable.

### 3. Market conditions (mean R per trade, training)
| Strategy | up-lowvol | up-highvol | down-lowvol | down-highvol | Asia | London | NY AM | NY PM |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GoldPilot V1 | +0.088 | +0.028 | −0.214 | −0.181 | −0.059 | −0.077 | −0.019 | +0.137 |
| Order Block | +0.100 | −0.027 | +0.044 | −0.191 | −0.009 | +0.017 | −0.030 | −0.121 |
| Liquidity Sweep | −0.020 | −0.106 | +0.036 | +0.023 | +0.043 | −0.112 | −0.024 | −0.198 |
| ORB London | −0.079 | −0.129 | −0.092 | +0.088 | — | −0.061 | — | — |
| ORB New York | −0.264 | +0.005 | −0.291 | +0.057 | — | — | −0.010 | (3 trades) |
Regime = H1-equivalent EMA200 trend × ATR% vs rolling median, known at signal time. Several cells have < 100 trades;
none is a validated filter (adding one would be a rule change requiring unseen-data validation).

### 4. Minimum lot vs the 0.5% limit; account size; contract size
- With a 0.01-lot minimum on a 100 oz contract, a trade risks ≥ 1.00 USD per 1.00 USD of stop distance. Max tradable
  stop (base costs, 0 commission): **1,000 USD → 4.67; 3,000 → 14.67; 5,000 → 24.67; 10,000 → 49.67; 25,000 → 124.67 USD**.
- Gold's M15 ATR in USD rose 4.5× (median 2.06 in 2022 at ~1,711 USD/oz → 9.29 in 2026 at ~4,459). Hence training-era
  eligibility at 3,000 USD was 80–97% of signals, but in 2026 only **8–43%** (Sweep 8%, ORB NY 16%, GoldPilot 21%,
  ORB London 40%, Order Block 43%); at 10,000 USD it is **81–98%**.
- **Smaller contracts:** none on MetaQuotes-Demo — XAUUSD, XAUEUR, XAUAUD are all 100 oz with 0.01-lot minimum;
  XAUCHF/XAUGBP are disabled; `GOLD` is Barrick Gold stock and `XAUG` an ETF, not substitutes. Brokers offering
  1–10 oz gold contracts would need separate verification.
- **Conclusion:** a 10,000 USD demo balance (risk fraction unchanged) makes ≥ ~90% of signals eligible; at 3,000 USD
  the engine correctly rejects most signals, and the survivors are a biased (tight-stop, worse) subset.

## Recommendations
1. **Forward-test (shadow mode, no orders): ORB New York only**, with the frozen Phase 2A parameters
   (params hash `c00dae9ee89b0a65`), plus the H1 EMA-cross and random-entry controls. Pre-registered success
   criteria and checkpoints are in `research/forward_test/phase2c_xauusd_preregistration.json`. Expected outcome on
   current evidence: break-even to slightly negative after costs; it is the only candidate whose entry signal
   beats random.
2. **Do not forward-test** GoldPilot V1, Order Block Retest, Liquidity Sweep + MSS or ORB London as they are.
3. **Research hypotheses (not validated; any test must be pre-registered and run on unseen forward data):**
   cost drag is the main failure mode at tight stops (wider stops / fewer trades); long-only trend participation
   explains much of GoldPilot/Order Block gross profit (compare against a long-only trend baseline, not random).
4. **Operational:** use a 10,000 USD demo balance for forward tests; confirm commission with the broker; build the
   read-only shadow runner (signals from live closed bars, simulated fills from live ticks, no order calls).

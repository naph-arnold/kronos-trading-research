# XAUUSD scalp v2: three new hypotheses (development results, 2026-11-09)

**Pre-registration:** `research/forward_test/xau_scalp_v2_preregistration.json`, commit `c766013`, sha256
`bd166072…`, committed **before any evaluation**.

**Code:**
- `trading/mtf/scalp_v2.py` (generators);
- `research/xau_scalp_v2.py` (harness);
- `tests/test_scalp_v2.py`.

**Execution model:** the existing cost-aware simulator, with the unchanged `trading.risk.evaluate`:
- 0.5% risk, 1.5% daily halt, 1% aggregate, one position;
- minimum lot and FOMC blackouts;
- verified swap; hourly spread profile; next-1m-open fills.

The previously rejected scalping configurations (B, B4H_only, B4H) were **not** reused or re-tuned.

## 1. Hypotheses (summary; full rules in the pre-registration)
| | Economic rationale | Rules (timeframes) | Stop | Target |
| --- | --- | --- | --- | --- |
| **H1 compression → expansion** | Volatility clusters; a break out of an abnormally quiet range tends to expand | 5m ATR14/ATR100 ≤ 0.75 and 1-hour box ≤ 2 × ATR100 → 1m close beyond the box ± 0.1 ATR; 1h EMA50 slope not opposite | m × ATR14(5m) | 2 R |
| **H2 trend-pullback continuation** | Persistent intraday flow pauses in bounded retracements | 4h + 1h trend (EMA50) → 15m impulse ≥ 2.5 × ATR15 → 5m pullback 38–62% → 5m close above the prior 3-bar high | m × ATR14(5m) | 2 R |
| **H3 liquidity sweep → displacement** | Stops cluster beyond the Asian range; a failed sweep plus an opposite displacement signals absorption | Asian range (00–07 UTC) → 5m sweep closing back inside (07–16 UTC) → bearish/bullish 5m body ≥ 1 ATR within 3 bars | farther of (sweep extreme + 0.1 ATR) and m × ATR | 2 R |

**Common settings:**
- Entries 07–20 UTC; 240-minute time limit.
- Net reward-to-risk of at least 1.5 at the fill.
- **Stop width m ∈ {1.5, 2.5, 4.0} × ATR is the only research variable.**

**Costs** (calculated from the broker's verified specification):

| Scenario | Spread | Slippage per side | Commission per lot (round trip) |
| --- | --- | --- | --- |
| Base | p50 spread profile | 0.10 | none |
| Stress | p90 spread profile | 0.30 | 7 USD |

## 2. Development protocol (pre-declared)
Both development datasets were examined by earlier research. They are used only to:
- **select** one stop multiple per hypothesis: the highest mean net R on 2025–26 at base costs, among variants with
  at least 100 trades;
- apply a **gate**: the selected variant needs net R > 0 on 2025–26 at base costs, ≥ 0 at stress costs, and > 0 on
  2019–20.

Nothing else may be chosen or changed.

## 3. Results (10,000 USD, 0.5% risk; mean net R per trade [bootstrap 95% CI])
| Hypothesis | m | 2025–26 trades | 2025–26 base | 2025–26 stress | Gross R / cost R | 2019–20 base | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| H1 | 1.5 | 138 | −0.049 [−0.32, 0.20] | −0.063 | 0.036 / 0.085 | — | — |
| H1 | **2.5** (selected) | 128 | −0.003 [−0.24, 0.24] | −0.168 | 0.055 / 0.058 | **−0.207** (90 trades) | **rejected** |
| H1 | 4.0 | 126 | −0.029 | −0.094 | 0.013 / 0.042 | — | — |
| H2 | 1.5 | 333 | −0.062 | −0.160 | −0.017 / 0.045 | — | — |
| H2 | 2.5 | 282 | +0.006 | −0.052 | 0.037 / 0.030 | — | — |
| H2 | **4.0** (selected) | 258 | **+0.120 [+0.005, +0.244]**, PF 1.36, DD 3.9% | +0.076 | 0.140 / 0.020 | **−0.115 [−0.21, −0.02]** (412 trades; gross −0.013) | **rejected** |
| H3 | **1.5** (selected) | 134 | +0.018 [−0.21, 0.22] | −0.103 | 0.050 / 0.032 | **−0.158** (212 trades) | **rejected** |
| H3 | 2.5 | 134 | −0.034 | −0.074 | −0.007 / 0.027 | — | — |
| H3 | 4.0 | 133 | −0.040 | −0.077 | −0.020 / 0.019 | — | — |

Matched random controls on 2025–26 (5 seeds, base costs) averaged:
- H1: −0.05;
- H2: −0.02;
- H3: −0.06.

**Decision (pre-registered): all three hypotheses are rejected at the development gate. None enters independent
validation.**

## 4. What the evidence says
- **Wider stops help.** In every hypothesis the cost share falls as the stop widens. For H2, cost was 0.045 R at
  1.5 ATR and 0.020 R at 4 ATR. For H2 only, the gross result also rose with the stop.
  - Very tight stops are not optimal for XAUUSD at today's volatility.
  - That is a property of the cost structure, not proof of an edge.
- **H2 (trend-pullback, 4 ATR) is the only promising signal, and it is regime-dependent.**
  - On 2025–26 gold, a strong trending market, the gross edge was +0.14 R per trade. Both years were positive and the
    result beat the random controls.
  - On 2019–20 the gross edge was about zero (−0.013 R) and the net result negative.
  - It therefore does not pass, and nothing was changed after seeing these results.
- **A follow-up "H2 only in trending regimes" would be a new hypothesis formed after seeing data.** It may be
  pre-registered, but it can only be tested on data not used here: the unexamined 2004–2018 cache or a forward shadow
  test.
- **The minimum lot binds at 3,000 USD.** A few signals (about 3% or fewer) are rejected because 0.01 lot would
  exceed the 15 USD risk budget at wide stops.

## 5. Independent validation sources (for any future hypothesis)
| Source | Status | Cost |
| --- | --- | --- |
| MT5 local M1 cache 2004–2018 | Unexamined; readable only after raising "Max bars in chart", which restarts the terminal and interrupts the gold runner and Strategy A. **Needs your approval.** No tick spreads exist for that period, so a conservative spread profile (2019–20 p90) would be used. | Short outage of both MT5 runners |
| Prospective shadow forward test | Cleanest independent data; needs a launch approval and months of data | Time |
| Another broker's history | Not available | — |

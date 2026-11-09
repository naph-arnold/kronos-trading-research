# XAUUSD scalping research: status and evidence (2026-11-09)

**Status: no scalping configuration has shown a positive net expectancy. None is in shadow testing or eligible for
demo execution.** Profitability is the research objective, not an assumed result.

## 1. Evidence so far
| Test | Data | Result |
| --- | --- | --- |
| Scalping B (MTF framework) | XAUUSD M1/M5 2025–2026 (examined) | Rejected: costs exceed the gross edge ([mtf-hybrid-research.md](mtf-hybrid-research.md)) |
| 4H-led scalp4h-v1 | M1 2026-07 → 11, 2 windows (examined) | +0.048 R, not significant ([mtf-scalp4h-research.md](mtf-scalp4h-research.md)) |
| Frozen B, B4H_only, B4H on **unexamined 2019–2020** (single pre-registered run) | 699,515 M1 bars from 55.5 M broker ticks | **All rejected**: mean net R −0.135 to −0.219, CI below 0; gross R ≈ −0.02 to −0.03 before costs; every year, quarter, regime, session and side negative; 0/36 diagnostic settings profitable ([mtf-scalp4h-oos-2019-2020.md](mtf-scalp4h-oos-2019-2020.md)) |

Those configurations are not reactivated: 2019–2020 is now examined data and cannot validate them again.

## 2. Cost floor (verified data; `research/xau_scalping_cost_floor.py`)
Descriptive only: no strategy, no parameter search.

**Formulas:**
- Round-trip cost per ounce = spread + 2 × slippage (0.10, the runners' allowance) + commission ÷ 100 oz.
- Cost in R = cost ÷ stop distance.
- Break-even win rate at reward-to-risk k = (1 + c) ÷ (1 + k).

**Data and caveats:**
- The spread is the broker's *minimum* spread per minute, so the true cost is higher and these figures are
  **optimistic**.
- Commission is a scenario (0 or 7 USD/lot), because MT5 does not expose it.

| Period (bid M1 from broker ticks) | Min-spread median / p90 | ATR(M1) median | Cost R at 1 × ATR stop | at 3 × ATR | at 10 × ATR |
| --- | --- | --- | --- | --- | --- |
| 2019–2020 (dataset `mt5_xauusd_1m_ticks_2019_2020.csv`) | 0.21 / 0.28 | 0.39 | 1.05–1.23 R | 0.35–0.41 R | 0.11–0.12 R |
| 2025-05 → 2026-11 (`mt5_xauusd_1m_ticks_2025_2026.csv`) | 0.13 / 0.19 | 1.75 | 0.19–0.23 R | 0.06–0.08 R | 0.02 R |

**Break-even win rates** (2025–2026, commission 7):

| Stop | RR 1 | RR 1.5 | RR 2 |
| --- | --- | --- | --- |
| 1 × ATR | 61.4% | 49.1% | 40.9% |
| 3 × ATR | 53.8% | 43.0% | 35.9% |

**What this means:**
- Gold's current volatility (price around 4,000, M1 ATR around 1.75) makes costs **5–6 times smaller in R** than in
  2019–2020. The cost hurdle is no longer prohibitive for stops of 3 × M1 ATR (about 5 USD) or more.
- But cost is only half the problem. In the unexamined 2019–2020 test the signals had **no edge even before costs**
  (gross R about −0.02). Lower costs make a real edge easier to keep; they do not create one.
- A scalper with stops under about 1 × M1 ATR needs win rates of about 60% or more at RR 1, a standard no tested
  configuration came close to.

## 3. What a new, testable scalping hypothesis must satisfy (before any data is examined)
1. **A stated mechanism**, not a re-parameterisation of the rejected B or B4H families: for example, session-open
   liquidity imbalance or event-window volatility expansion. Gross edge must be the target, with costs from §2.
2. **Pre-registration** of entry, exit, stops of at least 3 × ATR(M1) (so cost is under about 0.1 R), sessions,
   sizing, random controls and decision criteria, committed before the evaluation data is read.
3. **Data separation:**

   | Data | Status | Use |
   | --- | --- | --- |
   | 2025-05 → 2026-11 | M1 from ticks; partly examined at M5 and above | development only |
   | 2019–2020 | examined | development only |
   | Terminal's local M1 cache, 2004–2018 | genuinely **unexamined** | the only historical candidate for an independent test |

   The 2004–2018 cache is readable only after raising MT5 "Max bars in chart", which needs a terminal restart and
   would interrupt the gold runner and Strategy A: **operator approval required**. A pre-registered forward shadow
   test is the other independent source.
4. **A single evaluation run**, with matched random controls and stress costs. A Bonferroni-adjusted CI is required
   if more than one variant is evaluated.
5. **Execution realism:** minimum lot 0.01 (at 3,000 USD and a 0.5% risk budget, a 5 USD stop allows at most 0.03
   lots), stops level, fill at the next tick, and a latency allowance.

**Next step (needs your decision):** choose one mechanism to pre-register, and approve either the MT5 restart (for
2004–2018 data) or a forward shadow test as the independent evidence.

# XAUUSD 4H-led scalping: frozen evaluation on unexamined 2019–2020 history

Research only. No orders of any kind. MT5 settings were not changed and the terminal was not restarted. Neither
shadow runner was touched; both kept 100% heartbeat uptime with consistent pre-registrations throughout. PR #18's
parameters and results are unchanged.

## 1. How much additional history exists (read-only probes)
| Source | Availability |
| --- | --- |
| Bar API (`copy_rates_*`) | Capped by the terminal's "Max bars in chart" = 100,000. M1 starts 2026-07-27 and M5 2025-06-02; requests before the cap return nothing useful. |
| Terminal's local M1 cache (`bases/MetaQuotes-Demo/history/XAUUSD/*.hcc`) | 2004–2026, about 21 MB/year. Present, but readable through the API only if max bars is raised. The proprietary file format was not parsed. |
| Broker tick history (`copy_ticks_range`, not capped by max bars) | 2019-01-02 → 2020-12-31, then none until 2025-05. No ticks for 2016–2018 or 2021-01 → 2025-04. |

**Max bars setting.** MQL5 forum reports say a change to "Max bars in chart" takes effect only after closing and
reopening the terminal [1][2], and that CopyRates stays bound by that limit [3]. Raising it would therefore
restart MT5 and interrupt the gold shadow runner's connection. It was not done; that needs your approval.

**Unexamined period.** Every XAUUSD dataset examined before this work starts on or after 2022-09-01 (M15/H1 from
2022-09, M5 from 2025-06, M1 from 2026-07). The tick-covered 2019–2020 period is therefore genuinely unexamined.

## 2. Pre-registration (before any data download)
`research/forward_test/xau_scalp4h_oos_2019_2020_preregistration.json`, commit `b4d8a80`, sha256
`0e8a9390…1373`.

- **Frozen variants:** the most-selected PR #18 parameters per variant, never re-selected.
  - B (H1-led): RR 2.0, pull_atr 0.5, trigger_bars 5.
  - B4H_only: RR 1.75, pull_atr 0.5, trigger_bars 5.
  - B4H (4H + 1H): RR 2.5, pull_atr 0.5, trigger_bars 5.
- **Window, costs and decision rules:** fixed in advance, with a single run.

## 3. Data (existing read-only connector) and validation
- **Built:** 699,515 M1 bars from 55.5 M bid/ask ticks. Bars are bid OHLC, tick count, and minimum spread per
  minute (`research/xau_tick_history.py`, dataset sha256 `76d45bc3…`).

| Check | Result |
| --- | --- |
| Construction vs broker M1 (overlap 2026-08-03 → 08-29) | 26,280/26,280 minutes identical OHLC; spread identical 99.92% |
| vs broker H1 bars, 2019–2020 | 99.94% of 11,752 hours identical; 23 broker hours have no ticks; max deviation in 7 hours |
| Timestamps | UTC, minute-aligned, strictly increasing, unique |
| Server-time conversion (EET/EU DST) | No bars in the 17:00 New York daily break, on Saturdays, or before the Sunday open |
| Missing minutes vs CME Globex gold calendar | 1.44% of open minutes; 0 bars in closed time |
| Prices | 0 non-positive or inconsistent OHLC; 8 one-minute moves > 1% (max 1.9%, March 2020) |
| Spreads | 2019–20 median 0.27 vs 0.13 in the 2026 profile used as base cost; p90 similar (0.42–0.77) |

**What the missing minutes are:**
- Mostly the broker's shifted daily break in the weeks when US and EU daylight-saving time differ (65 minutes per
  day): broker session behaviour, not lost data.
- 2019-10-01: a full day (1,380 minutes) is absent.
- Partial holiday sessions.

**Event blackouts:** scheduled FOMC decisions for 2019–2020 were taken from the official federalreserve.gov
`fomchistorical2019/2020` pages. That gives 16 meetings; unscheduled meetings and notation votes are excluded.

## 4. Results (2019-02-01 → 2020-12-31, 10,000 account, 0.5% risk per trade, identical costs)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Win % | PF | Max DD % | Profitable months | vs random (20) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B (H1-led) | 3,802 | −96.1 | −0.194 (−0.228, −0.161) | −0.020 | 0.174 | 34.7 | 0.61 | 96.1 | 0/23 | 17/20 |
| B4H_only | 2,271 | −76.4 | −0.135 (−0.174, −0.098) | −0.028 | 0.107 | 38.2 | 0.70 | 76.8 | 2/23 | 6/20 |
| **B4H (4H + 1H)** | 1,975 | −87.0 | −0.219 (−0.264, −0.174) | −0.018 | 0.201 | 33.4 | 0.58 | 87.3 | 3/23 | 11/20 |

**Differences in mean R (bootstrap 95% CI):**
- B4H − B: −0.025 (−0.079, +0.030).
- B4H_only − B: +0.059 (+0.011, +0.108).
- B4H − B4H_only: −0.084 (−0.143, −0.026).

### Stress tests (net %, base spread profile unless noted)
| Variant | Comm 7 + slip 0.30 | Comm 14 + slip 0.50 | 2019–20 spread p50 | 2019–20 spread p90 |
| --- | --- | --- | --- | --- |
| B | −79.0 | −45.3 | −96.7 | −97.4 |
| B4H_only | −22.9 | −6.8 | −79.3 | −81.0 |
| B4H | −82.6 | −65.0 | −90.4 | −91.7 |

Higher commission or slippage reduces losses only because the net-RR ≥ 1.5 filter then rejects more trades.
Mean R stays negative in every scenario.

### Drawdown and consistency
| Variant | Max drawdown | Underwater | Longest losing streak | Worst month |
| --- | --- | --- | --- | --- |
| B4H | 87.3% | 699 days (never recovered) | 17 | 2019-04 |
| B | 96.1% | 699 days | 15 | 2019-02 |
| B4H_only | 76.8% | 670 days | 13 | 2020-05 |

- **Breakdowns:** every year, every quarter, every regime (up/down × high/low volatility), every session and both
  sides is negative for all three variants.
- **Research halts:** weekly 3%, 3 consecutive losses and session cap 2 reduce losses; none is positive.
- **3,000 account:** B4H takes 1,807 trades with 832 minimum-lot rejections.
- **Diagnostic grid** (12 settings per variant, not used for selection): 0/36 profitable.

## 5. Decision (pre-registered rules)
| Rule for B4H | Result |
| --- | --- |
| Base mean R > 0 with CI lower bound > 0 | Fail (−0.219) |
| Net > 0 with commission 7 + slippage 0.30 | Fail (−82.6%) |
| Beats ≥ 18/20 matched random | Fail (11/20) |
| Mean R > 0 in 2019 and 2020 | Fail (−0.241, −0.202) |
| Improvement over B (CI lower bound > 0) | Fail (−0.025, CI includes 0) |

**B4H is not supported. Adding the 1H alignment on top of the 4H bias makes results worse.**
- **4H-only:** the 4H filter alone is measurably less bad than the H1-led scalper (+0.059 R, CI excludes 0), but
  still loses money with no edge before costs.
- **Previous picture superseded:** the earlier favourable XAU signal (+0.048 R on 2 windows in 2026) does not
  generalize to 2019–2020; the 4H-led scalper has no edge before costs on unexamined data.
- **No re-tuning:** no parameters were changed on 2019–2020.
- **No prospective shadow test proposed:** the data was sufficient to test the hypothesis, and it failed.

## 6. Disclosure
The single evaluation run completed and wrote `report.json`. Formatting the Markdown summary then failed (a numpy
boolean in `json.dumps`). The formatter was fixed and the summary regenerated from the saved `report.json`;
the evaluation was not rerun.

## Reproduce
    python -m research.xau_tick_history download --start 2019-01-01 --end 2021-01-01 --output data/raw/mt5_xauusd_1m_ticks_2019_2020.csv
    python -m research.xau_tick_history validate --dataset data/raw/mt5_xauusd_1m_ticks_2019_2020.csv
    python -m research.xau_tick_history fomc --html fomchistorical2019.htm fomchistorical2020.htm --output data/processed/fomc_events_2019_2020.json
    python -m research.mtf_scalp4h_oos --workers 6

## Sources
1. MQL5 forum, "Limitation of historical data fetched using Python": https://www.mql5.com/en/forum/490426
2. MQL5 forum, "Max Bars in Chart": https://www.mql5.com/en/forum/462404
3. MQL5 forum, "FYI: Unexpected Behaviour of CopyRates": https://mql5.com/en/forum/262682
4. Federal Reserve FOMC historical pages: https://www.federalreserve.gov/monetarypolicy/fomchistorical2019.htm,
   https://www.federalreserve.gov/monetarypolicy/fomchistorical2020.htm

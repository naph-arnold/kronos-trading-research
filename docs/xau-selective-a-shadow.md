# XAUUSD Selective Strategy A: independent shadow forward test (prepared, not launched)

Status 2026-11-09: **prepared and tested; NOT launched.**
- A third runner starts only after the operator approves the launch-readiness checklist (section 6).
- Simulation only. Market data is read-only MT5. No orders of any kind.
- The XAUUSD NY ORB runner (Phase 2D) and the BTC/USDT Donchian runner (Phase 2E) are not touched: their code,
  parameters, pre-registrations, state and simulated accounts are unchanged.

## 1. Why Strategy A, and what does not count as evidence
Strategy A is the only multi-timeframe configuration with supporting evidence (`docs/mtf-hybrid-research.md`):

| Period | Role | Trades | Net mean R (95% CI) | Beats matched random |
| --- | --- | --- | --- | --- |
| 2025-06-02 → 2025-12-30 | development; parameters chosen here | 82 | +0.447 (+0.119, +0.796) | 20/20 |
| 2025-12-30 → 2026-11-06 | frozen parameters, **exploratory** | 277 | +0.109 (−0.070, +0.290) | 20/20 |

- **The edge decayed** and the 2026 confidence interval includes zero.
- **2026 had already been examined** in Phase 2A for other strategies.
- **Neither period is validation evidence.** The pre-registration lists both as excluded.
- Only bars closing at or after `unseen_window_starts` (2026-11-16 00:00 UTC) count as evidence.

## 2. What was built
| Item | File | Notes |
| --- | --- | --- |
| Strategy adapter | `trading/mtf/shadow_adapter.py` | Wraps the unchanged research generator `selective_candidates`. On each closed M5 bar it recomputes candidates over a trailing 60-day window (4H EMA50 converged) and emits a signal only if a candidate is anchored at that bar. An exact pre-check (the 5M trigger on the same window) skips about 87% of bars. |
| Fill-time cost rule | `net_rr_check` | The research rule: planned reward-to-risk after spread at the fill tick, slippage and commission must be ≥ 2.0. Otherwise the entry is rejected before the risk engine, as in the research simulator. |
| Engine | `CheckedShadowEngine` in `research/xau_selective_a_shadow.py` | Subclass of the shared `ShadowEngine`. Entry checks can only add rejections; approval and sizing stay with `trading.risk.evaluate`. |
| Runner | `SelectiveARunner` | Subclass of the hardened Phase 2D runner: heartbeat thread, read-only allowlist MT5 proxy, demo-account check, broker-verified sessions, CPI/NFP/FOMC guards, restart-safe store. Uses M5 bars. `start` and `poll` are mirrored so `research/phase2d_shadow.py` stays unchanged. |
| Pre-registration | `research/forward_test/xau_selective_a_preregistration.json` | Frozen parameters, controls, rules and criteria (section 4). |
| Config | `research/configs/xau_selective_a_shadow.json` | Own state directory `state/xau_selective_a_shadow`, and `launch_approved: false`. |
| Launch gate | `run` subcommand | Refuses to start without `--i-have-launch-approval`. |
| Tests | `tests/test_xau_selective_a_shadow.py` | 13 tests, all on synthetic data (section 5). |

**Frozen parameters:**
- `SelectiveParams(reward_to_risk=2.5, pull_atr=0.25, confirm_bars=6)`; every other field at the research
  default (min_net_rr 2.0, max hold 720 min = 144 M5 bars). Params hash `0e560ff37aa031db`.
- Shorts are allowed.

**Books (24):**
- Strategy A plus five random controls (seeds 1–5).
- Each at 10,000 and 3,000 USD × commission 0 and 7 USD/lot round trip.
- One position per book. 0.5% intended risk, 1.5% daily halt and 1% aggregate open risk, enforced by the unchanged
  risk engine.

**Random control:**
- Entry probability 0.0174 per M5 bar, stop 3.4 × ATR(14) of M5, RR 2.5, 144-bar limit, 50% long.
- Frequency and stop distance were calibrated on the **development period only**.
- The same net-RR cost check applies to the control.
- At each checkpoint, 20 matched random seeds are also re-simulated on the recorded live bars. This is reported but
  is not a pass criterion.

## 3. Differences from the research simulation (stated before any forward data)
- **Fills:** at the first live tick after the signal bar's close, at the real live spread. The research used the next
  M5 open plus the historical spread by New York hour.
- **Exits:** stop and take-profit are checked on every live tick. The research checked them on 1-minute or 5-minute
  bars, stop first.
- **Signal window:** recomputed over a trailing 60-day window rather than the full history.
  - On examined 2025-08 → 2025-09 data the adapter reproduced the research candidates exactly (181/181).
  - This is a mechanics check only, not evidence.
- **Start-up time:** the first start replays warm-up M5 bars from 2026-09-01 (about 3 minutes of CPU). A restart
  replays every stored bar (about 10 minutes after a year). The heartbeat thread keeps writing throughout.

## 4. Pre-registered evaluation
Primary book: 10,000 USD, 7 USD/lot round-trip commission.

| # | Criterion (all required) |
| --- | --- |
| 1 | Net mean R per trade > 0 |
| 2 | Bootstrap 95% CI lower bound of net mean R > 0 |
| 3 | Net mean R above every one of the 5 random-control books |
| 4 | Positive net result in ≥ 2 of the last 3 complete two-month blocks |
| 5 | Max drawdown < 8% |

**Checkpoints:**
- Interim report at 50 trades. There is no stopping for success.
- Final checkpoint: at least 150 trades **and** at least 183 days from the unseen start, whichever comes later.
- Hard end: 548 days.

**Outcomes:**

| Outcome | Condition | Consequence |
| --- | --- | --- |
| Pass | All criteria pass | Eligible for operator review only; no automatic promotion |
| Inconclusive-positive | Mean > 0 and every criterion except the CI passes | One pre-declared extension to 300 trades, with no changes |
| Retire | Anything else | Strategy is retired |
| Safety stop | Drawdown ≥ 10% | Retires immediately |

Calculation: `selective_a_criteria`. Run it with `python -m research.xau_selective_a_shadow report`.

**Power.** With RR 2.5 the per-trade standard deviation is about 1.6 R. At 150 trades the CI half-width is about
±0.26 R. A true edge of +0.11 R (the 2026 exploratory estimate) would therefore most likely end
"inconclusive-positive". The criteria are deliberately strict: this test is meant to validate, not to find
another candidate.

## 5. Verification
`tests/test_xau_selective_a_shadow.py` contains 13 tests, all on synthetic data:
- adapter parity with the research generator, bar by bar;
- signal price rounding and target;
- interval and window guards;
- the net-RR formula, long and short;
- the engine check rejects before the risk engine and otherwise defers to it;
- the config matches the frozen pre-registration, and drift in parameters, window, control, risk or state directory
  is refused;
- the running experiments' pre-registration hash is unchanged;
- the launch gate;
- no order or AI calls;
- runner start, M5 polling, 24 books, heartbeats, and restart without signal mismatch or breaker trip;
- no verdict before the final checkpoint;
- the frozen random calibration.

**Separate parity check:** on real examined 2025-08 → 2025-09 M5 data, 181/181 candidates were identical. The script
is in the session scratchpad and is not repository evidence.

## 6. Launch readiness checklist (operator approval required)
| # | Item | Status |
| --- | --- | --- |
| 1 | Operator approves launching a third runner | **Pending** |
| 2 | PR #16 runner code (heartbeat thread, adopt-mode supervisor) is in the deployed checkout. This branch contains it via a merge. | Code ready; deployment pending |
| 3 | **MT5 multi-client check:** one short `run --once` probe with approval, while the Phase 2D heartbeat is monitored. It shows that a third read-only process can attach to the same terminal without disturbing the ORB runner. | **Not done** (it touches the terminal, so it needs approval) |
| 4 | **Memory headroom:** at least 1.5 GB free with all three runners. On 2026-11-08 at 20:50 UTC free memory was 1.40 GB with two runners plus research jobs. | **Not met yet**; re-check with no research jobs running |
| 5 | Supervisor task for module `research.xau_selective_a_shadow` (copy of the Phase 2D Task Scheduler entry, own STOP file). This changes Task Scheduler. | Prepared in docs only; not installed |
| 6 | Extend `research/dual_health_report.py` to cover the third state directory | Not started (ops change) |
| 7 | CPI/NFP schedule file covers the test (it runs to 2027-12) | OK |
| 8 | If not warmed up by 2026-11-13 21:00 UTC, commit the one allowed start-date amendment before the new date | Rule recorded |

**Launch command (after approval only):**
```
python -m research.xau_selective_a_shadow run --i-have-launch-approval
```
- Status: `python -m research.xau_selective_a_shadow status`
- Report: `python -m research.xau_selective_a_shadow report`
- Emergency stop: create `state/xau_selective_a_shadow/KILL`, which rejects new simulated entries.
- Graceful exit: create `state/xau_selective_a_shadow/STOP`.

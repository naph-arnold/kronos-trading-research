# Kronos: consolidated status and next-step plan (2026-11-09)

Goal: a reliable trading system built only from **independently validated** strategies. Python is the central
engine, Kronos the forecaster, and six Grok agents on the official xAI API are advisory.
- Risk limits are unchanged: 0.5% per trade, 1.5% daily halt, 1% aggregate.
- No live trading, no demo/testnet orders and no MQL5 execution.

## 1. Running shadow systems (read-only verification, 2026-11-08 ~20:50 UTC)
| Runner | State | Evidence |
| --- | --- | --- |
| XAUUSD NY ORB (Phase 2D) | Running, ready for the 2026-11-09 forward start | 100% heartbeat uptime over the last 6 h. One 11.6-min gap at 13:18–13:30 UTC (the runner exited without a traceback and the supervisor restarted it; cause undetermined). Pre-registration consistent. 28 books flat. MT5 demo account unchanged. Entries correctly blocked while the market is closed; the clock offset is re-verified on the first fresh tick. |
| BTC/USDT Donchian (Phase 2E) | Running, ready | 100% uptime since 2026-11-08 04:45 UTC, no gap over 3 min. Websocket connected; fresh data ≥ 99.8%. Pre-registration consistent. 18 books flat. |

- **Not modified:** neither runner was restarted or changed.
- **Open concern:** free memory was 1.40 GB of 15.4 GB with research jobs present.
- **To check after the Sunday 23:00 UTC open:** the XAU calendar shows `broker_verified` and live gold data flows.

## 2. Pull requests #9–#20 (all open, none merged; mergeable)
| PR | Branch → base | Content | CI (workflow_dispatch) |
| --- | --- | --- | --- |
| #9 | docs/dual-market… → master | Architecture docs | success |
| #10 | phase1-risk-foundations → #9 | Risk engine, schema, foundations | success 42258475473 |
| #11 | phase2-market-research → #10 | Read-only research pipelines, calendars | success 42289799965 |
| #12 | phase2d-shadow-forward-test → #11 | XAU shadow system (running code base) | success 42342318588 |
| #13 | docs/python-first… → #12 | ADR-001 Python-first; MQL5 deferred | docs only (no run) |
| #14 | phase2e-binance-shadow → #12 | BTC shadow runner (**running** from this branch) | success 42448295495 |
| #15 | fix/xau-shadow-heartbeat → #12 | XAU heartbeat fix | success 42450677473 |
| #16 | ops/dual-runner-reliability → #15 | Supervisor, tasks, power, health report (**XAU runner runs from this branch**) | success 42454441493 |
| #17 | research/mtf-hybrid → #14 | MTF framework; Strategy A promising on XAU | success 42458847551 |
| #18 | research/mtf-scalp4h → #17 | 4H-led scalping: not improved | success 42480554331 |
| #19 | research/xau-scalp4h-oos → #18 | 2019–20 OOS: not supported | success 42483246868 |
| #20 | research/volume-profile → #19 | Volume Profile: not supported | success 42506095087 |

**Dependencies and overlaps:**
- **Two branches off #12 both change the shared shadow code:**
  - #14 generalised `trading/shadow/engine.py` for BTC.
  - #15/#16 hardened `research/phase2d_shadow.py`.
  - The research stack (#17–#20) is based on #14 and lacks #15/#16.
  - Merging both lines needs a conflict resolution in `.github/workflows/ci.yml` and `docs/PROGRESS.md` only. That
    resolution is done and tested in the new #21 branch (531 tests).
- **Two six-agent desks exist:**
  - `research/six_agent_desk.py` is the Phase 1 BTC paper-eligibility prototype.
  - The new advisory desk (#22) is separate on purpose, because the old one lets role verdicts gate paper
    eligibility.
  - Keep the old one as legacy; retire it in a later clean-up PR.

**Suggested merge order** (only with your approval):
1. #9 → #10 → #11 → #12, then #13, #14, #15 → #16.
2. Then #17 → #18 → #19 → #20 → #21 → #22.

Use "Create a merge commit" (not squash), so the stacked branches stay valid.

**Running-system caution:** the XAU runner runs from the `ops/dual-runner-reliability` checkout and the BTC runner
from the `feat/phase2e-binance-shadow` worktree. Merging on GitHub does not affect them. Do not switch those
checkouts to `master` except at a planned, approved restart.

## 3. New work in this session (not merged)
| PR | Content | Status |
| --- | --- | --- |
| #21 `feat/xau-selective-a-shadow-prep` (→ #20, with #16 merged in) | Independent Strategy A shadow test: adapter, runner subclass, pre-registration (unseen start 2026-11-16), own state, 24 books, launch gate | Prepared and tested; **not launched** |
| #22 `feat/phase4-grok-advisory` (→ #21) | Six-agent advisory contracts, desk, separate store, gated xAI transport | Implemented and tested; **not activated** |

## 4. Research verdicts so far (preserved, not re-optimised)
| Line | Verdict |
| --- | --- |
| XAU NY ORB | In forward test (Phase 2D) |
| BTC Donchian | In forward test (Phase 2E) |
| XAU Selective Strategy A | Promising but unproven; independent shadow test prepared |
| MTF scalping (B), hybrid (C), 4H-led scalping (both markets), XAU 2019–20 OOS scalping | Not supported |
| Volume Profile (BTC measured volume; XAU proxy) | Not supported; no shadow test |

## 5. Implementation plan: Selective Strategy A forward validation
| Step | What | Who / gate |
| --- | --- | --- |
| A1 | Review and approve PR #21 (pre-registration, criteria, books, risk) | You |
| A2 | Memory headroom: stop leftover research jobs; confirm ≥ 1.5 GB free with both runners | Read-only check |
| A3 | MT5 multi-client probe: `run --once --i-have-launch-approval` against a scratch state while watching the Phase 2D heartbeat. This proves a third read-only client does not disturb the ORB runner. | Your approval (touches the terminal) |
| A4 | Ops: Task Scheduler entry plus supervisor for `research.xau_selective_a_shadow`; extend `dual_health_report` to three runners | Your approval (Task Scheduler change) |
| A5 | Launch before 2026-11-13 21:00 UTC so the warm-up completes before the 2026-11-16 unseen start. Otherwise commit the single allowed start-date amendment. | Your approval |
| A6 | Weekly health checks; interim report at 50 trades; final checkpoint at ≥ 150 trades and ≥ 6 months | Pre-registered |
| A7 | On "pass", operator review only: no automatic promotion. Demo execution would be a separate Phase 3 decision. | You |

## 6. Implementation plan: Phase 4 Grok integration
See `docs/phase4-grok-advisory.md` section 6:
- 4.1 contracts (done);
- 4.2 recorded Kronos feed;
- 4.3 activation smoke test (approval and key);
- 4.4 scheduled advisory reviews in a separate process;
- 4.5 a pre-registered evaluation of whether the advice is informative;
- 4.6 any use of advice in decisions only as a new pre-registered experiment.

## 7. Decisions waiting for you
- **Merges:** PRs #9–#22.
- **Strategy A:** A1–A5 above.
- **Grok:** activation (paid usage, model id, key).
- **Earlier open items:**
  - MT5 "Max bars" setting, to allow unexamined pre-2025 M5 history;
  - commission confirmation;
  - CPI/NFP re-check before 2027-11.

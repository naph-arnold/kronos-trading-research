# XAUUSD Selective Strategy A: independent shadow forward test

Status 2026-11-09: **LAUNCHED 05:55:30 UTC** (operator-authorized local shadow test; see section 10). Forward evidence starts at the pre-registered boundary 2026-11-16 00:00 UTC; everything earlier is warm-up.
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
| Launch gate | `run` subcommand | Refuses to start without launch approval: `--i-have-launch-approval`, or `"launch_approved": true` in the reviewed config (the form the supervisor uses). |
| Single instance | `StateLock` (`<state>/runner.lock`) | One Strategy A process per state directory, whoever starts it. The supervisor's mutex and running-runner scan are a second layer. |
| Preflight | `preflight` subcommand | One-off, operator-approved MT5 compatibility test on a scratch state: start, warm-up, one poll, disconnect, restart. It refuses runner state directories, non-empty directories, a second Strategy A process and an unhealthy Phase 2D heartbeat. Results are in section 7. |
| Task Scheduler | `ops/windows/selective_a_task.ps1` | Dry run by default. `-Apply` refuses unless the dedicated checkout exists, its config says `launch_approved: true` and the pre-registration hash matches. Not registered. |
| Health report | `research/dual_health_report.py` | Optional third market `xauusd_selective_a`, reported as "not started" until its state exists. |
| Tests | `tests/test_xau_selective_a_shadow.py` | 19 tests, all on synthetic data (section 5). |

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
- **Start-up time (measured 2026-11-08, section 7):**
  - The first start loads and replays the warm-up M5 bars from 2026-09-01 (12,957 bars): 126 s.
  - A restart replays every stored bar at about 13–15 ms per bar: 168 s for 12,957 bars. Projected: about
    17–18 minutes after a year, and about 26 minutes at the 548-day hard end.
  - The heartbeat thread keeps writing throughout.
  - **No new signal is acted on during a restart.** The decision-latency guard rejects late fills, so a long
    restart costs opportunities but cannot create stale entries.

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
`tests/test_xau_selective_a_shadow.py` contains 19 tests, all on synthetic data:
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
- the frozen random calibration;
- the state lock refuses a second process;
- launch approval only through the flag or the reviewed config;
- preflight guards: approval, runner and non-empty state directories refused;
- the preflight warm-up, poll and restart on a scratch state, with the peer store byte-identical;
- the connector probe blocks every write function;
- KILL and STOP live in Strategy A's own state directory: KILL blocks entries, STOP prevents a start.

**Separate parity check:** on real examined 2025-08 → 2025-09 M5 data, 181/181 candidates were identical. The script
is in the session scratchpad and is not repository evidence.

## 6. Launch readiness checklist (operator approval required)
| # | Item | Status |
| --- | --- | --- |
| 1 | Operator approves launching a third runner | **Pending** |
| 2 | PR #16 runner code (heartbeat thread, adopt-mode supervisor) is in the deployed checkout. This branch contains it via a merge. | Code ready; deployed with the dedicated checkout (item 9) |
| 3 | **MT5 multi-client check** | **Passed 2026-11-08 23:15–23:21 UTC** (section 7) |
| 4 | **Memory headroom:** at least 1.5 GB available with all three runners | 2026-11-09 01:45 UTC: 2.3 GB available, commit 34.6 of 41.3 GB, CPU 14% (12 logical CPUs). The third runner needs about 170 MB peak working set and about 500 MB commit. Recovery options are in section 8; none applied. **Re-check at launch.** |
| 5 | Supervisor task `Kronos Shadow XAUUSD Selective A` (`ops/windows/selective_a_task.ps1`; supervisor `-Name xauselectivea`) | **Prepared; dry run verified; not registered** |
| 6 | Health report covers the third state directory | **Done** (optional market; takes effect when the gold checkout's report is updated) |
| 7 | CPI/NFP schedule file covers the test (it runs to 2027-12) | OK |
| 8 | If not warmed up by 2026-11-13 21:00 UTC, commit the one allowed start-date amendment before the new date | Rule recorded |
| 9 | Dedicated checkout `C:\Users\Admin\Projects\kronos-selective-a-shadow` (git worktree, branch `deploy/xau-selective-a`). Never run from the gold or crypto checkouts. | **Created 2026-11-09** (no state directory, nothing running). The config loads with the frozen pre-registration hash `b8574f53…`. The Strategy A, backup and health-report tests pass from this checkout. |
| 10 | Daily state backup (`research/state_backup.py`; task `Kronos Backup XAUUSD Selective A`, 00:30 local = 21:30 UTC, to `C:\Users\Admin\KronosBackups\xau_selective_a_shadow`, 14 sets kept) | **Prepared; dry run verified; not registered** (registered by `-Apply` together with the runner task) |

**Launch procedure (after approval only):**
1. The dedicated worktree exists (branch `deploy/xau-selective-a`). Bring it to the reviewed commit with
   `git -C C:\Users\Admin\Projects\kronos-selective-a-shadow merge --ff-only <reviewed commit>`.
2. In a reviewed commit on that branch, set `"launch_approved": true` in
   `research/configs/xau_selective_a_shadow.json`. The pre-registration and parameters are unchanged;
   `load_config` refuses any drift.
3. Run `ops\windows\selective_a_task.ps1` as a dry run and confirm "Pre-launch checks met".
4. Run `ops\windows\selective_a_task.ps1 -Apply`. This registers the runner task and the daily backup task; the
   supervisor starts the runner. `-Apply` refuses to overwrite an existing task, because re-registering a task
   ends its running instance.
5. Watch the first heartbeats, the `warmup_loaded` event and the health report. Warm-up takes about 2 minutes.

A manual one-off alternative to steps 2–4 is `python -m research.xau_selective_a_shadow run --i-have-launch-approval`.
- Status: `python -m research.xau_selective_a_shadow status`
- Report: `python -m research.xau_selective_a_shadow report`
- Emergency stop: create `state/xau_selective_a_shadow/KILL`, which rejects new simulated entries.
- Graceful exit: create `state/xau_selective_a_shadow/STOP`.

## 7. Preflight and resource benchmark (2026-11-08)
**Live MT5 preflight.** Approved by the operator as a one-off compatibility test, not a launch. Command:
`python -m research.xau_selective_a_shadow preflight --i-have-preflight-approval --state-dir <scratch>`.
The run was scheduled for 23:15:50–23:20:47 UTC: the gold runner's books were flat, and its next decision point was
the 23:30 bar close.

**Before the run:**
- the Phase 2D heartbeat was fresh;
- no other Strategy A process was running;
- 2.17 GB of RAM was free;
- CPU was at about 13%.

| Check | Result |
| --- | --- |
| Connection | Attached to the running terminal without login arguments (no `MT5_DEMO_*` variables set). MetaQuotes-Demo; demo account confirmed. |
| Read-only proxy | Active. Every write-capable function the installed MetaTrader5 package exposes (`order_send`, `order_check`, `market_book_*`) is blocked. None was called. |
| Demo account | Balance 3000, equity 3000, 0 positions, 0 orders: unchanged |
| Warm-up | 12,957 M5 bars (2026-09-01 → 2026-11-08 23:15), calendar `broker_verified`, 24 books, breaker clear, 1,272 warm-up signals, 0 decisions |
| First poll | 593 live ticks; 0 signals, entries or exits |
| Restart recovery | 12,957 bars restored, 0 signal mismatches, reconciliation ok, breaker clear |
| Phase 2D runner during the preflight | 5 heartbeats in 5 min (largest gap 60 s); MT5 connected on every beat; no disconnect or interruption events; no decisions affected |
| Calendar providers | The ECB fetch timed out on the first start (`URLError` 10060) and succeeded on the restart 2 minutes later. It was recorded as `calendar_refresh_failed`, which the health report flags. |
| Timing | Start and warm-up 126 s, poll < 0.1 s, restart restore 168 s, total 295 s |
| Memory (preflight process) | Working set 110–113 MB, peak 167 MB. Private commit 440 MB, peak 494 MB (mostly imported libraries). |

**Offline benchmark (synthetic, fake feed):**
- 15,042 M5 bars;
- warm-up 112 s and restore 223 s;
- peak working set 128 MB;
- 3.9 MB of state for 2.5 months, so about 20 MB per year.

**Projected third-runner footprint:**
- about 120–170 MB working set and about 500 MB commit;
- a single core during warm-up or restore, and near idle otherwise (5-minute bars);
- about 20 MB of disk per year.

For comparison, the running Phase 2D runner holds 425 MB of commit.

## 8. Launch preparation assessment (2026-11-09)
**Verdict: ready for launch approval.** Every technical item is met or prepared. Two operator decisions remain:
approve the launch (item 1) and confirm memory headroom on the day (item 4).

**Resources (measured 2026-11-09 01:45 UTC, two runners running):**

| Measure | Value |
| --- | --- |
| Physical memory | 15.35 GB total; 2.3 GB available (1.7 GB of it standby cache); 55 page-ins/s |
| Commit | 34.6 of 41.3 GB, so 6.7 GB of headroom against the third runner's ~0.5 GB |
| CPU | 14% average over 12 logical CPUs. The third runner uses one core for ~2 min at warm-up, then near idle. |
| Disk C: | 178 GB free; the third runner writes ~20 MB per year plus backups (14 × ~5–20 MB) |
| Largest users (working set / private) | Chrome 3.9 / 8.4 GB (52 processes); WSL `vmmemWSL` 1.5 / 7.5 GB; ChatGPT apps 1.8 / 3.7 GB; Claude Code 0.7 / 1.6 GB; the two runners 0.16 / 1.1 GB together |

**What WSL is holding.** `vmmemWSL` is the Docker Desktop VM (the only distribution is `docker-desktop`; there
is no `.wslconfig`). It runs two local Supabase stacks of the Settleward projects, 22 containers that have been
up for 2 days and use about 3.6 GB inside the VM. Of the 7.5 GB it has committed, only about 1.5 GB is resident;
Windows has already paged out or compressed the rest. Within it:
- each stack's `logflare` analytics container uses about 0.66 GB, and one was at about 75% of a CPU core;
- both `supabase_vector` containers are in a restart loop.

**Safe recovery options (none applied; all need your decision):**
1. **Close unused Chrome tabs and windows, and one of the two ChatGPT apps.** This is the largest resident
   saving, roughly 1–3 GB, with no effect on research.
2. **Stop the Supabase stack you are not using** (`supabase stop` in that project). This frees about 1.8 GB inside
   the VM, which WSL then returns gradually.
3. **Disable the analytics and vector services in the stacks you keep** (`[analytics] enabled = false` in that
   project's `supabase/config.toml`, then restart that stack). This saves about 1.3 GB and stops the restart loop
   and the CPU burn.
4. **Cap WSL and let it return its page cache.** Create `%UserProfile%\.wslconfig` with `[wsl2] memory=6GB` and
   automatic memory reclaim (`autoMemoryReclaim=gradual`; check the setting's section for WSL 3.0.3 in
   Microsoft's WSL configuration documentation). This takes effect only after `wsl --shutdown`, which stops
   every container, so schedule it when the Settleward work allows.
5. **Optional:** the `MySQL92` service (about 0.6 GB private, automatic start). Set it to manual if it is not
   used.

None of these touch the runners or MT5.

**Recommendation.** Before launch, do 1, and 2 or 3. That gives a comfortable margin of about 3–5 GB available.
Option 4 is the durable fix, but it needs a container restart window. With nothing done, the launch is still
feasible: the third runner adds about 0.17 GB resident, and commit headroom is ample. Paging pressure would
rise, though, and that already correlates with occasional heartbeat stalls of up to about 3 minutes on the BTC
runner (a hypothesis, not established).

**Controls verified** (tests on synthetic data, run from the dedicated checkout):

| Control | How it is enforced |
| --- | --- |
| KILL | Rejects new entries |
| STOP | Prevents a start, and the supervisor never restarts while it exists |
| Duplicate instance | `StateLock` refuses a second process on the same state; the supervisor mutex and running-runner scan add two more layers |
| Restart recovery | Bars, books, signals and decisions are restored with 0 mismatches, also on live MT5 data in the preflight (section 7) |
| Backup | Consistent online copy through a read-only connection, integrity check, digest manifest, retention, tamper detection |

## 10. Launch record (2026-11-09)
**Authorization.** The operator authorised a local, continuous, read-only shadow-testing process, provided every
preflight check passed. Launch steps without separate approval were excluded: no new scheduled tasks and no
operating-system changes.

**Preflight:**

| Check | Result |
| --- | --- |
| Frozen parameters and pre-registration hash | `load_config` accepted; hash `b8574f53…` |
| Forward start | `unseen_window_starts` 2026-11-16T00:00:00Z, unchanged |
| State directory | absent before launch (clean); 24 books created at start |
| NY ORB and Donchian runners | running, heartbeats fresh, MT5 and WebSocket connected |
| Safety tests (deployment checkout) | 21/21: STOP/KILL, restart recovery, duplicate instance, MT5 write functions blocked, backup |
| Memory | First check (~05:40 UTC): **0.78–1.11 GB available, FAILED** (launch held). Re-check (~05:53 UTC): 2.47–2.57 GB available, commit headroom 4.7 GB, CPU ~11%: passed. Nothing was stopped to free memory. |

**Launch:**
1. Approval commit `c731b45` on `deploy/xau-selective-a` sets only `launch_approved: true`.
2. The task-script dry run reported "Pre-launch checks met"; the task was **not** registered.
3. The shared supervisor was started as a hidden process with the same arguments as the prepared task
   (`-Name xauselectivea`; supervisor pid 828, runner pid 25972).
4. It is **not persistent**: it stops at logoff or reboot until the operator approves registering the task.

**Verified after launch:**
- MetaQuotes-Demo demo account: balance and equity 3000, 0 positions, 0 orders, unchanged.
- Read-only proxy active.
- Warm-up loaded: 13,037 M5 bars (2026-09-01 → 2026-11-09 05:55).
- 24 books: Strategy A + 5 random controls × {10,000, 3,000 USD} × {0, 7 USD/lot}. Realized P&L 0; reconciliation ok.
- Heartbeat: running, MT5 connected, data fresh, calendar `broker_verified`, guards pass.
- Signals so far: warm-up only, 0 decisions and 0 trades.
- Initial-state backup `C:\Users\Admin\KronosBackups\xau_selective_a_shadow\backup_20261109T055756Z`, verified
  (`--verify` ok).
- Command Center: lifecycle **running**; the decision card uses Strategy A's own heartbeat (fix `386bca5`).

**Observed side effect to watch:**
- The BTC runner had a 222 s heartbeat gap, 05:55:17 → 05:58:59. It began 13 s before Strategy A started and overlapped
  its warm-up.
- A 178 s gap had already occurred at 05:35, before the launch.
- No restart, no error event, status ok afterwards. See command-center.md section 8.1 for the existing BTC gap
  analysis.

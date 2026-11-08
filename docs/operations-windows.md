# Kronos shadow runners — Windows operations, reliability evidence and runbook

Status 2026-11-08. Both runners are simulation-only (no MT5, Binance or testnet orders are possible). Nothing in
this document has been applied to the system unless it is marked **applied**.

## 1. Evidence: XAUUSD supervisor termination (2026-11-08 05:06 UTC)
| Evidence (source) | Finding |
| --- | --- |
| Shadow store: last event 05:06:20, tick cursor 05:06:18, heartbeat thread silent from 05:07:14 | Runner and its heartbeat thread stopped between 05:06:20 and 05:07:14 UTC |
| `runner.log` | No "runner exited" line, so the supervisor did not survive the runner |
| Windows PowerShell log (400/403) | Engine start (400) for supervisor PID 11228 at 08:06:14 local and **no engine-stop (403)** record. A graceful exit logs 403 (the previous supervisor's STOP exit did, at 08:05:58) |
| Application log, WER, Application Error/Hang | No crash or hang reports in 08:05–08:16 local |
| Security 4689 (process exit) | Not recorded: process-termination auditing is disabled, so the terminating party is unknown |
| Same code in the foreground (2 min, real MT5) and the relaunch at 05:15 UTC | Ran normally with heartbeats every 60 s |
| BTC supervisor launched the same way (WMI) at 04:45 UTC | Unaffected |
| WMI host hypothesis | **Refuted**: both surviving supervisors' WMI parent hosts (27608, 9072) have exited and the supervisors live on |
| Other activity in the window | An OpenAI Codex service and app update were installed at 08:09 local (after the termination); no evidence links them |

**Conclusion:** an abrupt, targeted termination (no clean shutdown, no crash) of the XAUUSD supervisor tree. The
cause is **undetermined** with the logs available. To capture the cause next time, enable process-termination
auditing. This is a persistent change and needs administrator rights and approval:
`auditpol /set /subcategory:"Process Termination" /success:enable`. Scheduled tasks also record start and stop
history with result codes.

## 2. Evidence: sleep (Modern Standby) is the main cause of "broker disconnections"
- `powercfg /a`: only *Standby (S0 Low Power Idle) Network Connected* and Hibernate are available.
- The plan is Balanced with AC display-off 180 s and AC sleep 180 s.
- From 11-07 14:40 to 11-08 07:23 local the System log shows the PC mostly in Modern Standby (Kernel-Power
  506/507), with brief wake bursts.
- XAUUSD reconnect/disconnect times match standby exit/entry exactly: 17:29:22 / 17:31:07 local ↔ 14:30 / 14:31
  UTC, 03:21:21 local ↔ 00:21–00:22 UTC, and 07:23 local (power button) ↔ 04:23 UTC.
- Only the first disconnection (05:58 UTC on 11-07) began while the PC was awake.
- On 11-08 at 05:09:26 UTC the PC entered standby on "Idle Timeout" while a process-scoped `ES_SYSTEM_REQUIRED`
  request was active. On this hardware that request does not prevent idle standby.
- `ES_DISPLAY_REQUIRED` (**applied** in the XAUUSD supervisor) prevents it, but keeps the screen on.

**Recommended fix without a permanently-on screen** (`ops/windows/power_settings.ps1`, dry run by default):
- Set the AC sleep timeout to *never*: `powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0`.
- Leave the AC display timeout at 3 minutes, so the screen still turns off.
- Optionally (`-LidDoNothing`), make closing the lid on AC do nothing.
- Battery settings are untouched.
- `-Apply` stores the previous values, and `-Revert` restores them.

After applying it, switch the supervisors to `-KeepAwake system`, which lets the screen turn off. Confirm with the
System log that no "Idle Timeout" standby occurs during a monitored day.

## 3. Supervision: Task Scheduler (prepared, not applied)
`ops/windows/scheduled_tasks.ps1` (dry run prints the XML; `-Apply` registers; `-Remove` unregisters):
- **Tasks:**
  - **Kronos Shadow XAUUSD** and **Kronos Shadow BTCUSDT:** at user log-on, plus a check every 15 minutes.
    Interactive user only, because the MT5 terminal runs in the user session. No time limit, restart on failure.
    `MultipleInstances=IgnoreNew`.
  - **Kronos Daily Health:** daily at 00:15 local; runs the read-only report.
- **Supervisor guards:** each task runs `ops/windows/kronos_supervisor.ps1`, the hardened supervisor:
  - Single instance per market: a named mutex, plus a scan that exits if any process already runs that market's
    runner module. This was verified live against the running XAUUSD runner and started nothing.
  - **STOP is authoritative:** checked before every (re)start. The script never creates, deletes or renames
    STOP/KILL; verified live (STOP present → no runner started, STOP left in place).
  - **KILL** is enforced by the runner's circuit breaker on every decision, so restarts cannot bypass it.
    Breaker trips persist until `reset-breaker --operator`.
- **Migration:** with the tasks applied while the current supervisors run, task-started supervisors find the
  running runners and exit. No duplicate is created and no restart is needed. After the next log-on the tasks own
  supervision.

## 3a. Applied 2026-11-08 (operator-approved) and verified
| Change | Result |
| --- | --- |
| Power plan (`power_settings.ps1 -Apply`) | AC sleep 180 s → **never**. AC display-off stays 180 s. DC (battery) sleep 180 s and display 180 s unchanged. Lid action unchanged. Hibernate unchanged. Original values in `ops/windows/power_settings_backup.json` (git-ignored) |
| Rollback | Verified: `-Revert` restored 180 s and removed the backup; re-applied. Fixed a bug found in testing: a second `-Apply` used to overwrite the original backup and now keeps it |
| Sleep verification | No process holds a display request. Over 25 min, user-input idle reached 1,037 s and the System log recorded **0 Modern Standby entries** (no Kernel-Power events at all). Before the change the PC entered standby after 180 s idle. Screen-off itself is not logged on this machine |
| Scheduled tasks (`scheduled_tasks.ps1 -Apply -KeepAwake system`) | **Kronos Shadow XAUUSD**, **Kronos Shadow BTCUSDT** (log-on + 15-min check, IgnoreNew, Interactive) and **Kronos Daily Health** (00:15 local) registered |
| Before or after sign-in? | **After sign-in only.** The tasks use an Interactive principal, `AutoAdminLogon` is not set, and the MT5 terminal needs the user session. After a reboot nothing runs until the user signs in, then both supervisors start at log-on. Pre-login startup would need a stored password or S4U task (not configured; requires approval and would not work for MT5) |
| Supervisor hardening found during testing | Runner output was piped through the supervisor, so killing a supervisor killed its runner (reproduced with a synthetic runner). Supervisors now start runners with `Start-Process` and redirect output to `<state>\logs\runner_<time>.{out,err}.log`, so runners survive supervisor death and are adopted |
| Recovery tests (synthetic runner + temporary task, then removed) | Orphaned runner adopted (no duplicate). Second trigger ignored (IgnoreNew). Killed runner restarted once after the grace period. STOP made the runner and supervisor stop with no restart. A trigger with STOP present started nothing and left STOP in place |
| Production handover | BTC: the task supervisor **adopted** the running runner, which was not restarted. XAUUSD: retiring the legacy supervisor (which held the display request) needed a graceful STOP → restart. Interruption **05:54:06–05:55:51 UTC (1 min 45 s)**, market closed, pre-forward warm-up. Backup `state/backups/phase2d_shadow_20261108T055357Z_pre_task_handover`. Recovery exact (6,166 bars, 188 signals, 0 mismatches) |

Notes:
- The legacy BTC supervisor (`run_binance_shadow.ps1`, PID 33048) still owns its runner. The BTC task supervisor
  adopts it, so if either dies the other path recovers. A runner restarted by the legacy supervisor within its
  30 s delay is re-adopted (the task supervisor waits a 90 s grace period).
- The main checkout must stay on a branch containing `ops/windows/` (currently `ops/dual-runner-reliability`)
  while the tasks are registered. To remove the tasks: `ops/windows/scheduled_tasks.ps1 -Remove` (this does not
  stop running supervisors; use STOP for that).

## 4. Runbook
| Action | Command / file |
| --- | --- |
| Stop a market deliberately | `New-Item <state>\STOP`; it stays stopped across restarts, tasks and log-ons until the file is removed |
| Emergency: no new simulated entries | `New-Item <state>\KILL`; the runner keeps monitoring and managing open simulated positions |
| Resume after STOP | Remove `STOP`; the next task check (≤ 15 min) or a manual supervisor start resumes |
| Daily health (read-only) | `.venv311\Scripts\python -m research.dual_health_report [--hours 24]` → `research/output/health/` |
| Per-market detail | `python -m research.phase2d_shadow status|report` (gold); `python -m research.phase2e_binance_shadow status|report` (crypto, from its worktree) |

State directories: XAUUSD `kronos-trading-research\state\phase2d_shadow`; BTC/USDT
`kronos-binance-shadow\state\phase2e_binance_shadow`. Backups: `state\backups\` (SQLite online-backup API, SHA-256
manifests).

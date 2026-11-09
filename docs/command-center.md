# Kronos Trading Command Center V1 (Phase 5)

A working, read-only web dashboard for the shadow forward tests. Branch `feat/command-center-v1` (worktree
`C:\Users\Admin\Projects\kronos-command-center`), stacked on PR #23.

**Start:** double-click `Kronos Command Center.cmd` in the worktree folder. It starts the dashboard if it is not
running and opens <http://127.0.0.1:8765>. **Stop:** double-click `Stop Kronos Command Center.cmd`. See section 6.

## 1. Architecture
| Layer | Choice | Notes |
| --- | --- | --- |
| Engine | Python, unchanged | The runners are not modified, imported into or restarted by the dashboard |
| Backend | FastAPI (`command_center/api.py`) + uvicorn | GET-only. Any other method returns 405. Bound to `127.0.0.1:8765`. One process, about 55 MB working set. Responses `no-store`, gzip |
| Data access | `command_center/readonly.py` | Every SQLite connection uses `mode=ro` + `PRAGMA query_only`. `ReadOnlyShadowStore` reuses `ShadowStore`'s queries and refuses its write methods |
| Reused logic | `research.dual_health_report.market_health`, `trading.shadow.report.book_metrics` and `reason_key`, `trading.strategies.xauusd.SESSIONS`, `trading.agents.desk`, `research.grok_advisory.AuditLog` | No trading logic is re-implemented |
| Frontend | Next.js 14.2 (App Router, static export), React 18, TypeScript, Tailwind 3.4, TradingView Lightweight Charts 4.2 | `next build` produces `command_center/web/out/`, which FastAPI serves. **No Node process runs in production.** |
| Updates | Polling | Overview every 15 s, strategies every 20 s, charts every 60 s. Polling pauses while the tab is hidden. The backend caches each view for 5–10 s, so several open tabs cannot load the stores. SSE/WebSockets are not justified at 15-minute and 1-hour bar intervals. |
| Isolation | Own venv `.venv-cc` (FastAPI is **not** installed in the runners' `.venv311`), own worktree, own port | Stopping or crashing the dashboard has no effect on the runners |
| VPS later | Config paths in `command_center/config.json` (or `KRONOS_CC_CONFIG`) | Before exposing it beyond localhost: a reverse proxy with authentication and TLS. There is no auth in V1 because it binds to localhost only. |

## 2. What is shown, and how it is labelled
Every value carries one of four tags:
- **Observed**: recorded by a runner from the venue (quotes, bars, heartbeats, the MT5 demo-account check).
- **Simulated**: shadow-book results; no order was placed.
- **Derived**: computed by the dashboard (EMA 20/50, swing structure, aggregated candles).
- **Unavailable**: not recorded. It is shown as such and never filled in.

**Pages:**
- **Overview:**
  - per market: runner state, connection, open or closed, data freshness, new-entry guard, last observed quote
    (bid/ask and age), last market-data update, last heartbeat, window uptime, connection failures or websocket
    reconnects, pre-registration integrity and forward bars recorded;
  - the MT5 demo account (observed, never traded);
  - strategy summaries;
  - risk limits per market: 0.5% / 1.5% / 1% from the runner configs, open risk now, accounts halted today,
    KILL/STOP/breaker;
  - simulated accounts (each book is an alternative cost or equity scenario of the same signals, so they are never
    summed);
  - alerts, interruptions and heartbeat gaps.
- **Charts:**
  - XAUUSD and BTC/USDT at 4H, 1H, 15M, 5M and 1M;
  - candles from the recorded CSV history plus the runner's shadow store after the CSV ends, with no overlap, and
    the seam bucket combined;
  - larger timeframes are aggregated from recorded smaller bars and labelled as such;
  - a timeframe with no data source is disabled;
  - when the newest recorded data at a timeframe is old, the chart says so. For example, BTC 1m/5m end on
    2026-05-31 and XAU 1m/5m end on 2026-11-06; live bars exist only at the runners' intervals (XAU 15m,
    BTC 1h).
  - Layers: volume (tick volume for XAU), EMA 20/50, strategy signals (candidates coloured; random controls and
    warm-up toggleable), simulated entries/exits with R, SL/TP segments, ORB opening ranges, swing structure.
  - The opening range is computed from the strategy's own session definition (08:20 New York, DST-aware) and the
    signal's recorded range.
  - Historical inspection: pick any recorded signal to load that period, centre it, and draw its entry, stop and
    target. The inspector shows each book's risk decision and reasons, and any simulated trades.
- **Strategies:** three independent panels (NY ORB, BTC Donchian, Selective Strategy A) showing:
  - lifecycle (`prepared` / `running` / `paused` / `blocked` / `stopped`);
  - signals (forward vs warm-up), simulated trades, wins and losses, net P&L after estimated costs (spread,
    slippage, commission, swap), today and cumulative, win rate, profit factor, max drawdown;
  - approved/rejected decisions, rejection categories and each rejection's reasons, last activity, per-book table.
  - The headline book is the largest equity at the highest cost scenario.
- **Grok Decision Room:**
  - the activation gate (config flag, env flag, key presence only; the key is never read or displayed);
  - model, cost controls, review trigger and the six role briefs;
  - recorded reviews and audit-chain status when a store exists. Today none exists: "not activated".

**Strategy A** shows `prepared` because no runner state exists. It can only show `running` once its own state
directory exists and its heartbeats are fresh (< 3 min). `stopped` follows after 5 minutes without one.

## 3. Verification (2026-11-09)
- **Tests:** `tests/test_command_center.py` has 15 tests on synthetic stores and CSVs, covering:
  - CSV bisection;
  - the history/live seam without double counting;
  - aggregation;
  - no fabricated candles (1m with no source returns none, with a note);
  - read-only enforcement (write methods refused, SQL writes rejected, files byte-identical);
  - Strategy A `prepared`, and each lifecycle state;
  - metrics, rejections and markers from recorded simulation;
  - opening-range times in EST and EDT;
  - Grok room key secrecy;
  - the API is GET-only (405 on POST/PUT/DELETE/PATCH).

  Full suite: see PROGRESS.
- **Live smoke test** against the running runners' stores, read-only: every endpoint returns 200 in ≤ 0.17 s, and
  POST returns 405. The server listens on `127.0.0.1:8765` only, with a 54 MB working set.
- **Visual check** with headless Edge screenshots of all four pages at 1600 px and 600 px.
- **Data observation:** the XAU 15m bar at 2026-11-05 23:00 UTC (the daily reopen) has a recorded low of 3914.40,
  about 170 below its open. This is a broker reopen spike in the recorded MT5 history. It is displayed as recorded;
  the dashboard never edits data.

## 4. History, analytics and signal records (V1.1)
| Need | Where |
| --- | --- |
| Historical strategy signals | History → Signals: search by id or strategy; filter by strategy, status (forward, warm-up and others) and UTC dates; sortable; paginated (100 per page) |
| Risk-engine decisions and rejection reasons | History → Risk decisions (approved or rejected, size, planned risk, full reasons); Strategies → rejected opportunities; Signal record |
| Simulated trades and equity changes | History → Simulated trades; History → Equity & drawdown (per book, derived drawdown, selectable books) |
| Trading costs and drawdowns | Trades table: gross, costs, net, R, MFE/MAE. Signal record: spread/slippage, commission and swap per book. Equity panel: max drawdown |
| System interruptions and alerts | History → Interruptions & alerts. Every runner event is classed as interruption, alert or routine and is searchable. Overview: window summary |
| Grok advisory responses | History → Grok advisory (per-role outputs, validation errors, audit-chain status). Signal record: review of that signal. Today: none (not activated) |
| Inspect one signal | Click any row (History, or Charts → Signal history → Full detail). The record shows the market context chart (recorded candles, opening range, SL/TP, simulated execution), strategy context and provenance, every book's decision, execution with costs, and the Grok review |

## 5. Security and remote-access design
**Local (V1, active):**
- Bind `127.0.0.1:8765`. A non-loopback bind is refused unless `KRONOS_CC_TOKEN` is set and
  `--allow-remote-bind` is given.
- GET-only; any other method returns 405 before routing.
- Host allow-list (`127.0.0.1`, `localhost`) against DNS rebinding.
- Headers: CSP (`default-src 'self'`; `connect-src 'self'`; `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and `Cache-Control: no-store` on
  the API.
- Errors return a generic 503 (no tracebacks). Every response passes `services.scrub`: configured state, checkout,
  history and advisory paths and the home directory are replaced by labels.
- No credential is ever read. The Grok room reports only whether `XAI_API_KEY` is present. Broker credentials are
  not used by the dashboard.

**Future VPS deployment (prepared, not deployed):**
1. The app stays on `127.0.0.1`. An HTTPS reverse proxy (for example Caddy or nginx) terminates TLS (Let's
   Encrypt) and authenticates users (OIDC or SSO, or at least strong basic auth with rate limiting), ideally
   restricted further by VPN or IP allow-list.
2. Set `KRONOS_CC_TOKEN` (a long random secret) for the app. The proxy injects `Authorization: Bearer <token>`
   after authenticating, so a local process that bypasses the proxy is still refused.
3. Add the public host name to `security.allowed_hosts`. Keep `/api/openapi.json` disabled (automatic when a
   token is set).
4. The shadow stores must be readable by the app's user, ideally through a read-only replica or the backup copies
   (`research/state_backup.py`). Never give it write access.
5. Approval is required before any of this is deployed.

## 6. Local operation (launcher)
| File | What it does |
| --- | --- |
| `Kronos Command Center.cmd` | Starts the dashboard in the background if it is not already running (checked by `/api/health` identity), waits until it answers, then opens the browser. Errors are shown with the log location (`%LOCALAPPDATA%\KronosCommandCenter\logs`), and the window pauses |
| `Stop Kronos Command Center.cmd` | Graceful stop: writes `stop.request` in `%LOCALAPPDATA%\KronosCommandCenter`, and the server finishes requests and exits. Fallback after 10 s: ends only the PID the dashboard reported, after verifying its command line is `-m command_center` (runner command lines are refused) |
| `ops\windows\command_center.ps1 -Action status\|open\|restart\|setup\|rebuild` | Status, reopen, restart, first-time setup of `.venv-cc` (and the build if missing), rebuild of the web interface |
| `ops\windows\command_center_startup_task.ps1` | **Optional** start-at-logon task. Dry run by default; `-Apply` registers it. **Not registered** |

**Duplicate prevention:**
- a launcher mutex;
- the health-identity check;
- the server refuses a busy port (exit 3).

The launcher never starts, stops or modifies a trading runner, a runner task or a STOP/KILL file.

## 7. Usability and reliability verification (2026-11-09)
| Requirement | Result |
| --- | --- |
| Responsive layouts | Grids use `minmax(0,1fr)` columns; tables scroll inside their panel; navigation scrolls horizontally on narrow screens. Screenshots at 1600 px and 600 px (headless Chromium cannot render narrower) |
| Observed vs simulated | Every figure is tagged observed / simulated / derived / unavailable. Signal prices are *derived* (strategy outputs); book results are *simulated*; quotes, bars and heartbeats are *observed* |
| Timestamps and freshness | All records are UTC. A UTC/local toggle (stored in the browser) also re-labels chart axes. Quote age, last data update, heartbeat age, and data "fresh"/"stale" come from the runners' own flags |
| Loading, empty, error states | Skeletons while loading. Empty states say why ("runner not started", "no forward signals yet", "none are invented"). An API failure banner shows the time of the last good data and keeps it |
| Inactive vs running | Lifecycle badges: prepared / running / paused / blocked / stopped / unavailable. Strategy A shows **prepared** |
| Grok not activated | Banner and badge "not activated"; gate panel; every agent card "idle"; history and signal records state "not activated" |
| No invented data | Candles only from recorded files and stores (aggregation labelled); no candle, signal or trade is synthesised (tests) |
| Hidden or reopened tab | Polling pauses while the tab is hidden and refreshes immediately when it becomes visible (`visibilitychange`) |
| Disconnected runners, inaccessible databases | A stale heartbeat shows "heartbeat delayed" (3 min) or "not responding" (5 min); a strategy shows "stopped". An unreadable database shows "data unavailable" for that runner only (test) |
| XAUUSD spike | Broker data (MetaQuotes-Demo daily-reopen print at 3914.40 on 2–5 Nov, present in ticks and all bar timeframes). Not a chart-processing issue. Flagged as "off-market print"; source data unchanged |

## 8. Targeted improvements after review (V1.2, 2026-11-09)
Everything below is read-only and computed from recorded data or the frozen rules. Nothing predicts a trade.

**Live Decision Status** (Overview, `command_center/decisions.py`). One card per strategy says why it is or is not
trading right now:

| State | Meaning |
| --- | --- |
| Waiting for its session | Before the opening range, after the entry window, market closed, or no setup is possible today (range outside the volatility filter, or today's one signal already generated) |
| Monitoring | Building today's opening range |
| Waiting for a valid signal | Entry window open (ORB), or every hourly close (Donchian), with all guards passing |
| Blocked by a deterministic guard | STOP/KILL/breaker, or a reason in the runner's latest heartbeat (`entry_block_reasons`, `guard`) |
| Simulated position open, runner not responding, not launched, data unavailable | As named |

Inputs:
- **Observed:** the runner's latest heartbeat, the control files, recorded signals, and the event schedule in the
  runner's `calendar.sqlite`. Event blackouts use the risk engine's fixed `DEFAULT_RULES`.
- **Derived session window:** computed from the pre-registered parameters (08:20 New York, a 30 min range, a
  180 min entry window) and the calendar the runner itself builds: NY gold hours plus CME gold holidays and early
  closes from `pandas_market_calendars` 5.5.0, the same pinned versions. The runner additionally intersects it with
  the broker's sessions, and its heartbeat reports `broker_verified`.
- **Derived setup:**
  - The frozen strategy class is replayed on the bars the runner recorded:
    - ORB: the opening range, its width relative to ATR against the filter, and the long and short trigger levels.
    - Donchian: the 96-bar channel high the next close must exceed.
  - The strategy code is identical to the runners' commits: `git diff f224754` / `f129e55` shows no change to these
    classes.
  - The replay never records anything.

**Alerts:** they are now split into four groups.
- **Active now:** control files, breaker trips, daily halts, runner down, connection lost, stale data while the
  market is open, integrity problems, and calendar refresh or reconciliation failures that have not resolved.
- **Temporary connection issues:** interruption, error and reconnect counts in the window, with "recovered" when
  the runner is connected now.
- **Resolved warnings:** heartbeat gaps that ended, with the runner's own evidence. Also failures followed by a
  success, housekeeping errors and clock offsets.
- **Past STOP/KILL events:** 7 days; the file is no longer present.

**Warm-up vs forward:** a `forward` / `warm-up - not evidence` tag appears in:
- the decision cards and the activity feed;
- History (a Phase column, plus "forward only" and "warm-up only" filters);
- the Strategies page (forward counts; warm-up shown as excluded);
- Charts (warm-up markers are grey and labelled; forward markers are larger and labelled FORWARD);
- the signal record.

**Overview layout:**
- Decision status comes first.
- Then recent activity (forward signals, decisions, simulated trades, runner events) and safety alerts.
- Then markets, simulated accounts and risk limits.

**Equity and drawdown:**
- One simulated book at a time. Books are alternative equity and cost simulations of the same signals and are never
  summed.
- Curve = initial equity + recorded closed-trade P&L, plus the equity marks recorded while a position was open
  (step line).
- Drawdown is derived from that curve.
- Empty books say so; nothing is drawn.

**Resources:**
- BLAS is limited to 1 thread.
- Measured on this machine: 79 MB private memory and 7 threads, compared with 432 MB before that limit.
- The holiday calendar is built once in a background thread and cached for 6 h.
- The overview takes about 0.3 s when cached.
- Refresh intervals are unchanged: Overview 15 s, Strategies 20 s, History 30-60 s. Polling pauses in hidden tabs.

**Price spikes and missing data:** the broker off-market-print flag and the lower-timeframe notes are unchanged.
Recorded history is never edited.

### 8.1 BTC heartbeat gaps over 3 min (investigation, 2026-11-09)
The runner's store recorded three gaps:

| Gap (UTC) | Evidence | Cause |
| --- | --- | --- |
| 08 Nov 22:59:09 -> 23:03:27 (4.3 min) | The next heartbeat recorded the guard `market data stale (0:03:23.98)`: quotes had not been processed since about 23:00:03. `kronos_forecasts.jsonl` starts at exactly 23:00, the first bar inside the forward window (`bar.close_time >= unseen - 1h`). | **Supported:** the first live Kronos forecast loads the model lazily inside the polling loop (`LiveKronos._get`), and the heartbeat is written by that same loop (`housekeeping` after `poll`). The 00:00-05:00 forecasts caused no gap. |
| 09 Nov 01:25:37 -> 01:28:54 (3.3 min) | Next heartbeat normal (`ok`, data fresh). No event recorded. The WebSocket thread stayed connected (reconnects unchanged at 3, message count rising). | **Not determined.** |
| 09 Nov 04:18:49 -> 04:24:38 (5.8 min) | Same as above. | **Not determined.** |

Findings common to all three gaps:
- The machine was not asleep: the gold runner, whose heartbeat runs on an independent thread, wrote one every minute
  throughout.
- No restart was recorded.
- Possible but unproven cause: a slow Binance REST call in `housekeeping`. Each call can take up to 3 x 20 s timeouts
  plus back-off, `ClockSync.measure` makes 3 calls, and a call that succeeds after retries leaves no record.
- Gaps 2 and 3 did not coincide with Windows Defender's hourly health reports (gap 3 started 9 s before one; gap 2
  did not overlap one).

Recommendation (requires approval; the frozen runner was not modified):
- move the BTC heartbeat to an independent thread, as was done for XAUUSD in `2436d78`;
- log slow REST calls;
- pre-load the Kronos model at start-up.

The dashboard now shows each gap under "Resolved warnings" with the runner's own evidence.

## 9. Not in V1 (future versions)
- Controls (STOP/KILL buttons, runner start/stop, agent activation). These need a separate authenticated,
  audited design and approval.
- Remote HTTPS access (design in section 5), multi-user accounts, push alerts (e-mail or Telegram) and SSE
  streaming.
- Grok reviews appear once activation is authorised.
- Equity curves become informative after forward trades exist (per-book view ready).
- Live 1m/5m candles: the runners record only their own intervals (XAU 15m, BTC 1h; Strategy A M5 once
  launched).
- Light theme and additional languages. The UI is English, with ISO-style 24 h timestamps and UTC or local time.

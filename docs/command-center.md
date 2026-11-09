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

## 8. Not in V1 (future versions)
- Controls (STOP/KILL buttons, runner start/stop, agent activation). These need a separate authenticated,
  audited design and approval.
- Remote HTTPS access (design in section 5), multi-user accounts, push alerts (e-mail or Telegram) and SSE
  streaming.
- Grok reviews appear once activation is authorised.
- Equity curves become informative after forward trades exist.
- Live 1m/5m candles: the runners record only their own intervals (XAU 15m, BTC 1h; Strategy A M5 once
  launched).
- Light theme and additional languages. The UI is English, with ISO-style 24 h timestamps and UTC or local time.

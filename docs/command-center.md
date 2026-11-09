# Kronos Trading Command Center V1 (Phase 5)

A working, read-only web dashboard for the shadow forward tests. Branch `feat/command-center-v1` (worktree
`C:\Users\Admin\Projects\kronos-command-center`), stacked on PR #23.

**Start:** `.\ops\windows\command_center.ps1`, then open <http://127.0.0.1:8765>. Use `-Setup` the first time and
`-Rebuild` after changing the frontend.

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

## 4. Not in V1
- No controls: no buttons create STOP/KILL, start runners, or activate agents. Any control would need a separate,
  authenticated, audited design and approval.
- No push updates (SSE/WebSocket) and no authentication (localhost only).
- Section 6 of the Phase 5 brief ("Grok Agent Decision Room") was truncated after "Prepare". The room is built as
  a read-only view of the existing advisory configuration and store; further requirements are pending.

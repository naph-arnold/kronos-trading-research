# Phase 2E — BTC/USDT continuous shadow trading (research, methodology, operations)

Status 2026-11-08. **Simulation only: no real or testnet orders exist in this pipeline, and none can be sent.**
No Binance credentials are read or sent. The forward window starts 2026-11-09T00:00Z (pre-registered). No forward
results exist yet.

## Market data (public, read-only)
| Need | Source | Handling |
| --- | --- | --- |
| Closed 1h candles | REST `data-api.binance.vision/api/v3/klines` | Fetched a few seconds after each hour closes. The forming candle is dropped. A missing hour is recorded as `kline_gap` (Binance had real outages; nothing is interpolated). Stale candles (> 1 h + 5 min) block entries |
| Best bid/ask | WebSocket `data-stream.binance.vision` `btcusdt@bookTicker` | Receive-only. Automatic reconnect with capped back-off. Per-second compression keeps first/last/min/max bid and ask. Falls back to REST `bookTicker` when the stream is down or silent (> 10 s) |
| Exchange filters/status | REST `exchangeInfo` | Tick 0.01, step 0.00001, min notional 5 USDT, status `TRADING`, refreshed every 5 min (status) / 12 h (spec); a non-trading status blocks entries |
| UTC time | REST `/api/v3/time` | Offset measured every 10 min (fastest of 3 samples; slow samples are ignored). Quotes are stamped with corrected time. Offset > 2 s blocks entries. Measured: local clock about 0.95 s ahead |
| Persistent storage | `state/phase2e_binance_shadow/shadow.sqlite` | Bars, signals, every decision, positions, trades, equity, events and heartbeats. Kronos forecasts are kept in `kronos_forecasts.jsonl` |

## Shadow engine and risk
- **Engine:** the Phase 2D engine (`trading/shadow/engine.py`), generalized with `EngineSettings.market`/`interval`. XAUUSD defaults are unchanged and all 415 earlier tests pass unmodified.
- **Risk:** every entry goes through the unchanged `trading.risk.evaluate`:
  - 0.5% intended risk with size rounded down to the 0.00001 BTC step
  - 1% aggregate open risk, one position per book
  - 1.5% daily-loss halt on UTC days
  - 100 USDT maximum spread, 0.5% maximum price drift
  - spot is long/flat only
- **Fills:** first best bid/ask at or after the signal candle's close. Entry at the ask + 10 USDT slippage. Stop at the bid − 10 USDT. Target at the limit price. Time exit at the last quote of the Nth candle. Fees are charged per side on notional.
- **Fee scenarios:**
  - 0.1%: Binance's published base-tier spot taker fee, not verified for any account. Used for the criteria.
  - 0.2%: stress scenario.
- **Integrity:** same rules as Phase 2D. Entries need a fill quote processed within 30 s ("system not live" otherwise). Candles before 2026-11-09 are warm-up only. Restarts must reproduce every recorded signal or the breaker trips. Duplicates are prevented by UNIQUE constraints, and reconciliation runs hourly.
- **Guards** (no Grok): FOMC, BEA, ECB and CPI/NFP blackouts (15 min before / 30 min after US high-impact releases), market health (stale or gapped quotes, abnormal spread, venue status), stale candles and clock offset.
- **Emergency controls:** `state/phase2e_binance_shadow/KILL` rejects new entries while open simulated positions keep their stops; `STOP` exits gracefully; breaker trips persist until `reset-breaker --operator NAME`.
- **Separation from XAUUSD:** separate process, worktree, state directory, SQLite files, breaker (market-checked, so a foreign state file fails closed), daily ledger, calendar store, kill/stop files and reports.

## Research (`research/phase2e_btcusdt_research.py`)
- **Data:**
  - 2019-01-01 → 2026-05-24 20:00 UTC: 64,760 hourly candles.
  - 2019-01 → 2023-10 is newly downloaded and was never examined before in this project. It contains 20 recorded exchange-outage gaps.
  - The Phase 2B holdout (2026-05-24 onward) was already examined, so it is excluded entirely.
- **Method:**
  - 41 rolling walk-forward windows: 6-month training, then 2-month test.
  - Parameters are selected on each training window and evaluated once on the next test window.
  - Costs as in Phase 2B: 0.1% fee, 1 bp spread, 10 USDT slippage.
  - Kronos keeps its KR-05 parameters (never tuned). 64,632 leakage-safe Kronos-mini forecasts were generated for the whole span.
  - Random controls: 20 long-only seeds per candidate, matching that candidate's exit rules and out-of-sample trade frequency.
  - Event blackouts were not applied in research (historical schedules are incomplete); they are applied live.

Out-of-sample results, 2019-07-03 → 2026-05-03:

| Candidate | Trades | Mean R gross | Mean R net (95% CI) | Beats matched random | Walk-forward compounded | Profitable windows | Configurations profitable | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Donchian breakout | 593 | +0.123 | −0.020 (−0.124, +0.091) | 18/20 | −6.9% | 19/41 | 3/12 | **Forward test (candidate)** |
| EMA trend (Phase 2B rule) | 208 | +0.072 | −0.084 (−0.273, +0.112) | 7/20 | −8.9% | 18/41 | 5/24 | Shadow as trend control |
| VWAP mean reversion | 336 | −0.056 | **−0.230 (−0.327, −0.132)** | 0/20 | −32.3% | 12/41 | 0/16 | **Rejected** (not forward-tested) |
| Kronos forecast (KR-05) | 7,159 | +0.012 | **−0.167 (−0.175, −0.158)** | 20/20 | n/a (fixed) | n/a | fixed | Monitored model, not a candidate |

Context: buy-and-hold over the same span, unsized, returned +590% with a 77% drawdown, so long-only gross profits
mostly reflect market direction.

Kronos earns about zero before costs. Its 1-hour holds pay fees on every candle, so it is reliably negative after
costs. It beats random entries with the same 1-hour exit because those random entries also pay fees every hour.
Donchian results alternate by year (2022, 2025 and 2026 were losers) and the evidence is weak. That's why it is
forward-tested and not promoted.

**Frozen parameters** (`research/forward_test/phase2e_btcusdt_preregistration.json`):
- **Donchian:** lookback 96 h, stop 2.5 ATR, target 3R, 72-hour time exit (`1fe243096a627b34`).
- **EMA trend:** EMA 50/200, 2.5 ATR stop, 3R target (`96ba7f762bf1d783`).
- **Kronos:** 20 bps edge, EMA200 filter, 1.5 ATR stop, 1-hour exit (`ccb1ff62566a9aeb`).
- **Random controls:** seeds 1–3 each.
  - Donchian control: p = 0.00991 per hour, 2.5 ATR, 3R, 72 h.
  - Kronos control: p = 0.1196, 1.5 ATR, 1-hour exit.
- **Books:** 9 strategies × fee 0.1%/0.2% at 10,000 USDT = 18.

**Success criteria** (all required, on the 0.1% fee scenario):
1. Net mean R > 0.
2. Bootstrap 95% CI lower bound > −0.05.
3. Mean R above the median of the candidate's own random controls.
4. Positive net result in ≥ 2 of the latest 3 complete two-month blocks.
5. Max drawdown < 6%.

The final checkpoint needs ≥ 100 trades **and** ≥ 2027-05-09. Donchian traded about 85 times a year out of sample,
so expect roughly 14 months. There is no re-tuning on forward data.

## Future Spot Testnet execution (Phase 3, not authorized)
`trading/execution_prep/binance_testnet.py` contains tested building blocks only. There is no HTTP code, and
`submit()` always raises `ExecutionNotAuthorized`.
- **Order intents:** built from approved decisions only, re-validated against LOT_SIZE, NOTIONAL and the price
  tick. The deterministic `newClientOrderId` comes from the signal's idempotency key, so duplicate orders are
  refused by the exchange and visible on reconciliation.
- **Reconciliation:** compares expected positions with the testnet's balances.
- **Reset detection:** catches balance changes not explained by the ledger and `updateTime` moving backwards.
  A reset must be recorded as a cash flow, never as P&L.

Endpoints remain restricted to `testnet.binance.vision` (`verify_binance_endpoint`), and production key variables
abort start-up (`load_demo_environment`).

## Grok / Kronos
The six-agent architecture is unchanged and no agent is active. All six agents (Scout, News, Chart, Kronos
Analyst, Skeptic, Coordinator) remain on the official xAI Grok API. Kronos remains the only forecaster. Live
Kronos forecasts are recorded for later evaluation. No AI output reaches the risk engine.

## Operations (Windows)
```powershell
# from C:\Users\Admin\Projects\kronos-binance-shadow (branch feat/phase2e-binance-shadow)
$env:EXECUTION_MODE = "demo"
..\kronos-trading-research\.venv311\Scripts\python -m research.phase2e_binance_shadow status
..\kronos-trading-research\.venv311\Scripts\python -m research.phase2e_binance_shadow report
New-Item state\phase2e_binance_shadow\KILL   # emergency: reject all new simulated entries
New-Item state\phase2e_binance_shadow\STOP   # graceful exit of the runner and its supervisor
```
- **Supervision:** `research/forward_test/run_binance_shadow.ps1` restarts the runner after any exit and holds a
  process-scoped keep-awake.
- **Heartbeats:** written every minute *even when data is unavailable*, with the status (`ok` /
  `entries_blocked`), the blocking reason, stream health and data status.
- **Worktree:** the runner executes from the worktree `C:\Users\Admin\Projects\kronos-binance-shadow`. Don't
  remove that worktree while it runs.

## Known limitations
- Best bid/ask is not a full order book: there is no depth or queue modelling, and slippage is a fixed allowance.
- Per-second compression keeps price extremes, but the ordering of extremes within one second is approximate.
- Fees are assumptions until a testnet or real account fee is verified.
- Research used historical candles without event blackouts. Live trading applies them, so live trade counts may
  be slightly lower.
- The CPI/NFP schedule file is static and needs a monthly re-check (shared with Phase 2D).

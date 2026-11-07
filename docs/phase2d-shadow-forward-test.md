# Phase 2D — XAUUSD shadow forward test (methodology and operations)

Status 2026-11-07. **Simulation only: no MT5 orders (real or demo) are sent, and none can be.** The forward window
starts 2026-11-09T00:00Z (pre-registered). No forward results exist yet; nothing in this document is a result.

## What runs
`research/phase2d_shadow.py run` (engine in `trading/shadow/`) polls the MetaQuotes-Demo terminal through the
read-only connector every 5 s: closed XAUUSD M15 bars (`copy_rates_range`) and bid/ask ticks (`copy_ticks_range`).
Seven strategies run side by side on the same data:

| Key | Role | Parameters |
| --- | --- | --- |
| `orb_newyork` | candidate | frozen pre-registration (`params_hash c00dae9ee89b0a65`), 16-bar time exit |
| `h1_ema_cross` | trend control | H1 EMA 50/200 cross, 3 ATR stop, 2R (`e8257efa97a2b2a3`) |
| `random_s1`…`random_s5` | matched random-entry control | family `random_1.0atr_1.5r_time16_long51`: p=0.004/bar, 1 ATR stop, 1.5R, 16-bar exit, 51% long (the candidate's exit structure and long share), seeds 1–5 |

Every strategy feeds four hypothetical **books** (28 in total): equity 10,000 USD (primary) and 3,000 USD (minimum-lot
study) × commission 0 and 7 USD per lot. Each book is an independent account with its own equity, daily-loss ledger,
duplicate claims and position limit. The real demo account (3,000 USD) is never used for sizing and is checked
every 10 minutes to confirm it is unchanged (balance, positions, orders).

## Integrity rules
- **Unseen data only.** Bars closing before 2026-11-09T00:00Z (history from 2026-08-03) only warm up indicators;
  their signals are stored as `warmup` and never evaluated. Bars and decisions after that are the forward test.
- **No lookahead.** Strategies see closed bars only (`HistoryView`). A signal on the bar closing at C fills at the
  first tick stamped ≥ C (the next bar's open); ticks are processed strictly after the bar-close event.
- **Live-only entries.** An entry is simulated only if its fill tick was processed within 30 s of real time. After a
  restart or outage the catch-up still manages open simulated positions (broker-side stops would have worked), but
  every entry it would have needed is recorded as rejected, `System not live at decision time`.
- **Frozen parameters.** The config must match the pre-registration file (line-ending-normalized SHA-256
  `6f743af6…`), the candidate/control parameter hashes and the 0.5% risk fraction, or the runner refuses to start.
  The state database records the pre-registration hash at creation and refuses a different one later. Book
  definitions (equity, commission) are frozen in the database.
- **Deterministic restarts.** Strategy state is rebuilt by replaying the stored bars; the regenerated signals must
  equal the recorded ones or the breaker trips. Verified on real data: 6,166 bars, 188 signals, 0 mismatches.

## Simulated execution and costs
| Item | Model | Status |
| --- | --- | --- |
| Entry | live ask (long) / bid (short) of the fill tick + 0.10 USD slippage | spread **verified live** |
| Stop | first tick with bid ≤ stop (long) / ask ≥ stop (short); fills at that tick − / + 0.10; checked before the target | `stop_overshoot` and losses beyond planned risk are reported |
| Take-profit | first tick with bid ≥ target (long) / ask ≤ target (short); fills at the target (no positive slippage) | |
| Time exit | at the close of the 16th bar, last tick of that bar − / + slippage | ORB and random controls |
| Bar without ticks | replay rules (stop-first, bar high/low, half-spread 0.085) | recorded as `bar_without_ticks` |
| Commission | 0 and 7 USD **per 1.00 lot per round trip** (3.50 per side), charged on close | **unverified** (see below) |
| Swap | `symbol_info` swap in points per lot per night (long −12.6, short −4.6, ×3 Wednesday), per server midnight crossed | **verified live** 2026-11-07 |

The pre-registration says "fills: next bar open after the signal bar close (same as replay); stop-first intrabar"
and "spread from live ticks at the simulated fill time". With live ticks, the next bar's open is its first tick,
and stop-before-target ordering is applied tick by tick (and in the no-tick fallback).

## Risk controls (unchanged)
Each entry goes through the unchanged `trading.risk.evaluate`: 0.5% intended risk per trade, round-down sizing,
minimum lot rejected (never rounded up), 1% aggregate open stop risk, one position per book, 1.5% daily-loss halt
latched for the broker risk day (`DailyLossLedger`), spread ≤ 1.00 USD, signal ≤ 5 min and quote ≤ 10 s old,
price drift ≤ 0.2%, margin ≤ 50% of free margin, demo environment only. The engine also applies the pre-trade
gate's preconditions: broker-verified session calendar, specification ≤ 1 day old, mandatory calendar/market
guard, durable duplicate claims. `PreTradeGate` itself is not used because it requires a fully verified
specification and the commission is unverified. Shadow mode records each commission scenario as an assumption
(`commission_verified: false` on every decision).

Emergency controls: `state/phase2d_shadow/KILL` (or any breaker trip) rejects every new simulated entry, while
open simulated positions keep their stops. Breaker trips persist across restarts and need
`reset-breaker --operator NAME`. Reconciliation runs at start-up and hourly. It checks that books, trades,
positions and approved decisions agree, and any mismatch trips the breaker.

## Calendar and market restrictions (no Grok dependency)
- **Sessions:** New York gold market hours + CME holidays/early closes (`pandas_market_calendars`) intersected with
  the broker's observed sessions; promoted to `broker_verified` only after a fresh-tick server-offset check. Until
  then (e.g. weekends) every entry is rejected; verification is retried every 10 minutes.
- **High-impact releases:** official FOMC, BEA (GDP, PCE) and ECB calendars fetched every 12 h, plus the US CPI and
  Employment Situation schedule in `research/forward_test/calendar/us_cpi_nfp_bls_2026_2027.json` (28 releases,
  Nov 2026 – Dec 2027, 08:30 ET). Sources: BLS release schedule pages (cpi.htm, empsit.htm, 2026 monthly lists),
  cross-checked against OMB/OIRA "Schedule of Release Dates for Principal Federal Economic Indicators for 2027" (all
  2027 dates agree). Blackouts: 30 min before/after US high-impact data, 60/90 min around central-bank decisions.
  Missing, stale or contradictory calendar data fails closed. The runner also fails closed 7 days before the
  CPI/NFP file coverage ends.
- **Market health:** stale quotes (> 15 s), price-update gaps (> 60 s), abnormal spreads (> 4× rolling median),
  venue trade mode not `full`, and an unknown venue status all reject entries.

## Commission verification (item 7)
- MT5 exposes no commission schedule. The Python package (5.0.6231) has no `symbol_info_commissions` equivalent.
- The account's own deal history (`history_deals_get`, read-only, added to the allowlist) contains **1 deal (the
  balance deposit) and no XAUUSD deals** (checked 2026-11-07), so commission cannot be verified from records.
- MetaQuotes-Demo is MetaQuotes' platform test server, not a broker with a published fee schedule. No official
  document was found.
- Status: **unverified**. Both scenarios are reported separately. Verification needs broker documentation or a
  separately authorized demo trade; no trade was placed. `python -m research.phase2d_shadow commission-evidence`
  re-checks the account history at any time.

## Reports
`python -m research.phase2d_shadow report` writes `research/output/phase2d_shadow_report.{json,md}` with:
- Per book: trades, net return, max drawdown (bar-close marks), win rate, profit factor, mean R with bootstrap CI,
  and costs split into spread+slippage, commission and swap.
- Rejected signals by reason, and risk-limit exceptions (planned risk above budget or above 0.5%, losses beyond
  planned risk).
- Strategy comparison, uptime from heartbeats, and data interruptions.
- The pre-registered success criteria, evaluated as written on the 10,000 USD candidate book under the 7 USD
  commission scenario.

No verdict is issued before the final checkpoint (≥ 100 trades and ≥ 2027-05-09). Until then each criterion
reports `insufficient_data` or its interim value. "Two-month blocks" are anchored at the unseen-data start, and
the latest three complete blocks are used.

## Operating instructions (Windows, MetaQuotes-Demo terminal logged in)
```powershell
$env:EXECUTION_MODE = "demo"                                   # required; live flags/production keys abort
.venv311\Scripts\python -m research.phase2d_shadow run         # continuous loop (Ctrl+C or STOP file to end)
.venv311\Scripts\python -m research.phase2d_shadow run --once  # start-up + one poll (smoke test)
.venv311\Scripts\python -m research.phase2d_shadow status
.venv311\Scripts\python -m research.phase2d_shadow report
.venv311\Scripts\python -m research.phase2d_shadow reset-breaker --operator "<name>"
New-Item state\phase2d_shadow\KILL   # emergency: reject all new simulated entries
New-Item state\phase2d_shadow\STOP   # graceful exit
```
For unattended operation, `research/forward_test/run_shadow.ps1` restarts the runner after any exit and appends to
`state/phase2d_shadow/runner.log`. Registering it as a Windows scheduled task (at log-on) is optional and must be
done by the operator. The terminal must stay logged in to the demo account, and the PC must not sleep during
market hours. Downtime is visible in the report: heartbeat gaps, and entries rejected as "system not live".

## Known limitations
- The simulation has no queue position, partial fills or requotes. MT5 demo ticks are broker quotes, not exchange
  prints.
- Drawdown uses equity marked at bar closes plus trade closes, so intrabar extremes are not captured.
- The CPI/NFP file is static. A BLS schedule change (e.g. a government shutdown) is **not detected automatically**,
  so the operator must re-verify it monthly against BLS and rebuild with `build-calendar`.
- Commission remains an assumption (see above), and MetaQuotes-Demo costs may differ from a production broker.
- One runner process per state directory (single SQLite writer).

# Shared foundations and deterministic risk engine (Phase 1)

Package: `trading/`. Decision-only: nothing in this package places, modifies or cancels orders.

| Module | Purpose |
| --- | --- |
| `common.py` | `Market`, `Side`, strict `Decimal` parsing (`to_decimal`), UTC enforcement, round-down `floor_to_step`. |
| `config.py` | Demo-only guard. `ExecutionMode` has only `DEMO`. `load_demo_environment` requires `EXECUTION_MODE=demo`, rejects `LIVE_TRADING_ENABLED!=false`, production credential variable names and any Binance host other than `testnet.binance.vision`. `verify_mt5_demo_account` requires `account_info().trade_mode == 0` (demo). |
| `marketdata.py` | Canonical UTC `Bar` (open-time keyed), `Quote`, series validation (duplicates, ordering, alignment, gaps — `allow_gaps` for XAUUSD sessions), feed freshness, adapter from existing `research/` CSV rows. |
| `signals.py` | `TradeSignal` schema v1 (strategy id/version, provenance, mandatory stop, lookahead check), `dedup_key` (same strategy/version/side/bar ⇒ same signal), `idempotency_key`. |
| `instruments.py` | `Mt5InstrumentSpec` (tick size, `SYMBOL_TRADE_TICK_VALUE_LOSS`, lot min/max/step, stops level, margin per lot, commission) and `BinanceSpotSpec` (PRICE_FILTER, LOT_SIZE, min notional, taker fee). Values must come from the venue. |
| `state.py` | Per-account `AccountSnapshot` / `OpenPosition` (demo flag, reconciled flag, day-start equity, open stop risk, pending orders). |
| `clock.py` | MT5 server time → UTC. `ServerTimeZone` (fixed or NY-close UTC+2/+3 following US DST), conversion of MT5 `time`/`time_msc`, rejection of ambiguous/non-existent DST-switch times, `verify_server_offset` against a fresh tick. |
| `sessions.py` | `TradingCalendar` in server time with UTC closures (holidays/early closes), open/close buffers, `XAUUSD_NY_CLOSE_TEMPLATE` (Mon–Fri 01:00–24:00 server = Sun 18:00–Fri 17:00 New York with the daily 17:00–18:00 NY break), `validate_session_bars` (every bar inside a session; every gap explained by closed time). |
| `daily.py` | Persisted `DailyLossLedger` per market/account (definitions below). |
| `risk.py` | `RiskLimits` and `evaluate()` — never raises; returns approval with size or every rejection reason. XAUUSD requires a session calendar. |
| `ledger.py` | SQLite duplicate ledger; atomic claims by dedup key and signal ID; survives restarts. |
| `breaker.py` | Per-market circuit breaker persisted to JSON (atomic write; trips survive restarts; corrupt/foreign state ⇒ tripped); manual reset recorded with operator identity; optional kill file. |
| `audit.py` | Hash-chained JSONL audit (reuses `research.six_agent_desk.append_audit`) and JSON structured logging. |
| `gate.py` | `PreTradeGate`: daily-loss ledger → risk evaluation (with session calendar) → duplicate claim → mandatory audit. Advisory inputs (AI, Kronos, TradingView) are recorded but never passed to risk. Audit failure ⇒ rejection + breaker trip. |
| `replay.py` | Offline replay: strategy sees closed bars only, fills at next bar open, stop-before-target, gap fills at open; every signal passes through `risk.evaluate`. |

## Sizing
```
risk_budget   = min(equity × risk_fraction, equity × aggregate_cap − open_stop_risk)   # risk_fraction ≤ 0.5% hard ceiling
entry         = ask (long) / bid (short)
loss_per_unit = value_per_price_unit × |(entry ± slippage) − (stop ∓ slippage)| + round-trip costs
size          = floor_to_step(min(budget / loss_per_unit, venue_max), venue_step)
reject if size < venue_min   (never rounded up)
```
* XAUUSD (MT5): `value_per_price_unit = lots × tick_value_loss / tick_size`; costs = commission per lot; margin must be ≤ `max_margin_utilization` × free margin.
* BTCUSDT (Binance spot): `value_per_price_unit = qty`; costs = taker fee on entry and exit notional; long/flat only; min notional and free USDT (incl. fee) enforced. Tight stops that would need more notional than the balance are **rejected**, not resized.

## Daily-loss accounting (`trading/daily.py`)
* **Risk day:** XAUUSD/MT5 = broker server calendar day (server midnight = 17:00 New York on NY-close servers, the gold daily break); BTCUSDT/Binance = UTC day.
* **Equity:** account equity including unrealized P&L in account currency.
* **Day-start equity:** first observation of the risk day, raised to the last observation from an earlier day if higher (conservative). No history ⇒ first observation, flagged `estimated_start`.
* **Cash flows** (deposits, withdrawals, testnet resets) must be recorded with `record_cash_flow`; unrecorded withdrawals look like losses (conservative), unrecorded deposits would mask losses.
* **Halt:** loss ≥ 1.5% of day-start equity latches the halt for the rest of the risk day; it clears only on the next risk day. Observations must be chronological; regressions fail closed. The gate always overwrites adapter-supplied `day_start_equity` with the ledger value, and replay uses the same ledger.

## Rejection conditions (all collected, fail closed)
Non-demo environment or account; XAUUSD without a session calendar, market closed or within open/close buffer; latched daily halt; malformed signal/spec/account/quote/limits; mixed markets/symbols/currency; unreconciled account; stale or future-dated snapshot, quote or signal; tripped breaker / kill file; duplicate signal; pending orders; max concurrent positions; open position with unknown stop risk; daily loss ≥ 1.5% of day-start equity; aggregate open stop risk cap (1%) exhausted; spread above limit; market moved beyond entry deviation; stop off tick grid, already crossed or inside broker stops level; below venue minimum; insufficient margin/balance; any unexpected exception.

The 1.5% daily halt and 1% aggregate cap are **proposed guardrails to validate**, not proven settings.

## Known limitations
* Planned risk is not a loss bound: gaps (demonstrated in `test_gap_through_stop_fills_at_open_and_can_exceed_plan`), slippage beyond the allowance, partial fills and stop failures can exceed it.
* Binance fees charged in BTC on buys slightly reduce held quantity; reconciliation belongs to Phase 3.
* The server time zone and session template must be confirmed per broker (contract specification + `verify_server_offset` on a live demo tick) before Phase 2A data use. The MT5 Python API does not expose session tables.
* No built-in holiday calendar: holidays/early closes are operator-supplied closures.
* D1 bars on a non-UTC broker day boundary require `require_alignment=False`.
* Breaker state and the daily ledger are single-process per market; they are not cross-process locks.

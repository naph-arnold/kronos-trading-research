# MT5 demo execution: adapter, readiness, trial plan and live-money gate

Code: `trading/execution/mt5_demo.py`, tests in `tests/test_mt5_demo_execution.py` (15, on a fake terminal).
**No order has been submitted. Submission stays disabled until you separately approve a demo execution trial.**
There is no real-money mode anywhere in the codebase.

## 1. What the adapter does
Python remains the engine. The adapter only executes an **approved** decision from `trading.risk.evaluate`.

| Layer | Behaviour |
| --- | --- |
| Isolation | No shadow runner, research module or Command Center file imports it (test). It uses its own state directory, `state/mt5_demo_execution`. |
| Trial approval | `TRIAL_APPROVAL.json` in that directory names the account login, server, symbol, magic number, expiry, daily order cap and one open position. The environment variable `KRONOS_MT5_DEMO_TRIAL` must equal its id. Missing, expired or mismatched means blocked and recorded. |
| Demo only | `trade_mode == DEMO` is checked at connect **and immediately before every `order_send`**. Otherwise `LiveTradingForbidden`. |
| Proxy | The read-only allowlist plus `order_check`. `order_send` works only while armed inside a gated submit. Every other write function (close, modify, …) is refused. |
| Sizing | Volume comes from the risk decision, rounded **down** to the broker step. Below the minimum lot or above the maximum, the order is rejected. The loss at the stop is re-computed with the terminal's `order_calc_profit` at the actual price, plus commission, and must be ≤ 0.5% of current equity. |
| Protection | SL and TP are mandatory, on the correct sides, rounded away from or toward entry, and outside the stops and freeze level. After a fill the broker position must show SL and TP; otherwise **HALT**. |
| Duplicates | Idempotency key = sha256(signal ∣ book)[:16], recorded **before** sending and carried in the order comment. Positions, orders and 7 days of deals are searched for the key. One order per signal, and no re-send after a rejection. |
| Pre-check | `order_check` runs before `order_send`. Broker return codes are recorded verbatim, with names. |
| Connection | A disconnected terminal fails closed. Up to 3 reconnects, each logged. After any reconnect, a reconciliation must pass before the next order. |
| Reconciliation | Ledger against broker positions for the magic number. Closes found in deal history are recorded with net results. Foreign positions, vanished positions or unresolved sends trigger **HALT**. |
| Controls | KILL, STOP or HALT files block new orders. The daily order cap and the one-position limit come from the approval. |
| Records | `execution.sqlite`: every intent, request, check, result, return code, ticket, close and halt, with timestamps. The Command Center shows it read-only. |

There is no martingale, no averaging down, no retry loop, and no dashboard or Grok entry point.

## 2. Readiness assessment (2026-11-09)
| Item | Status |
| --- | --- |
| Adapter and safety layers | **Built and tested** on a fake terminal (15 tests) |
| Read-only account monitor | **Built**. One snapshot on the real demo account: balance and equity 3,000 USD, 0 positions, 1 deal (the deposit). Balance reconciles with deals. |
| Terminal "Algo Trading" switch | **Off** (`terminal_info().trade_allowed = false`). Orders would be refused (return code 10027) until you enable it. |
| Real `order_check` against MetaQuotes-Demo | **Not exercised**: it is a write-category call and needs trial approval |
| Filling mode, stops level, commission per deal | Filling and stops are read from `symbol_info` at order time. **Commission is unverified** until the first demo fill (MT5 does not expose it). |
| A strategy that qualifies for execution | **None.** No strategy has passed its pre-registered forward criteria, and scalping is rejected. A trial would test **execution mechanics only**, not profitability. |
| Runner isolation | The adapter is a separate process and state. A trial would add a further MT5 client, and the preflight showed multi-client operation is safe. |

**Verdict:** the mechanics are ready for an **operator-approved mechanics trial** on the demo account. Nothing is
ready to be traded for profit.

## 3. Step-by-step plan for an authorised demo execution trial (only after your written approval)
1. **Choose the scope.** Mechanics trial: at most 3 orders per day for 5 trading days, minimum lot, 1 open position,
   using a dedicated *trial signal source*. That source should be a frozen strategy's forward signals (e.g. NY ORB),
   mirrored with a separate magic number, so the frozen shadow tests stay untouched.
2. **Before the first order:**
   - you enable Algo Trading in the terminal;
   - write `state/mt5_demo_execution/TRIAL_APPROVAL.json` (login, server MetaQuotes-Demo, symbol XAUUSD, magic,
     expiry, cap);
   - set `KRONOS_MT5_DEMO_TRIAL` in that process's environment only;
   - run the preflight: connect, verify demo, reconcile = clean, and `order_check` on a sample request.
3. **During the trial**, every order goes signal → `trading.risk.evaluate` → adapter.
   - The Command Center shows the account, open positions, the execution log, rejections and SL/TP outcomes.
   - The read-only monitor polls every 60 s.
4. **Stop rules:**
   - any HALT, an unprotected position, a reconciliation mismatch, or a loss beyond planned risk plus slippage
     tolerance → stop and review;
   - the daily 1.5% halt and the 1% aggregate open risk stay enforced by the risk engine.
5. **Evaluation:**
   - fills against intended prices (slippage);
   - the real commission and swap per deal;
   - latency from signal to fill;
   - return codes;
   - zero duplicates and zero unprotected positions;
   - broker-confirmed P&L reconciling to the balance.

   Results are documented. The trial is not evidence of profitability.
6. **End:** remove the approval file. Open positions are closed by their own SL/TP, never by the dashboard.

## 4. Separate evidence-based gate before any real-money trading (all required; none met)
1. A pre-registered strategy passes its forward-test criteria on untouched data. Historical and Monte Carlo results
   do not count.
2. An authorised demo execution trial completes its pre-defined minimum: costs and slippage within tolerance, zero
   reconciliation mismatches, zero unprotected positions.
3. Broker commission, swap and slippage are verified from real demo fills, and the strategy stays net-positive with
   them.
4. Independent review of the execution code, risk limits and kill procedures, with STOP/KILL/HALT drills.
5. A written operator decision naming the account, the maximum capital at risk and an end date. Credentials are never
   stored in code, logs or GitHub.
6. A separate live adapter and configuration. This codebase has none, and the demo adapter refuses non-demo accounts.

The Command Center's "Real money (future)" tab shows this gate with every item "not met".

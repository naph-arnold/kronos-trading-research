# Command Center: Scalping & account performance (`/account/`)

The view is read-only: no control can place, change or close an order. It has four modes, and balances are never
mixed.

| Mode | Source | Meaning |
| --- | --- | --- |
| MT5 demo broker | `research/mt5_account_monitor.py` snapshots (account_info, terminal_info, positions, deal history through the read-only allowlist proxy), the runners' periodic account checks, and the execution ledger | Broker-confirmed. **Demo funds are not withdrawable money.** |
| Shadow forward test | The frozen runners' shadow books (headline book per strategy) | Simulated fills on live quotes; no order; not money |
| Historical research | Recorded holdout trade ledgers of the Phase 2 research reports | Examined history, modelled costs, report book; not forward evidence |
| Real money (future) | none | Disabled; shows the live-money approval gate |

## Accounting rules (`trading/accounting.py`, tests in `tests/test_accounting.py`)
**Broker deals:**
- Trading result of a deal = profit + commission + swap + fee.
- A position is the set of deals sharing a position id. It is closed when exit volume equals entry volume. Its net
  result is the sum of its deals, and its exit price is volume-weighted.

**Cash flows and other deals:**
- **Deposits, withdrawals, credit and bonus are cash flows, never trading profit.**
- Charge and commission-type deals are trading costs.
- Corrections are reported separately.

**Reported figures:**
- Realized P&L (today, this week and this month, all UTC) comes from closed positions.
- Unrealized P&L is the open positions' profit + swap as reported by the terminal.
- Drawdown and the equity curve use starting capital (the first deposit or the book's starting equity) + cumulative
  closed-trade P&L, so cash flows never appear as gains or drawdowns. This is closed-trade drawdown; research reports
  that mark open trades can show more.
- The win rate, profit factor and losing streak are `None` (shown as "—") when there are no trades, never 0.

**Balance check:** the reported balance must equal net cash flows + trading P&L + adjustments.

**Missing data:**
- Without a snapshot the account shows **unavailable**, never 0.
- A failed snapshot shows **disconnected**, together with the last good snapshot and its time.
- A snapshot older than 10 minutes shows **stale**.

**Shadow-book costs:**
- They are itemised: spread + slippage, commission and swap.
- Open shadow positions are marked at bar closes.

**Trade details:** expanding a trade shows entry, exit, stop, target, volume, planned risk, R, each cost, session and
timeframe. For shadow trades it also shows the strategy's recorded context at the signal (why it was taken). The
risk-engine rejections list shows why signals were *not* taken.

## Live monitoring
- Balance and equity come from the runners' read-only checks (every few minutes) and from the monitor's snapshots:
  `python -m research.mt5_account_monitor --interval 60` for continuous polling, or `--once`.
- The monitor is **not** started persistently. Running it continuously adds one more read-only MT5 client process and
  needs your go-ahead.
- Demo orders, rejections, fills and SL/TP outcomes appear in the execution log once a trial is authorised.

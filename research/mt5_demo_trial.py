"""One controlled MT5 DEMO mechanics trial order: pre-trial checklist (read-only) and, only after operator approval,
the single order. Purpose: verify broker execution, protective stops, commission and reconciliation - NOT strategy
profitability. Uses the existing adapter (trading.execution.mt5_demo); nothing here can bypass its gates.

    python -m research.mt5_demo_trial preflight            # read-only; default; sends nothing
    python -m research.mt5_demo_trial template             # prints an approval-file template (writes nothing)
    python -m research.mt5_demo_trial execute              # ONE order; refused unless the approval file + env exist

The mechanics order: BUY at market, minimum lot (0.01), stop = entry - 3 x ATR14(5m), target = entry + 3 x ATR14(5m),
approved by trading.risk.evaluate (0.5% ceiling; size only ever reduced to the minimum lot). One order per approval.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

UTC = timezone.utc
STATE = Path("state/mt5_demo_execution")
MAGIC = 260_901
STOP_ATR = Decimal("3")
TRIAL_VOLUME = Decimal("0.01")


def _atr(bars, n: int = 14) -> Decimal:
    trs, prev = [], None
    for b in bars:
        tr = b.high - b.low if prev is None else max(b.high - b.low, abs(b.high - prev), abs(b.low - prev))
        trs.append(tr)
        prev = b.close
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = a + (tr - a) / n
    return a


def build_trial(conn, now: datetime) -> dict:
    """Read-only: everything up to (but excluding) order_check/order_send."""
    from trading.breaker import CircuitBreaker
    from trading.common import Market, Side
    from trading.config import load_demo_environment
    from trading.connectors.mt5 import _as_dict
    from trading.execution.mt5_demo import SymbolRules, build_request, idempotency_key
    from trading.risk import RiskLimits, evaluate
    from trading.signals import TradeSignal
    from trading.state import AccountSnapshot

    checks = []
    account = _as_dict(conn.mt5.account_info())
    terminal = _as_dict(conn.mt5.terminal_info())
    checks.append(("demo account (trade_mode 0)", account.get("trade_mode") == 0, f"server {account.get('server')}"))
    checks.append(("terminal connected", bool(terminal.get("connected")), ""))
    checks.append(("terminal Algo Trading enabled", bool(terminal.get("trade_allowed")),
                   "operator must enable the Algo Trading button" if not terminal.get("trade_allowed") else ""))
    checks.append(("account trading allowed", bool(account.get("trade_allowed")), ""))
    info = _as_dict(conn.mt5.symbol_info("XAUUSD"))
    rules = SymbolRules.from_info(info)
    checks.append(("filling mode available (FOK/IOC)", bool(rules.filling_flags & 3), f"flags {rules.filling_flags}"))
    checks.append(("minimum lot 0.01", rules.volume_min == TRIAL_VOLUME, f"min {rules.volume_min} step {rules.volume_step}"))
    disc = conn.symbol_specification("XAUUSD")
    inst = disc.to_instrument_spec(commission_assumption=Decimal("7"))
    quote = conn.quote("XAUUSD", now=now)
    bars = conn.history("XAUUSD", "5m", now - timedelta(days=2), now)
    a = _atr([b.bar for b in bars][-200:])
    from decimal import ROUND_FLOOR
    tick = Decimal("0.01")
    entry = quote.ask
    stop = (entry - STOP_ATR * a).quantize(tick, rounding=ROUND_FLOOR)   # away from entry
    tp = (entry + STOP_ATR * a).quantize(tick, rounding=ROUND_FLOOR)     # toward entry
    signal = TradeSignal.create(signal_id=f"mt5-trial-mechanics-{now:%Y%m%dT%H%M}", market=Market.XAUUSD_MT5,
                                symbol="XAUUSD", side=Side.LONG, entry_price=entry, stop_price=stop, take_profit=tp,
                                strategy_id="mechanics-trial", strategy_version="1", generated_at=now, bar_close_time=now)
    equity = Decimal(str(account.get("equity")))
    snap = AccountSnapshot(market=Market.XAUUSD_MT5, account_id="mt5-demo", is_demo=True,
                           currency=account.get("currency", "USD"), equity=equity,
                           balance=Decimal(str(account.get("balance"))),
                           available_funds=Decimal(str(account.get("margin_free"))), day_start_equity=equity,
                           snapshot_at=now, reconciled=True)
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=Decimal("1.00"), slippage_allowance=Decimal("0.10"))
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, (now - timedelta(days=3)).date(), (now + timedelta(days=3)).date())
    decision = evaluate(signal, account=snap, instrument=inst, quote=quote, limits=limits,
                        environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
                        breaker=CircuitBreaker(Market.XAUUSD_MT5), now=now, session_calendar=calendar)
    checks.append(("risk engine approves (0.5% ceiling)", decision.approved, "; ".join(decision.reasons)))
    request = None
    if decision.approved:
        from dataclasses import replace
        decision = replace(decision, size=min(decision.size, TRIAL_VOLUME))  # reduce only, to the minimum lot
        request = build_request(signal, decision, rules, _as_dict(conn.mt5.symbol_info_tick("XAUUSD")),
                                key=idempotency_key(signal.signal_id, "trial"), magic=MAGIC)
        loss = conn.mt5.order_calc_profit(0, "XAUUSD", request["volume"], request["price"], request["sl"])
        checks.append(("loss at stop <= 0.5% equity (terminal calc + 7 USD/lot)",
                       loss is not None and abs(Decimal(str(loss))) + Decimal("0.07") <= equity * Decimal("0.005"),
                       f"loss {loss} USD at {request['volume']} lot"))
    checks.append(("trial approval file present", (STATE / "TRIAL_APPROVAL.json").exists(), "operator writes it"))
    return {"at": now.isoformat(), "account": {k: account.get(k) for k in ("server", "balance", "equity", "margin_free", "leverage")},
            "atr5": float(a), "quote": {"bid": float(quote.bid), "ask": float(quote.ask)},
            "signal": {"id": signal.signal_id, "side": "long", "entry": float(entry), "stop": float(stop), "tp": float(tp)},
            "risk_decision": decision.to_record(), "request": request,
            "checks": [{"check": c, "ok": bool(ok), "detail": d} for c, ok, d in checks], "_signal": signal,
            "_decision": decision}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="preflight", choices=["preflight", "template", "execute"])
    args = ap.parse_args(argv)
    now = datetime.now(UTC)
    if args.command == "template":
        print(json.dumps({"approval_id": "trial-YYYYMMDD-1", "approved_by": "<operator>", "approved_at": now.isoformat(),
                          "expires_at": (now + timedelta(hours=8)).isoformat(), "account_login": "<demo login>",
                          "server": "MetaQuotes-Demo", "symbol": "XAUUSD", "magic": MAGIC, "max_orders_per_day": 1,
                          "max_open_positions": 1}, indent=1))
        print(f"# save as {STATE / 'TRIAL_APPROVAL.json'} and set KRONOS_MT5_DEMO_TRIAL=<approval_id> for the execute process only")
        return 0
    from trading.clock import EET_EU
    from trading.connectors.mt5 import Mt5ReadOnlyConnector
    conn = Mt5ReadOnlyConnector(server_tz=EET_EU)  # MetaQuotes-Demo server time (verified EET/EU DST)
    conn.connect()
    try:
        trial = build_trial(conn, now)
    finally:
        conn.shutdown()
    public = {k: v for k, v in trial.items() if not k.startswith("_")}
    out = Path("research/output/mt5_demo_trial")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"preflight_{now:%Y%m%dT%H%M%S}.json").write_text(json.dumps(public, indent=1, default=str), encoding="utf-8")
    print(json.dumps(public, indent=1, default=str))
    if args.command != "execute":
        return 0
    import MetaTrader5  # noqa: N813 - only for the approved execute path
    from trading.execution.mt5_demo import Mt5DemoExecutor
    ex = Mt5DemoExecutor(MetaTrader5, STATE, symbol="XAUUSD", magic=MAGIC)
    ex.connect()
    print(json.dumps(ex.reconcile(), default=str))
    print(json.dumps(ex.submit(trial["_signal"], trial["_decision"], book="trial", strategy="mechanics-trial"), default=str))
    print(json.dumps(ex.reconcile(), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

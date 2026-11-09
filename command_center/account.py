"""Scalping & Account Performance views (read-only). Four modes, never mixed:

  historical - research reports' recorded holdout ledgers (examined history, modelled costs, report book)
  shadow     - forward-test shadow books (simulated fills on live quotes; no order was placed; not money)
  broker     - the MT5 DEMO account as recorded by the read-only account monitor and the runners' account checks
               (broker-confirmed deals; demo funds are not withdrawable money)
  live       - real-money trading: a future capability behind a separate approval gate; no data, no connection

Missing broker data is reported as unavailable/disconnected, never as zero. Deposits and withdrawals are cash
flows, not trading profit (trading.accounting).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from command_center.readonly import ReadOnlyShadowStore, connect_ro
from trading.accounting import (ClosedTrade, cash_flow_summary, performance, positions_from_deals, session_of,
                                unrealized)

UTC = timezone.utc
STALE_AFTER = timedelta(minutes=10)


def _t(s) -> datetime | None:
    return None if not s else datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)


def _dec(v) -> Decimal:
    return Decimal("0") if v in (None, "") else Decimal(str(v))


def _mask(login) -> str | None:
    return None if login is None else f"••••{str(login)[-3:]}"


# --------------------------------------------------------------------------- broker (MT5 demo)

def broker_view(cfg: dict, now: datetime) -> dict:
    acfg = cfg.get("account") or {}
    out = {"mode": "broker", "label": "MT5 demo account (broker-confirmed)", "money": "demo funds - not withdrawable",
           "available": False, "connection": "unavailable", "account": None}
    db = Path(acfg.get("mt5_state_dir", "")) / "account.sqlite"
    runner_checks = _runner_account_checks(cfg)
    out["runner_checks"] = runner_checks
    out["series"] = balance_equity_series(cfg, Path(acfg.get("mt5_state_dir", "")) / "account.sqlite")
    if not db.exists():
        out["note"] = ("The read-only account monitor has not recorded a snapshot. Balance and equity below come only "
                       "from the runners' periodic read-only account checks.") if runner_checks else \
            "No broker data recorded: the account is shown as unavailable, not as zero."
        out["execution"] = execution_log(cfg)
        return out
    con = connect_ro(db)
    try:
        last = con.execute("SELECT at, connected, account, terminal, positions, error FROM snapshots ORDER BY at DESC LIMIT 1").fetchone()
        last_ok = con.execute("SELECT at, account, terminal FROM snapshots WHERE connected=1 ORDER BY at DESC LIMIT 1").fetchone()
        deals = [json.loads(b) for (b,) in con.execute("SELECT body FROM deals ORDER BY time")]
        positions = [json.loads(b) for (b,) in con.execute("SELECT body FROM positions")]
    finally:
        con.close()
    if last is None:
        out["note"] = "No snapshot recorded."
        return out
    age = now - _t(last[0])
    out["last_snapshot_at"] = last[0]
    out["last_snapshot_age_s"] = round(age.total_seconds())
    out["connection"] = ("disconnected" if not last[1] else "stale" if age > STALE_AFTER else "connected")
    out["last_error"] = last[5]
    if last_ok is None:
        return out
    acct, term = json.loads(last_ok[1]), json.loads(last_ok[2] or "{}")
    out["available"] = True
    out["last_successful_at"] = last_ok[0]
    out["account"] = {k: acct.get(k) for k in ("balance", "equity", "margin", "margin_free", "margin_level", "profit",
                                               "credit", "currency", "leverage", "server", "company")}
    out["account"]["login"] = _mask(acct.get("login"))
    out["account"]["is_demo"] = acct.get("trade_mode") == 0
    out["terminal"] = {"connected": term.get("connected"), "algo_trading_enabled": term.get("trade_allowed"),
                       "build": term.get("build")}
    cf = cash_flow_summary(deals)
    closed, still_open = positions_from_deals(deals, strategy_by_magic=(acfg.get("strategy_by_magic") or {}))
    first_deposit = next((Decimal(str(f["amount"])) for f in cf["flows"] if f["amount"] > 0), None)
    perf = performance(closed, starting_capital=first_deposit, now=now, extra_costs=_dec(cf["account_cost_deals"]))
    out.update(cash_flows=cf, performance=perf, open_positions=unrealized(positions),
               closed_trades=[_trade_row(c) for c in reversed(closed[-200:])], open_position_ids=len(still_open),
               reconciliation={"deals": len(deals), "positions": len(positions),
                               "balance_check": _balance_check(acct, cf, perf)})
    out["execution"] = execution_log(cfg)
    if out["connection"] != "connected":
        out["note"] = f"Showing the last successful snapshot from {last_ok[0]}; the account is currently {out['connection']}."
    return out


def _balance_check(acct: dict, cf: dict, perf: dict) -> dict:
    """Balance should equal cash flows + trading P&L + adjustments (all from broker deals)."""
    expected = Decimal(str(cf["net_cash_flow"])) + Decimal(str(perf["net_profit"])) + Decimal(str(cf["adjustments"]))
    actual = _dec(acct.get("balance"))
    return {"expected_from_deals": float(expected), "reported_balance": float(actual),
            "difference": float(actual - expected), "ok": abs(actual - expected) < Decimal("0.01")}


def balance_equity_series(cfg: dict, acct_db: Path | None, max_points: int = 600) -> dict:
    """Observed broker balance and equity over time: the gold runner's periodic read-only account checks plus the
    account monitor's snapshots. Nothing is interpolated; gaps stay gaps."""
    pts: dict[int, tuple[float, float, str]] = {}
    db = Path(cfg["markets"]["xauusd"]["state_dir"]) / "shadow.sqlite"
    if db.exists():
        con = connect_ro(db)
        try:
            for at, detail in con.execute("SELECT at, detail FROM events WHERE kind IN ('mt5_account_check', "
                                          "'mt5_account_baseline') ORDER BY id"):
                d = json.loads(detail)
                if d.get("balance") is not None and d.get("equity") is not None:
                    pts[int(_t(at).timestamp())] = (float(d["balance"]), float(d["equity"]), "runner check")
        finally:
            con.close()
    if acct_db is not None and acct_db.exists():
        con = connect_ro(acct_db)
        try:
            for at, a in con.execute("SELECT at, account FROM snapshots WHERE connected=1 ORDER BY at"):
                d = json.loads(a)
                pts[int(_t(at).timestamp())] = (float(d["balance"]), float(d["equity"]), "account monitor")
        finally:
            con.close()
    items = sorted(pts.items())
    if len(items) > max_points:
        step = len(items) / max_points
        items = [items[int(i * step)] for i in range(max_points - 1)] + [items[-1]]
    return {"points": len(pts), "balance": [{"time": t, "value": v[0]} for t, v in items],
            "equity": [{"time": t, "value": v[1]} for t, v in items],
            "source": "observed: runners' read-only account checks + account monitor snapshots"}


def _runner_account_checks(cfg: dict) -> list[dict]:
    """Latest read-only account check recorded by each MT5 runner (balance/equity/positions/orders + timestamp)."""
    rows = []
    stores = [("xauusd", cfg["markets"]["xauusd"]["state_dir"])] + [
        (s["id"], s["state_dir"]) for s in cfg.get("strategies", []) if s.get("state_dir") and s["market"] == "xauusd"]
    for name, d in stores:
        db = Path(d) / "shadow.sqlite"
        if not db.exists():
            continue
        con = connect_ro(db)
        try:
            r = con.execute("SELECT at, detail FROM events WHERE kind IN ('mt5_account_check', 'mt5_account_baseline') "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if r:
            d_ = json.loads(r[1])
            rows.append({"runner": name, "at": r[0], "balance": d_.get("balance"), "equity": d_.get("equity"),
                         "positions": d_.get("positions_total"), "orders": d_.get("orders_total"),
                         "unchanged": d_.get("unchanged")})
    return rows


def execution_log(cfg: dict) -> dict:
    d = Path((cfg.get("account") or {}).get("execution_state_dir", "")) / "execution.sqlite"
    status = {"authorized": (Path((cfg.get("account") or {}).get("execution_state_dir", "")) / "TRIAL_APPROVAL.json").exists()}
    if not d.exists():
        return {**status, "orders": [], "counts": {}, "note": "No demo orders: the execution trial has not been authorized."}
    con = connect_ro(d)
    try:
        cur = con.execute("SELECT key, signal_id, book, strategy, state, created_at, side, volume, price, sl, tp, retcode, "
                          "reason, net_result, closed_at FROM orders ORDER BY created_at DESC LIMIT 200")
        cols = [c[0] for c in cur.description]
        orders = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["state"]] = counts.get(o["state"], 0) + 1
    return {**status, "orders": orders, "counts": counts}


# --------------------------------------------------------------------------- shadow books

def shadow_view(cfg: dict, now: datetime, strategy_id: str | None) -> dict:
    from command_center.services import _primary_book, _strategy_runner

    strategies = []
    for s in cfg["strategies"]:
        runner = _strategy_runner(cfg, s)
        db = Path(runner["state_dir"]) / "shadow.sqlite"
        strategies.append({"id": s["id"], "name": s["name"], "market": s["market"], "available": db.exists()})
    sid = strategy_id or next((s["id"] for s in strategies if s["available"]), None)
    out = {"mode": "shadow", "label": "Shadow forward test (simulated books)", "money": "simulated - no order, not money",
           "strategies": strategies, "strategy": sid}
    s = next((x for x in cfg["strategies"] if x["id"] == sid), None)
    if s is None:
        return {**out, "available": False}
    runner = _strategy_runner(cfg, s)
    db = Path(runner["state_dir"]) / "shadow.sqlite"
    if not db.exists():
        return {**out, "available": False, "note": "Runner not launched: nothing recorded."}
    with ReadOnlyShadowStore(db) as store:
        books = store.books()
        primary = _primary_book(books, s["strategy_key"])
        info = books.get(primary) if primary else None
        trades = store.trades(primary) if primary else []
        sigs = {x["signal_id"]: x for x in store.signals() if x["strategy_key"] == s["strategy_key"]}
        decisions = [d for d in store.decisions(primary)] if primary else []
        positions = {b: p for b, p in store.positions().items() if b == primary}
        curve = store.equity_curve(primary) if primary else []
    closed = [ClosedTrade(
        trade_id=t["signal_id"], strategy=s["strategy_key"], symbol=s["market"].upper(), side=t.get("side"),
        opened_at=_t(t.get("opened_at")), closed_at=_t(t["closed_at"]), volume=_dec(t.get("size")),
        entry=_dec(t.get("entry")), exit=_dec(t.get("exit")), gross=_dec(t.get("gross_pnl")),
        commission=-abs(_dec(t.get("commission"))), swap=_dec(t.get("swap")),  # signed: charge < 0, credit > 0
        spread_slippage=-abs(_dec(t.get("spread_slippage_cost"))), planned_risk=_dec(t.get("planned_risk")) or None,
        stop=_dec(t.get("stop")), target=_dec(t.get("take_profit")) if t.get("take_profit") else None,
        session=session_of(_t(t.get("opened_at"))), timeframe=runner.get("store_interval"), exit_reason=t.get("exit_reason"),
        meta={"reason": (sigs.get(t["signal_id"], {}).get("body") or {}).get("provenance", {}).get("context")})
        for t in trades]
    initial = info["initial_equity"] if info else None
    perf = performance(closed, starting_capital=initial, now=now)
    taken = {t.trade_id for t in closed}
    rejected = [{"signal_id": d["signal_id"], "at": d["evaluated_at"], "reasons": d["reasons"]}
                for d in decisions if not d["approved"]]
    return {**out, "available": True, "book": primary, "initial_equity": float(initial) if initial else None,
            "equity_marked": float(curve[-1][1]) if curve else (float(initial) if initial else None),
            "equity_marked_at": curve[-1][0] if curve else None, "performance": perf,
            "closed_trades": [_trade_row(c) for c in reversed(closed[-200:])],
            "open_positions": [{"book": b, **{k: p.get(k) for k in ("signal_id", "side", "entry", "stop", "take_profit",
                                                                     "size", "opened_at", "planned_risk")},
                                "floating": None, "floating_note": "shadow books are marked at bar closes (see equity)"}
                               for b, p in positions.items()],
            "rejected": rejected[-100:], "forward_signals": sum(1 for x in sigs.values() if x["status"] != "warmup"),
            "warmup_signals": sum(1 for x in sigs.values() if x["status"] == "warmup"), "taken": len(taken)}


def _trade_row(c: ClosedTrade) -> dict:
    return {"id": c.trade_id, "strategy": c.strategy, "side": c.side, "opened_at": c.opened_at.isoformat() if c.opened_at else None,
            "closed_at": c.closed_at.isoformat(), "volume": float(c.volume) if c.volume is not None else None,
            "entry": float(c.entry) if c.entry is not None else None, "exit": float(c.exit) if c.exit is not None else None,
            "stop": float(c.stop) if c.stop else None, "target": float(c.target) if c.target else None,
            "gross": float(c.gross), "commission": float(c.commission), "swap": float(c.swap), "fee": float(c.fee),
            "spread_slippage": float(c.spread_slippage), "net": float(c.net),
            "planned_risk": float(c.planned_risk) if c.planned_risk else None, "r": float(c.r) if c.r is not None else None,
            "session": c.session, "timeframe": c.timeframe, "exit_reason": c.exit_reason, "reason": c.meta.get("reason")}


# --------------------------------------------------------------------------- historical research

def historical_view(cfg: dict, now: datetime, strategy: str | None) -> dict:
    out = {"mode": "historical", "label": "Historical research (examined holdout, modelled costs)",
           "money": "research backtest - not an account", "datasets": []}
    for src in (cfg.get("account") or {}).get("historical_reports", []):
        p = Path(src["file"])
        if not p.exists():
            continue
        rep = json.loads(p.read_text(encoding="utf-8"))
        for name, s in rep.get("strategies", {}).items():
            if s.get("holdout_trades"):
                out["datasets"].append({"id": f"{src['market']}:{name}", "market": src["market"], "strategy": name,
                                        "trades": len(s["holdout_trades"]), "file": p.name})
    sel = strategy or next((d["id"] for d in out["datasets"] if d["strategy"] == "orb_newyork"), None)
    d = next((x for x in out["datasets"] if x["id"] == sel), None)
    if d is None:
        return {**out, "available": False}
    src = next(x for x in cfg["account"]["historical_reports"] if x["market"] == d["market"])
    rep = json.loads(Path(src["file"]).read_text(encoding="utf-8"))
    s = rep["strategies"][d["strategy"]]
    equity = Decimal(str(rep.get("initial_equity") or src.get("initial_equity")))
    closed = [ClosedTrade(trade_id=t["signal_id"], strategy=d["strategy"], symbol=d["market"].upper(), side=t["side"],
                          opened_at=_t(t["opened_at"]), closed_at=_t(t["closed_at"]), volume=_dec(t["size"]),
                          entry=_dec(t["entry"]), exit=_dec(t["exit"]), gross=_dec(t["gross_pnl"]),
                          spread_slippage=-_dec(t["costs"]), planned_risk=_dec(t["planned_risk"]),
                          session=session_of(_t(t["opened_at"])), timeframe=src.get("timeframe"),
                          exit_reason=t.get("exit_reason")) for t in s["holdout_trades"]]
    perf = performance(closed, starting_capital=equity, now=now)
    return {**out, "available": True, "selected": sel, "initial_equity": float(equity), "performance": perf,
            "period": [s.get("holdout", {}).get("start"), s.get("holdout", {}).get("end")],
            "report_summary": {k: s.get("holdout", {}).get(k) for k in ("trades", "net_return_pct", "max_drawdown_pct")},
            "costs_note": "Costs are the report's combined modelled spread + slippage + commission per trade.",
            "closed_trades": [_trade_row(c) for c in reversed(closed[-300:])]}


# --------------------------------------------------------------------------- live (future)

LIVE_GATE = [
    "A pre-registered strategy passes its forward-test criteria on untouched data (not historical or Monte Carlo).",
    "An authorized MT5 demo execution trial completes its pre-defined minimum (fills, slippage and costs within "
    "the stated tolerances; zero reconciliation mismatches; zero unprotected positions).",
    "Broker commission, swap and slippage are verified from real demo fills, and the strategy stays net-positive with them.",
    "Independent review of the execution code, risk limits and kill procedures; tested STOP/KILL/HALT drills.",
    "Written operator decision naming the account, maximum capital at risk, and an end date; separate credentials "
    "never stored in code, logs or GitHub.",
    "A separate live adapter and configuration (this codebase has no live mode).",
]


def live_view() -> dict:
    return {"mode": "live", "label": "Real-money trading", "available": False, "enabled": False,
            "note": "Future capability only. No real-money account is connected and no live mode exists.",
            "approval_gate": [{"requirement": g, "status": "not met"} for g in LIVE_GATE]}


def account_view(cfg: dict, mode: str, strategy: str | None = None) -> dict:
    now = datetime.now(UTC)
    if mode == "broker":
        return broker_view(cfg, now)
    if mode == "shadow":
        return shadow_view(cfg, now, strategy)
    if mode == "historical":
        return historical_view(cfg, now, strategy)
    if mode == "live":
        return live_view()
    raise ValueError(f"unknown account mode {mode!r}")

"""Trading-performance accounting shared by every account mode (pure functions, no I/O).

Rules (docs/account-performance.md):
* Trading P&L of a broker deal = profit + commission + swap + fee (MT5 reports costs as negative numbers).
* Deposits, withdrawals, credit and bonuses are CASH FLOWS, never trading profit.
* Broker charges and commission-type deals are trading costs; corrections are reported separately (they are
  neither trading results nor ordinary cash flows) and are excluded from trading P&L.
* A position is closed when its exit volume equals its entry volume; its net result is the sum of all its deals.
* Drawdown is measured on the trading curve: starting capital + cumulative trading P&L, so a withdrawal is never a
  drawdown and a deposit is never a gain.
* Nothing is filled in: missing inputs give None, never 0.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

UTC = timezone.utc
D0 = Decimal("0")

# MetaTrader5 DEAL_TYPE_* values
DEAL_BUY, DEAL_SELL = 0, 1
CASH_FLOW_TYPES = {2: "balance", 3: "credit", 6: "bonus"}
COST_TYPES = {4: "charge", 7: "commission", 8: "commission_daily", 9: "commission_monthly",
              10: "agent_commission_daily", 11: "agent_commission_monthly", 13: "interest"}
ADJUSTMENT_TYPES = {5: "correction"}
# DEAL_ENTRY_*
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3


def _d(v) -> Decimal:
    return D0 if v is None else Decimal(str(v))


@dataclass(frozen=True)
class ClosedTrade:
    """One closed position / simulated trade, in account currency, after every recorded cost."""
    trade_id: str
    strategy: str | None
    symbol: str
    side: str | None
    opened_at: datetime | None
    closed_at: datetime
    volume: Decimal | None
    entry: Decimal | None
    exit: Decimal | None
    gross: Decimal                 # price P&L before costs
    commission: Decimal = D0       # negative = cost
    swap: Decimal = D0
    fee: Decimal = D0
    spread_slippage: Decimal = D0  # simulated books only (broker deals embed spread in the fill price)
    planned_risk: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    session: str | None = None
    timeframe: str | None = None
    exit_reason: str | None = None
    meta: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def net(self) -> Decimal:
        return self.gross + self.commission + self.swap + self.fee + self.spread_slippage

    @property
    def r(self) -> Decimal | None:
        return None if not self.planned_risk else self.net / self.planned_risk


def session_of(t: datetime | None) -> str | None:
    if t is None:
        return None
    h = t.astimezone(UTC).hour
    return "asia" if h < 7 else "london" if h < 12 else "new_york" if h < 21 else "late"


# --------------------------------------------------------------------------- broker deals

def classify_deals(deals: list[dict]) -> dict:
    """Split MT5 deals into trading deals, cash flows, cost deals and adjustments."""
    out = {"trading": [], "cash_flows": [], "costs": [], "adjustments": [], "unknown": []}
    for d in deals:
        t = d.get("type")
        if t in (DEAL_BUY, DEAL_SELL):
            out["trading"].append(d)
        elif t in CASH_FLOW_TYPES:
            out["cash_flows"].append(d)
        elif t in COST_TYPES:
            out["costs"].append(d)
        elif t in ADJUSTMENT_TYPES:
            out["adjustments"].append(d)
        else:
            out["unknown"].append(d)
    return out


def _deal_time(d: dict) -> datetime:
    if d.get("time_msc"):
        return datetime.fromtimestamp(int(d["time_msc"]) / 1000, UTC)
    return datetime.fromtimestamp(int(d["time"]), UTC)


def positions_from_deals(deals: list[dict], *, strategy_by_magic: dict | None = None) -> tuple[list[ClosedTrade], list[dict]]:
    """Group trading deals by position id. Returns (closed positions, still-open position ids with their deals)."""
    by_pos: dict = defaultdict(list)
    for d in classify_deals(deals)["trading"]:
        by_pos[d.get("position_id") or d.get("ticket")].append(d)
    closed, still_open = [], []
    for pid, ds in by_pos.items():
        ds.sort(key=_deal_time)
        vin = sum((_d(x["volume"]) for x in ds if x.get("entry") in (ENTRY_IN,)), D0)
        vout = sum((_d(x["volume"]) for x in ds if x.get("entry") in (ENTRY_OUT, ENTRY_OUT_BY)), D0)
        if vin == D0 or vout < vin:
            still_open.append({"position_id": pid, "deals": ds})
            continue
        first = next(x for x in ds if x.get("entry") == ENTRY_IN)
        exits = [x for x in ds if x.get("entry") in (ENTRY_OUT, ENTRY_OUT_BY)]
        exit_vwap = sum((_d(x["price"]) * _d(x["volume"]) for x in exits), D0) / vout
        closed.append(ClosedTrade(
            trade_id=str(pid), strategy=(strategy_by_magic or {}).get(first.get("magic")) or (f"magic {first.get('magic')}" if first.get("magic") else "manual"),
            symbol=first.get("symbol", ""), side="long" if first.get("type") == DEAL_BUY else "short",
            opened_at=_deal_time(first), closed_at=_deal_time(exits[-1]), volume=vin, entry=_d(first.get("price")),
            exit=exit_vwap, gross=sum((_d(x.get("profit")) for x in ds), D0),
            commission=sum((_d(x.get("commission")) for x in ds), D0), swap=sum((_d(x.get("swap")) for x in ds), D0),
            fee=sum((_d(x.get("fee")) for x in ds), D0), session=session_of(_deal_time(first)),
            exit_reason=_exit_reason(exits[-1].get("reason")),
            meta={"deals": [x.get("ticket") for x in ds], "magic": first.get("magic"), "comment": first.get("comment")}))
    closed.sort(key=lambda c: c.closed_at)
    return closed, still_open


def _exit_reason(reason) -> str | None:
    return {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "stop_loss", 5: "take_profit", 6: "stop_out",
            7: "rollover", 8: "variation_margin", 9: "split"}.get(reason)


def cash_flow_summary(deals: list[dict]) -> dict:
    c = classify_deals(deals)
    flows = [{"at": _deal_time(d).isoformat(), "kind": CASH_FLOW_TYPES[d["type"]], "amount": float(_d(d.get("profit"))),
              "comment": d.get("comment")} for d in c["cash_flows"]]
    deposits = sum((Decimal(str(f["amount"])) for f in flows if f["amount"] > 0), D0)
    withdrawals = sum((Decimal(str(f["amount"])) for f in flows if f["amount"] < 0), D0)
    costs = sum((_d(d.get("profit")) + _d(d.get("commission")) + _d(d.get("fee")) for d in c["costs"]), D0)
    adjustments = sum((_d(d.get("profit")) for d in c["adjustments"]), D0)
    return {"flows": flows, "deposits": float(deposits), "withdrawals": float(withdrawals),
            "net_cash_flow": float(deposits + withdrawals), "account_cost_deals": float(costs),
            "adjustments": float(adjustments), "unknown_deals": len(c["unknown"])}


# --------------------------------------------------------------------------- performance

def _start_of(period: str, t: datetime) -> datetime:
    t = t.astimezone(UTC)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def performance(trades: list[ClosedTrade], *, starting_capital: Decimal | None, now: datetime,
                extra_costs: Decimal = D0) -> dict:
    """Net performance of closed trades. `extra_costs` = account-level cost deals (charges, commission deals)."""
    trades = sorted(trades, key=lambda t: t.closed_at)
    n = len(trades)
    nets = [t.net for t in trades]
    total = sum(nets, D0) + extra_costs
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win, gross_loss = sum(wins, D0), -sum(losses, D0)
    streak = longest = 0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        longest = max(longest, streak)
    curve, peak, max_dd, max_dd_pct = [], None, D0, None
    if starting_capital is not None:
        eq = starting_capital
        peak = eq
        max_dd_pct = D0
        for t in trades:
            eq += t.net
            peak = max(peak, eq)
            dd = peak - eq
            max_dd = max(max_dd, dd)
            max_dd_pct = max(max_dd_pct, dd / peak * 100 if peak else D0)
            curve.append({"time": int(t.closed_at.timestamp()), "value": float(eq)})
    buckets = {}
    for period in ("day", "week", "month"):
        start = _start_of(period, now)
        buckets[period] = float(sum((t.net for t in trades if t.closed_at >= start), D0))
    costs = {"commission": float(sum((t.commission for t in trades), D0)), "swap": float(sum((t.swap for t in trades), D0)),
             "fee": float(sum((t.fee for t in trades), D0)),
             "spread_slippage": float(sum((t.spread_slippage for t in trades), D0)),
             "account_cost_deals": float(extra_costs)}
    rs = [t.r for t in trades if t.r is not None]

    def group(key):
        g: dict = defaultdict(list)
        for t in trades:
            g[key(t) or "unknown"].append(t.net)
        return {k: {"trades": len(v), "net": float(sum(v, D0)), "win_rate_pct": round(100 * sum(1 for x in v if x > 0) / len(v), 2)}
                for k, v in sorted(g.items())}
    return {
        "closed_trades": n, "net_profit": float(total), "gross_before_costs": float(sum((t.gross for t in trades), D0)),
        "win_rate_pct": round(100 * len(wins) / n, 2) if n else None,
        "profit_factor": round(float(gross_win / gross_loss), 4) if gross_loss > 0 else None,
        "average_net": float(total / n) if n else None, "mean_r": float(sum(rs) / len(rs)) if rs else None,
        "longest_losing_streak": longest if n else None,
        "max_drawdown": float(max_dd) if starting_capital is not None else None,
        "max_drawdown_pct": float(max_dd_pct) if max_dd_pct is not None else None,
        "today": buckets["day"], "this_week": buckets["week"], "this_month": buckets["month"],
        "costs": costs, "curve": curve,
        "by_strategy": group(lambda t: t.strategy), "by_session": group(lambda t: t.session),
        "by_timeframe": group(lambda t: t.timeframe),
    }


def unrealized(positions: list[dict]) -> dict:
    """Floating P&L of broker positions (profit + swap as reported by the terminal)."""
    rows = [{"ticket": p.get("ticket"), "symbol": p.get("symbol"), "side": "long" if p.get("type") == 0 else "short",
             "volume": p.get("volume"), "open_price": p.get("price_open"), "current": p.get("price_current"),
             "sl": p.get("sl") or None, "tp": p.get("tp") or None, "magic": p.get("magic"),
             "floating": float(_d(p.get("profit")) + _d(p.get("swap"))),
             "opened_at": datetime.fromtimestamp(int(p["time"]), UTC).isoformat() if p.get("time") else None}
            for p in positions]
    return {"positions": rows, "floating_total": float(sum((Decimal(str(r["floating"])) for r in rows), D0))}

"""Accounting accuracy: broker deals, cash flows, costs, performance and drawdown (worked examples)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from trading.accounting import (ClosedTrade, cash_flow_summary, classify_deals, performance, positions_from_deals,
                                unrealized)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # Monday


def deal(ticket, pos, typ, entry, vol, price, profit=0.0, commission=0.0, swap=0.0, fee=0.0, minutes=0, magic=7, reason=None):
    return {"ticket": ticket, "position_id": pos, "type": typ, "entry": entry, "volume": vol, "price": price,
            "profit": profit, "commission": commission, "swap": swap, "fee": fee, "symbol": "XAUUSD", "magic": magic,
            "time": int((T0 + timedelta(minutes=minutes)).timestamp()), "reason": reason, "comment": ""}


DEALS = [
    {"ticket": 1, "type": 2, "profit": 3000.0, "time": int(T0.timestamp()) - 3600, "comment": "deposit"},
    deal(2, 10, 0, 0, 0.10, 4000.0, commission=-0.35, minutes=0),                       # buy in
    deal(3, 10, 1, 1, 0.10, 4006.0, profit=60.0, commission=-0.35, minutes=30, reason=5),  # tp: +60 - 0.70
    deal(4, 11, 1, 0, 0.10, 4010.0, commission=-0.35, minutes=60),                      # sell in
    deal(5, 11, 0, 1, 0.05, 4013.0, profit=-15.0, commission=-0.18, minutes=70),        # partial out
    deal(6, 11, 0, 1, 0.05, 4014.0, profit=-20.0, commission=-0.17, swap=-1.10, minutes=60 * 24 + 5, reason=4),
    {"ticket": 7, "type": 2, "profit": -500.0, "time": int((T0 + timedelta(days=2)).timestamp()), "comment": "withdrawal"},
    {"ticket": 8, "type": 4, "profit": -2.0, "time": int((T0 + timedelta(days=2)).timestamp()), "comment": "charge"},
    {"ticket": 9, "type": 5, "profit": 1.5, "time": int((T0 + timedelta(days=2)).timestamp()), "comment": "correction"},
    deal(10, 12, 0, 0, 0.10, 4020.0, commission=-0.35, minutes=60 * 24 * 3),           # still open
]


def test_cash_flows_are_not_trading_profit():
    c = classify_deals(DEALS)
    assert len(c["cash_flows"]) == 2 and len(c["costs"]) == 1 and len(c["adjustments"]) == 1
    cf = cash_flow_summary(DEALS)
    assert cf["deposits"] == 3000.0 and cf["withdrawals"] == -500.0 and cf["net_cash_flow"] == 2500.0
    assert cf["account_cost_deals"] == -2.0 and cf["adjustments"] == 1.5


def test_positions_net_of_commission_swap_with_partial_exits():
    closed, still_open = positions_from_deals(DEALS, strategy_by_magic={7: "scalp-x"})
    assert [c.trade_id for c in closed] == ["10", "11"] and [o["position_id"] for o in still_open] == [12]
    a, b = closed
    assert a.net == D("59.30") and a.side == "long" and a.exit_reason == "take_profit" and a.strategy == "scalp-x"
    assert b.net == D("-15") - D("20") - D("0.35") - D("0.18") - D("0.17") - D("1.10")  # = -36.80
    assert b.side == "short" and b.volume == D("0.10") and b.exit == D("4013.5")  # volume-weighted exit
    assert b.closed_at == T0 + timedelta(minutes=60 * 24 + 5) and b.exit_reason == "stop_loss"


def test_performance_metrics_and_drawdown_exclude_cash_flows():
    closed, _ = positions_from_deals(DEALS)
    cf = cash_flow_summary(DEALS)
    now = T0 + timedelta(days=3)
    p = performance(closed, starting_capital=D("3000"), now=now, extra_costs=D(str(cf["account_cost_deals"])))
    assert p["closed_trades"] == 2 and p["net_profit"] == pytest.approx(59.30 - 36.80 - 2.0)
    assert p["win_rate_pct"] == 50.0 and p["profit_factor"] == pytest.approx(59.30 / 36.80, rel=1e-4)
    assert p["max_drawdown"] == pytest.approx(36.80) and p["max_drawdown_pct"] == pytest.approx(36.80 / 3059.30 * 100)
    assert p["longest_losing_streak"] == 1
    assert p["costs"]["commission"] == pytest.approx(-1.40) and p["costs"]["swap"] == pytest.approx(-1.10)
    assert p["today"] == 0.0 and p["this_week"] == pytest.approx(59.30 - 36.80)  # both closed this ISO week
    assert p["curve"][-1]["value"] == pytest.approx(3000 + 59.30 - 36.80)  # withdrawal is not a drawdown


def test_no_trades_gives_none_not_zero_for_ratios():
    p = performance([], starting_capital=D("3000"), now=T0)
    assert p["closed_trades"] == 0 and p["win_rate_pct"] is None and p["profit_factor"] is None
    assert p["longest_losing_streak"] is None and p["mean_r"] is None
    assert performance([], starting_capital=None, now=T0)["max_drawdown"] is None


def test_simulated_trade_costs_and_r():
    t = ClosedTrade(trade_id="s", strategy="orb", symbol="XAUUSD", side="long", opened_at=T0, closed_at=T0, volume=D("0.1"),
                    entry=D("1"), exit=D("2"), gross=D("100"), commission=D("-0.7"), swap=D("-1"),
                    spread_slippage=D("-3.3"), planned_risk=D("50"))
    assert t.net == D("95") and t.r == D("1.9")
    p = performance([t], starting_capital=D("10000"), now=T0)
    assert p["costs"]["spread_slippage"] == -3.3 and p["mean_r"] == pytest.approx(1.9)
    assert p["by_strategy"]["orb"]["trades"] == 1


def test_unrealized_uses_broker_profit_and_swap():
    u = unrealized([{"ticket": 1, "symbol": "XAUUSD", "type": 1, "volume": 0.1, "price_open": 4000, "price_current": 3990,
                     "sl": 4010, "tp": 3980, "profit": 100.0, "swap": -0.5, "magic": 7, "time": int(T0.timestamp())}])
    assert u["floating_total"] == 99.5 and u["positions"][0]["side"] == "short"

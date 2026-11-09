"""Account performance view + read-only MT5 account monitor: honest unavailable states, cash flows, mode
separation. Fake terminal only; no runner state is touched."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from command_center.account import account_view, broker_view, live_view
from research.mt5_account_monitor import AccountStore, snapshot

UTC = timezone.utc


class FakeTerminal:
    def __init__(self, *, connected=True, trade_mode=0, deals=(), positions=()):
        self.connected, self.trade_mode, self._deals, self._positions = connected, trade_mode, list(deals), list(positions)
        self.calls = []

    def initialize(self, **_):
        self.calls.append("initialize")
        return self.connected

    def last_error(self):
        return (-10004, "No IPC connection")

    def shutdown(self):
        self.calls.append("shutdown")

    def account_info(self):
        return {"login": 5550524, "server": "MetaQuotes-Demo", "trade_mode": self.trade_mode, "balance": 3059.3,
                "equity": 3071.1, "margin": 41.0, "margin_free": 3030.1, "margin_level": 7490.5, "profit": 11.8,
                "currency": "USD", "leverage": 100, "trade_allowed": True, "password": "never-stored"}

    def terminal_info(self):
        return {"connected": self.connected, "trade_allowed": False, "build": 6308}

    def positions_get(self):
        return self._positions

    def history_deals_get(self, a, b):
        return self._deals

    def order_send(self, *_):  # must never be reachable through the read-only proxy
        raise AssertionError("order_send called")


T = int(datetime(2026, 11, 2, 9, tzinfo=UTC).timestamp())
DEALS = [{"ticket": 1, "type": 2, "profit": 3000.0, "time": T - 60, "comment": "deposit"},
         {"ticket": 2, "position_id": 9, "type": 0, "entry": 0, "volume": 0.1, "price": 4000.0, "profit": 0.0,
          "commission": -0.35, "swap": 0.0, "fee": 0.0, "time": T, "magic": 0, "symbol": "XAUUSD"},
         {"ticket": 3, "position_id": 9, "type": 1, "entry": 1, "volume": 0.1, "price": 4006.0, "profit": 60.0,
          "commission": -0.35, "swap": 0.0, "fee": 0.0, "time": T + 600, "magic": 0, "symbol": "XAUUSD", "reason": 5}]
POS = [{"ticket": 11, "symbol": "XAUUSD", "type": 0, "volume": 0.1, "price_open": 4010, "price_current": 4011.2,
        "sl": 4005, "tp": 4020, "profit": 12.0, "swap": -0.2, "magic": 0, "time": T + 900}]


def _cfg(tmp_path):
    return {"markets": {"xauusd": {"state_dir": str(tmp_path / "none")}}, "strategies": [],
            "account": {"mt5_state_dir": str(tmp_path / "acct"), "execution_state_dir": str(tmp_path / "exec"),
                        "historical_reports": []}}


def test_monitor_records_snapshot_read_only_and_never_stores_password(tmp_path):
    term = FakeTerminal(deals=DEALS, positions=POS)
    store = AccountStore(tmp_path / "acct" / "account.sqlite")
    out = snapshot(store, module=term, now=datetime(2026, 11, 2, 10, tzinfo=UTC))
    assert out == {"connected": True, "balance": 3059.3, "equity": 3071.1, "positions": 1, "deals": 3}
    raw = (tmp_path / "acct" / "account.sqlite").read_bytes()
    assert b"never-stored" not in raw and term.calls[-1] == "shutdown"


def test_monitor_refuses_live_accounts_and_records_disconnection(tmp_path):
    store = AccountStore(tmp_path / "acct" / "account.sqlite")
    assert snapshot(store, module=FakeTerminal(trade_mode=2), now=datetime(2026, 11, 2, 10, tzinfo=UTC))["connected"] is False
    assert snapshot(store, module=FakeTerminal(connected=False), now=datetime(2026, 11, 2, 10, 1, tzinfo=UTC))["connected"] is False
    rows = store.db.execute("SELECT connected, account, error FROM snapshots ORDER BY at").fetchall()
    assert [r[0] for r in rows] == [0, 0] and all(r[1] is None for r in rows)
    assert "LiveTradingForbidden" in rows[0][2] and "Mt5Unavailable" in rows[1][2]


def test_broker_view_unavailable_is_not_zero(tmp_path):
    v = broker_view(_cfg(tmp_path), datetime.now(UTC))
    assert v["available"] is False and v["account"] is None and v["connection"] == "unavailable"
    assert "not as zero" in v["note"]


def test_broker_view_accounting_cash_flows_and_staleness(tmp_path):
    store = AccountStore(tmp_path / "acct" / "account.sqlite")
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    snapshot(store, module=FakeTerminal(deals=DEALS, positions=POS), now=t0)
    v = broker_view(_cfg(tmp_path), datetime.now(UTC))
    assert v["connection"] == "stale" and v["account"]["login"] == "••••524" and v["account"]["is_demo"]
    assert v["cash_flows"]["deposits"] == 3000.0
    assert v["performance"]["net_profit"] == pytest.approx(59.3) and v["performance"]["closed_trades"] == 1
    assert v["reconciliation"]["balance_check"]["ok"] is True  # 3000 deposit + 59.30 trading = 3059.30
    assert v["open_positions"]["floating_total"] == pytest.approx(11.8)
    assert v["terminal"]["algo_trading_enabled"] is False
    snapshot(store, module=FakeTerminal(connected=False), now=datetime.now(UTC))
    v2 = broker_view(_cfg(tmp_path), datetime.now(UTC))
    assert v2["connection"] == "disconnected" and v2["account"]["balance"] == 3059.3  # last good, clearly labelled
    assert "currently disconnected" in v2["note"]


def test_modes_are_separate_and_live_is_disabled(tmp_path):
    lv = live_view()
    assert lv["enabled"] is False and lv["available"] is False and all(g["status"] == "not met" for g in lv["approval_gate"])
    cfg = _cfg(tmp_path)
    assert account_view(cfg, "historical")["available"] is False
    assert account_view(cfg, "broker")["money"].startswith("demo funds")
    with pytest.raises(ValueError):
        account_view(cfg, "combined")


def test_account_api_is_get_only(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from command_center.api import create_app
    from command_center import services
    base = json.loads(Path("command_center/config.json").read_text(encoding="utf-8"))
    cfg = {**base, **_cfg(tmp_path), "markets": base["markets"], "strategies": []}
    p = tmp_path / "cc.json"
    p.write_text(json.dumps(cfg))
    services.CACHE._items.clear()
    c = TestClient(create_app(p), base_url="http://127.0.0.1")
    assert c.get("/api/account/live").json()["enabled"] is False
    assert c.get("/api/account/combined").status_code == 404
    for verb in ("post", "put", "delete"):
        assert getattr(c, verb)("/api/account/broker").status_code == 405

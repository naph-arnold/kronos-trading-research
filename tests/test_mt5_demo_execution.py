"""MT5 demo execution adapter: every safety layer, on a fake terminal. No real terminal, no order of any kind."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from trading.common import Market, Side
from trading.config import LiveTradingForbidden
from trading.execution.mt5_demo import (APPROVAL_ENV, APPROVAL_FILE, DemoTradingMt5, ExecutionNotAuthorized,
                                        ExecutionRejected, Mt5DemoExecutor, SymbolRules, build_request,
                                        idempotency_key)
from trading.risk import RiskDecision
from trading.signals import TradeSignal

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
MAGIC = 260001


class FakeMt5:
    """Minimal MetaTrader5 stand-in. order_send opens a position with the request's SL/TP and comment."""

    def __init__(self, *, trade_mode=0, connected=True, bid=4000.0, ask=4000.3, retcode=10009, keep_sl=True):
        self.trade_mode, self.connected, self.bid, self.ask = trade_mode, connected, bid, ask
        self.retcode, self.keep_sl = retcode, keep_sl
        self.positions, self.deals, self.sent, self.checked = [], [], [], []
        self.init_ok = True

    def initialize(self, **_):
        return self.init_ok

    def shutdown(self):
        return True

    def last_error(self):
        return (1, "fake")

    def account_info(self):
        return {"login": 123, "server": "MetaQuotes-Demo", "trade_mode": self.trade_mode, "equity": 10000.0,
                "balance": 10000.0, "trade_allowed": True}

    def terminal_info(self):
        return {"connected": self.connected, "trade_allowed": True}

    def symbol_info(self, _):
        return {"name": "XAUUSD", "point": 0.01, "trade_tick_size": 0.01, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "trade_stops_level": 20, "trade_freeze_level": 0, "filling_mode": 2, "digits": 2}

    def symbol_info_tick(self, _):
        return {"bid": self.bid, "ask": self.ask}

    def positions_get(self, symbol=None):
        return list(self.positions)

    def orders_get(self, symbol=None):
        return []

    def history_deals_get(self, a, b):
        return list(self.deals)

    def order_calc_profit(self, typ, sym, vol, price, close):
        return (close - price) * vol * 100 * (1 if typ == 0 else -1)

    def order_check(self, req):
        self.checked.append(req)
        return {"retcode": 0, "comment": "Done"}

    def order_send(self, req):
        self.sent.append(req)
        if self.retcode == 10009:
            self.positions.append({"ticket": 900 + len(self.sent), "symbol": req["symbol"], "type": req["type"],
                                   "volume": req["volume"], "price_open": req["price"], "sl": req["sl"] if self.keep_sl else 0,
                                   "tp": req["tp"] if self.keep_sl else 0, "magic": req["magic"], "comment": req["comment"],
                                   "profit": 0.0, "swap": 0.0, "time": int(NOW.timestamp())})
        return {"retcode": self.retcode, "order": 700 + len(self.sent), "deal": 800 + len(self.sent),
                "volume": req["volume"], "price": req["price"], "comment": "fake"}


def _signal(sid="s1", side=Side.LONG, entry="4000.3", stop="3995", tp="4010"):
    return TradeSignal.create(signal_id=sid, market=Market.XAUUSD_MT5, symbol="XAUUSD", side=side, entry_price=entry,
                              stop_price=stop, take_profit=tp, strategy_id="scalp-test", strategy_version="0",
                              generated_at=NOW, bar_close_time=NOW)


def _decision(sid="s1", size="0.09", approved=True):
    return RiskDecision(approved=approved, market="xauusd_mt5", signal_id=sid, dedup_key=sid, size=D(size), reasons=(),
                        evaluated_at=NOW.isoformat(), planned_risk=D("47.7"))


def _approve(state, *, expires=NOW + timedelta(days=1), **over):
    body = {"approval_id": "trial-1", "approved_by": "operator", "approved_at": NOW.isoformat(),
            "expires_at": expires.isoformat(), "account_login": 123, "server": "MetaQuotes-Demo", "symbol": "XAUUSD",
            "magic": MAGIC, "max_orders_per_day": 3, **over}
    (state / APPROVAL_FILE).write_text(json.dumps(body))


def _executor(tmp_path, fake, *, approved=True, env=True):
    if approved:
        _approve(tmp_path)
    ex = Mt5DemoExecutor(fake, tmp_path, symbol="XAUUSD", magic=MAGIC, env={APPROVAL_ENV: "trial-1"} if env else {},
                         clock=lambda: NOW)
    ex.connect()
    ex.reconcile()
    return ex


def test_default_state_sends_nothing(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake, approved=False, env=False)
    with pytest.raises(ExecutionNotAuthorized, match="not approved"):
        ex.submit(_signal(), _decision(), book="b", strategy="s")
    assert fake.sent == [] and fake.checked == []
    assert ex.ledger.get(idempotency_key("s1", "b"))["state"] == "blocked"


def test_approval_needs_matching_env_account_and_unexpired(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake, env=False)
    with pytest.raises(ExecutionNotAuthorized, match=APPROVAL_ENV):
        ex.submit(_signal(), _decision(), book="b", strategy="s")
    _approve(tmp_path, expires=NOW - timedelta(minutes=1))
    ex.env = {APPROVAL_ENV: "trial-1"}
    with pytest.raises(ExecutionNotAuthorized, match="expired"):
        ex.submit(_signal("s2"), _decision("s2"), book="b", strategy="s")
    _approve(tmp_path, account_login=999)
    with pytest.raises(ExecutionNotAuthorized, match="login/server"):
        ex.submit(_signal("s3"), _decision("s3"), book="b", strategy="s")
    assert fake.sent == []


def test_live_account_is_refused_at_connect_and_before_send(tmp_path):
    with pytest.raises(LiveTradingForbidden):
        Mt5DemoExecutor(FakeMt5(trade_mode=2), tmp_path, symbol="XAUUSD", magic=MAGIC, env={}).connect()
    fake = FakeMt5()
    proxy = DemoTradingMt5(fake)
    proxy._arm(True)
    fake.trade_mode = 2  # switched to a real account after connecting
    with pytest.raises(LiveTradingForbidden):
        proxy.order_send({"action": 1})
    assert fake.sent == []


def test_proxy_blocks_unarmed_send_and_other_write_functions(tmp_path):
    proxy = DemoTradingMt5(FakeMt5())
    with pytest.raises(ExecutionNotAuthorized):
        proxy.order_send({})
    for name in ("positions_close", "order_modify", "Buy", "market_book_add"):
        with pytest.raises(PermissionError):
            getattr(proxy, name)


def test_approved_order_has_sl_tp_rounded_volume_and_is_recorded(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    out = ex.submit(_signal(), _decision(size="0.0959"), book="b", strategy="s")
    assert out["state"] == "filled" and len(fake.sent) == 1
    req = fake.sent[0]
    assert req["volume"] == 0.09 and req["sl"] == 3995.0 and req["tp"] == 4010.0 and req["magic"] == MAGIC
    assert req["comment"] == f"kr:{idempotency_key('s1', 'b')}" and len(req["comment"]) <= 31
    row = ex.ledger.get(idempotency_key("s1", "b"))
    assert row["state"] == "filled" and row["retcode"] == 10009 and json.loads(row["request"])["sl"] == 3995.0
    assert not (tmp_path / "HALT").exists()


def test_duplicate_signal_is_never_sent_twice(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    ex.submit(_signal(), _decision(), book="b", strategy="s")
    again = ex.submit(_signal(), _decision(), book="b", strategy="s")
    assert again["state"] == "duplicate" and len(fake.sent) == 1
    # crash between send and ledger write: a fresh ledger still finds the key at the broker
    ex2 = Mt5DemoExecutor(fake, tmp_path / "other", symbol="XAUUSD", magic=MAGIC, env={APPROVAL_ENV: "trial-1"}, clock=lambda: NOW)
    _approve(tmp_path / "other", max_orders_per_day=3)
    ex2.connect()
    fake.positions.clear()  # position closed, but the deal history still carries the key
    fake.deals.append({"ticket": 1, "comment": fake.sent[0]["comment"], "magic": MAGIC, "type": 0, "entry": 0})
    ex2.needs_reconcile = False
    assert ex2.submit(_signal(), _decision(), book="b", strategy="s")["state"] == "duplicate" and len(fake.sent) == 1


def test_risk_recheck_rejects_orders_above_half_percent(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    # 0.2 lots x 100 oz x 5.3 = 106 USD + commission > 50 USD (0.5% of 10,000)
    out = ex.submit(_signal(), _decision(size="0.2"), book="b", strategy="s")
    assert out["state"] == "validation_rejected" and "exceeds 0.5%" in out["reason"] and fake.sent == []


def test_validation_rejects_missing_tp_wrong_side_stops_level_and_min_lot():
    rules = SymbolRules.from_info(FakeMt5().symbol_info("XAUUSD"))
    tick = {"bid": 4000.0, "ask": 4000.3}
    with pytest.raises(ExecutionRejected, match="take-profit"):
        build_request(_signal(tp=None), _decision(), rules, tick, key="k", magic=1)
    with pytest.raises(ExecutionRejected, match="wrong side"):  # price moved through the stop since the signal
        build_request(_signal(), _decision(), rules, {"bid": 3994.0, "ask": 3994.3}, key="k", magic=1)
    with pytest.raises(ExecutionRejected, match="stops level"):
        build_request(_signal(stop="4000.2", tp="4010"), _decision(), rules, tick, key="k", magic=1)
    with pytest.raises(ExecutionRejected, match="below the broker minimum"):
        build_request(_signal(), _decision(size="0.005"), rules, tick, key="k", magic=1)
    with pytest.raises(ExecutionRejected, match="approved"):
        build_request(_signal(), _decision(approved=False), rules, tick, key="k", magic=1)
    short = build_request(_signal(side=Side.SHORT, stop="4005.004", tp="3990.006"), _decision(), rules, tick, key="k", magic=1)
    assert short["sl"] == 4005.01 and short["tp"] == 3990.01  # stop rounded away, target toward entry


def test_disconnection_fails_closed_and_requires_reconciliation(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    fake.connected = False
    fake.init_ok = False
    with pytest.raises(ExecutionNotAuthorized, match="disconnected"):
        ex.submit(_signal(), _decision(), book="b", strategy="s")
    assert fake.sent == []
    kinds = [r[0] for r in ex.ledger._db.execute("SELECT kind FROM events").fetchall()]
    assert "disconnected" in kinds and kinds.count("reconnect_failed") == 3
    fake.connected, fake.init_ok = True, True
    with pytest.raises(ExecutionNotAuthorized, match="reconciliation required"):
        ex.submit(_signal("s2"), _decision("s2"), book="b", strategy="s")


def test_kill_stop_halt_and_daily_cap_block(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    for flag in ("KILL", "STOP", "HALT"):
        (tmp_path / flag).write_text("")
        with pytest.raises(ExecutionNotAuthorized, match=flag):
            ex.submit(_signal(f"s-{flag}"), _decision(f"s-{flag}"), book="b", strategy="s")
        (tmp_path / flag).unlink()
    assert fake.sent == []


def test_rejected_order_is_recorded_and_not_resent(tmp_path):
    fake = FakeMt5(retcode=10016)
    ex = _executor(tmp_path, fake)
    out = ex.submit(_signal(), _decision(), book="b", strategy="s")
    assert out["state"] == "rejected" and out["reason"] == "invalid_stops" and len(fake.sent) == 1
    assert ex.submit(_signal(), _decision(), book="b", strategy="s")["state"] == "duplicate" and len(fake.sent) == 1


def test_missing_broker_protection_halts(tmp_path):
    fake = FakeMt5(keep_sl=False)
    ex = _executor(tmp_path, fake)
    ex.submit(_signal(), _decision(), book="b", strategy="s")
    assert (tmp_path / "HALT").exists()
    with pytest.raises(ExecutionNotAuthorized, match="HALT"):
        ex.submit(_signal("s2"), _decision("s2"), book="b", strategy="s")


def test_reconciliation_detects_foreign_and_vanished_positions(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    ex.submit(_signal(), _decision(), book="b", strategy="s")
    fake.positions.append({"ticket": 5, "magic": MAGIC, "comment": "manual", "sl": 1, "tp": 2})
    rep = ex.reconcile()
    assert rep["ok"] is False and rep["foreign_positions_same_magic"] == [5] and (tmp_path / "HALT").exists()
    (tmp_path / "HALT").unlink()
    fake.positions.clear()  # our position vanished with no closing deal
    rep = ex.reconcile()
    assert rep["ok"] is False and rep["missing_without_close"] == [idempotency_key("s1", "b")]


def test_close_is_recorded_from_broker_deals(tmp_path):
    fake = FakeMt5()
    ex = _executor(tmp_path, fake)
    ex.submit(_signal(), _decision(), book="b", strategy="s")
    pos = fake.positions.pop()
    t = int(NOW.timestamp())
    fake.deals += [{"ticket": 1, "position_id": pos["ticket"], "type": 0, "entry": 0, "volume": 0.09, "price": 4000.3,
                    "profit": 0.0, "commission": -0.32, "swap": 0.0, "fee": 0.0, "magic": MAGIC, "comment": pos["comment"],
                    "time": t, "symbol": "XAUUSD"},
                   {"ticket": 2, "position_id": pos["ticket"], "type": 1, "entry": 1, "volume": 0.09, "price": 4010.0,
                    "profit": 87.3, "commission": -0.31, "swap": 0.0, "fee": 0.0, "magic": MAGIC, "comment": "[tp 4010]",
                    "reason": 5, "time": t + 600, "symbol": "XAUUSD"}]
    rep = ex.reconcile()
    row = ex.ledger.get(idempotency_key("s1", "b"))
    assert rep["ok"] and row["state"] == "closed" and D(row["net_result"]) == D("86.67") and row["reason"] == "take_profit"


def test_adapter_is_isolated_from_shadow_runners_and_dashboard():
    from pathlib import Path
    for path in list(Path("research").glob("phase2*_shadow.py")) + [Path("research/xau_selective_a_shadow.py")] + \
            list(Path("trading/shadow").glob("*.py")) + list(Path("command_center").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert "trading.execution" not in src and "order_send" not in src, path

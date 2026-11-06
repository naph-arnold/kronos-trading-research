"""Pre-trade gate: duplicates, audit, AI cannot override, and demo-only guard."""
from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from trading.audit import AuditLog
from trading.common import Market
from trading.config import (ExecutionMode, LiveTradingForbidden, load_demo_environment, verify_binance_endpoint,
                            verify_mt5_demo_account)
from trading.daily import DailyLossLedger, DayBoundary
from dataclasses import replace

from trading.gate import PreTradeGate
from trading.instruments import VerifiedInstrument
from trading.ledger import SignalLedger
from trading_helpers import ENV, NOW, XAU_CALENDAR, account, breaker, limits, xau_quote, xau_signal, xau_spec

XAU = Market.XAUUSD_MT5
ROOT = Path(__file__).resolve().parents[1]
ALL_AGENTS_APPROVE = {role: "approve" for role in
                      ("scout", "news", "chart", "kronos_analyst", "skeptic", "coordinator")}


def daily_ledger(tmp_path):
    return DailyLossLedger(tmp_path / "daily.sqlite", market=XAU, boundary=DayBoundary.server(XAU_CALENDAR.server_tz),
                           halt_fraction=D("0.015"))


VERIFIED_CALENDAR = replace(XAU_CALENDAR, verification="broker_verified")  # test fixture, not broker evidence


def no_disruption(now):
    return None


def make_gate(tmp_path, ledger=None, audit=None, daily=None, market_guard=no_disruption, calendar=VERIFIED_CALENDAR):
    return PreTradeGate(environment=ENV, limits=limits(XAU), breaker=breaker(XAU),
                        ledger=ledger or SignalLedger(tmp_path / "ledger.sqlite"),
                        audit=audit or AuditLog(tmp_path / "audit.jsonl"),
                        daily_ledger=daily or daily_ledger(tmp_path), session_calendar=calendar,
                        market_guard=market_guard)


def submit(gate, signal=None, **overrides):
    kwargs = dict(account=account(XAU), instrument=VerifiedInstrument(xau_spec(), "test-fixture", NOW, True),
                  quote=xau_quote(), now=NOW)
    kwargs.update(overrides)
    return gate.evaluate(signal or xau_signal(), **kwargs)


def test_duplicate_signal_id_and_same_bar_signal_rejected(tmp_path):
    gate = make_gate(tmp_path)
    assert submit(gate).approved
    again = submit(gate)
    assert not again.approved and "Duplicate" in " ".join(again.reasons)
    renamed = submit(gate, xau_signal(signal_id="xau-renamed"))
    assert not renamed.approved and "Duplicate" in " ".join(renamed.reasons)


def test_duplicate_ledger_survives_restart(tmp_path):
    assert submit(make_gate(tmp_path)).approved
    reopened = make_gate(tmp_path)
    assert not submit(reopened).approved


def test_rejected_signal_is_not_claimed(tmp_path):
    gate = make_gate(tmp_path)
    assert not submit(gate, account=account(XAU, reconciled=False)).approved
    assert submit(gate).approved


def test_unknown_broker_state_blocks_even_if_all_agents_approve(tmp_path):
    gate = make_gate(tmp_path)
    decision = submit(gate, account=account(XAU, reconciled=False), advisory=ALL_AGENTS_APPROVE)
    assert not decision.approved
    assert "not reconciled" in " ".join(decision.reasons)


def test_every_decision_is_audited_with_verifiable_chain(tmp_path):
    gate = make_gate(tmp_path)
    submit(gate, advisory=ALL_AGENTS_APPROVE)
    submit(gate)
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert len(audit.verify()) == 64
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"advisory_inputs_not_used_by_risk"' in lines[0] and '"live_trading_enabled":false' in lines[0]
    audit.path.write_text(lines[0].replace('"approved":true', '"approved":false') + "\n" + lines[1] + "\n",
                          encoding="utf-8")
    with pytest.raises(ValueError):
        audit.verify()


class _BrokenAudit:
    def append(self, record):
        raise OSError("disk full")


def test_audit_failure_rejects_and_trips_breaker(tmp_path):
    gate = make_gate(tmp_path, audit=_BrokenAudit())
    decision = submit(gate)
    assert not decision.approved and "Audit write failed" in " ".join(decision.reasons)
    assert gate.breaker.is_tripped
    follow_up = submit(gate, xau_signal(signal_id="xau-2"))
    assert not follow_up.approved and "Circuit breaker: Audit write failed" in " ".join(follow_up.reasons)


def test_gate_requires_matching_breaker_market(tmp_path):
    with pytest.raises(ValueError):
        PreTradeGate(environment=ENV, limits=limits(XAU), breaker=breaker(Market.BTCUSDT_BINANCE_SPOT),
                     ledger=SignalLedger(), audit=AuditLog(tmp_path / "a.jsonl"), daily_ledger=daily_ledger(tmp_path),
                     market_guard=no_disruption)
    with pytest.raises(ValueError):
        PreTradeGate(environment=ENV, limits=limits(XAU, daily_loss_halt_fraction=D("0.02")), breaker=breaker(XAU),
                     ledger=SignalLedger(), audit=AuditLog(tmp_path / "b.jsonl"), daily_ledger=daily_ledger(tmp_path),
                     market_guard=no_disruption)


# --- Demo-only environment -----------------------------------------------------

def test_execution_mode_has_no_live_member():
    assert [m.value for m in ExecutionMode] == ["demo"]
    with pytest.raises(ValueError):
        ExecutionMode("live")


@pytest.mark.parametrize("env", [
    {},
    {"EXECUTION_MODE": "live"},
    {"EXECUTION_MODE": "DEMO"},
    {"EXECUTION_MODE": "demo", "LIVE_TRADING_ENABLED": "true"},
    {"EXECUTION_MODE": "demo", "TRADING_MODE": "live"},
    {"EXECUTION_MODE": "demo", "BINANCE_API_KEY": "prod-key"},
    {"EXECUTION_MODE": "demo", "MT5_REAL_PASSWORD": "x"},
    {"EXECUTION_MODE": "demo", "BINANCE_TESTNET_BASE_URL": "https://api.binance.com"},
    {"EXECUTION_MODE": "demo", "BINANCE_TESTNET_BASE_URL": "http://testnet.binance.vision"},
    {"EXECUTION_MODE": "demo", "BINANCE_TESTNET_BASE_URL": "https://testnet.binance.vision@api.binance.com"},
    {"EXECUTION_MODE": "demo", "BINANCE_TESTNET_BASE_URL": "https://testnet.binance.vision.evil.com"},
])
def test_non_demo_environments_fail_closed(env):
    with pytest.raises(LiveTradingForbidden):
        load_demo_environment(env)


def test_demo_environment_accepted():
    env = load_demo_environment({"EXECUTION_MODE": "demo", "LIVE_TRADING_ENABLED": "false",
                                 "TRADING_MODE": "paper", "BINANCE_API_KEY": ""})
    assert env.mode is ExecutionMode.DEMO and env.live_trading_enabled is False
    assert env.binance_base_url == "https://testnet.binance.vision"
    assert verify_binance_endpoint("https://testnet.binance.vision/api") == "https://testnet.binance.vision"


@pytest.mark.parametrize("mode", [1, 2, True, "0", None])
def test_mt5_non_demo_account_modes_rejected(mode):
    with pytest.raises(LiveTradingForbidden):
        verify_mt5_demo_account(mode)
    verify_mt5_demo_account(0)


def test_trading_package_contains_no_order_routing():
    """No executor before Phase 3: no order endpoints, MT5 order calls, POST/DELETE, or HTTP client libraries."""
    forbidden = ("order_send", "order_check", "/api/v3/order", "api.binance.com", "fapi.binance", "sapi/",
                 'method="POST"', 'method="DELETE"', "import requests", "import httpx", "import ccxt")
    files = sorted((ROOT / "trading").rglob("*.py"))
    assert any(p.parent.name == "connectors" for p in files)
    for path in files:
        source = path.read_text(encoding="utf-8")
        ast.parse(source)
        assert not any(token in source for token in forbidden), path
        if path.name != "mt5.py" or path.parent.name != "connectors":
            assert "import MetaTrader5" not in source, path


def test_size_is_decimal_not_float(tmp_path):
    decision = submit(make_gate(tmp_path))
    assert isinstance(decision.size, D)


def test_gate_uses_ledger_day_start_and_latches_halt(tmp_path):
    from datetime import timedelta
    gate = make_gate(tmp_path)
    # Adapter-supplied day_start_equity is ignored; the ledger's first observation (10000) is used.
    assert submit(gate, account=account(XAU, day_start_equity=D("1"))).approved
    later = NOW + timedelta(seconds=1)
    down = submit(gate, xau_signal(signal_id="x2", bar_close_time=NOW.replace(second=0) - timedelta(hours=1)),
                  account=account(XAU, equity=D("9840"), snapshot_at=later), now=later)
    assert not down.approved and "Daily loss halt" in " ".join(down.reasons)
    recovered_at = NOW + timedelta(seconds=2)
    recovered = submit(gate, xau_signal(signal_id="x3", bar_close_time=NOW.replace(second=0) - timedelta(hours=2)),
                       account=account(XAU, equity=D("10100"), snapshot_at=recovered_at), now=recovered_at)
    assert not recovered.approved and "latched" in " ".join(recovered.reasons)


def test_gate_rejects_out_of_order_account_snapshots(tmp_path):
    from datetime import timedelta
    gate = make_gate(tmp_path)
    assert submit(gate).approved
    older = submit(gate, xau_signal(signal_id="x9", bar_close_time=NOW.replace(second=0) - timedelta(hours=1)),
                   account=account(XAU, snapshot_at=NOW - timedelta(seconds=1)))
    assert not older.approved and "Daily loss accounting unavailable" in " ".join(older.reasons)


def test_no_ai_provider_in_deterministic_decision_path():
    """AI agents (Grok), Kronos and Claude are advisory; the trading package must not call any of them."""
    forbidden = ("import openai", "import anthropic", "from openai", "from anthropic", "xai_sdk", "api.x.ai",
                 "from model", "import model", "import torch", "research.six_agent_desk import decide",
                 "research.forecast")
    for path in (ROOT / "trading").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.parent.name == "agents":  # contracts may *name* the Grok provider but never call it
            assert not any(t in source for t in ("import openai", "import anthropic", "xai_sdk", "urlopen",
                                                  "import requests", "import httpx")), path
            continue
        assert not any(token in source for token in forbidden), path
        assert "trading.agents" not in source, f"{path} must not depend on AI agent contracts"


def test_architecture_docs_state_grok_kronos_claude_roles():
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for text in (claude_md, architecture):
        assert "official xai grok api" in text.lower()
        assert "Kronos" in text and "Claude Code" in text


@pytest.mark.parametrize("instrument,reason", [
    (lambda: xau_spec(), "not verified"),
    (lambda: VerifiedInstrument(xau_spec(), "fixture", NOW, False), "not verified"),
    (lambda: VerifiedInstrument(xau_spec(), "fixture", NOW - timedelta(days=2), True), "stale"),
])
def test_gate_requires_verified_recent_instrument_spec(tmp_path, instrument, reason):
    decision = submit(make_gate(tmp_path), instrument=instrument())
    assert not decision.approved and reason in " ".join(decision.reasons)


def test_gate_requires_market_guard_and_broker_verified_sessions(tmp_path):
    missing_guard = submit(make_gate(tmp_path / "a", market_guard=None))
    assert not missing_guard.approved and "No market-data/calendar guard" in " ".join(missing_guard.reasons)
    template = submit(make_gate(tmp_path / "b", calendar=XAU_CALENDAR))
    assert not template.approved and "not verified against the broker" in " ".join(template.reasons)
    disrupted = submit(make_gate(tmp_path / "c", market_guard=lambda now: "abnormal spread"))
    assert not disrupted.approved and "abnormal spread" in " ".join(disrupted.reasons)

"""Six Grok agents, advisory-only (Phase 4 preparation): strict output contracts, fail-closed Coordinator,
activation gate, official-endpoint-only transport, key hygiene, call budget, separate store, and read-only
access to shadow state. No network: the xAI transport is exercised with a fake opener only."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from research.grok_advisory import (AdvisoryStore, DryRunTransport, ReplayTransport, XaiTransport, calendar_snapshot,
                                    make_transport, recent_signals, run_desk, signal_context)
from trading.agents.desk import PROVIDER, SPECIALISTS, AdvisoryContext, parse_role_output
from trading.agents.mtf_advisory import GrokNotActivated
from trading.common import ValidationError
from trading.marketdata import Bar
from trading.shadow.store import ShadowStore

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
CLOSE = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
NOW = CLOSE + timedelta(minutes=2)
SUBJECT = "xau_orb:orb-20261117T1500-long"


def _shadow_db(tmp_path: Path) -> Path:
    path = tmp_path / "shadow.sqlite"
    store = ShadowStore(path)
    with store.transaction():
        for i in range(30):
            t = CLOSE - timedelta(minutes=15 * (30 - i))
            p = D("4000") + i
            store.add_bar(Bar.create(symbol="XAUUSD", interval="15m", open_time=t, open=p, high=p + 2, low=p - 2,
                                     close=p + 1), "live")
        store.record_signal("orb_newyork", "orb-20261117T1500-long", CLOSE, "evaluated", {"side": "long"})
        store.record_decision("orb_newyork.e10000.c7", "orb-20261117T1500-long", dedup_key="k", evaluated_at=CLOSE,
                              approved=False, size=D("0"), planned_risk=None, reasons=["Spread too wide"], context={})
    store.close()
    return path


def _ctx(tmp_path, kronos=None) -> AdvisoryContext:
    db = _shadow_db(tmp_path)
    (sig,) = recent_signals(db, signal_id="orb-20261117T1500-long")
    return signal_context(db, sig, runner="xau_orb", market="xauusd_mt5", now=NOW, kronos=kronos)


def payload(role: str, /, **over) -> dict:
    base = {"schema_version": 1, "role": role, "provider": PROVIDER, "model": "grok-test", "assessed_at": NOW.isoformat(),
            "subject": {"kind": "signal", "id": SUBJECT}, "assessment": f"{role} view", "evidence": [
                "signal:orb-20261117T1500-long"], "concerns": [], "confidence": 0.4, "recommendation": "caution",
            "valid_for_minutes": 60}
    if role == "kronos_analyst":
        base.update(recommendation="no_view", kronos_forecast_id=None)
    if role == "coordinator":
        base.update(role_inputs=list(SPECIALISTS), disagreements=["skeptic vs chart"])
    base.update(over)
    return base


def news_payload(**over) -> dict:
    from trading.agents.news import PROVIDER as NEWS_PROVIDER
    base = {"schema_version": 1, "role": "news", "provider": NEWS_PROVIDER, "model": "grok-test",
            "assessed_at": NOW.isoformat(), "markets": ["xauusd_mt5"], "event_ids": [], "headlines": [],
            "interpretation": "No scheduled event within the next hours.", "implications": {}, "geopolitical_risks": [],
            "abnormal_conditions": [], "conflicts": [], "confidence": 0.5, "recommendation": "no_view",
            "valid_for_minutes": 60}
    base.update(over)
    return base


# --------------------------------------------------------------------------- contracts

def test_valid_specialist_output_parses_and_is_advisory_only(tmp_path):
    out = parse_role_output(payload("scout"), role="scout", context=_ctx(tmp_path), now=NOW)
    assert out.advisory_only and out.recommendation == "caution" and out.evidence == ("signal:orb-20261117T1500-long",)


@pytest.mark.parametrize("bad,match", [
    ({"stop": 3990}, "controlled fields"), ({"concerns": [], "extra": {"size": 1}}, "controlled fields"),
    ({"risk_fraction": 0.01}, "controlled fields"), ({"execution_mode": "live"}, "controlled fields"),
    ({"approve_trade": True}, "controlled fields"), ({"side": "short"}, "controlled fields"),
    ({"provider": "Claude"}, "official xAI"), ({"role": "chart"}, "schema"),
    ({"subject": {"kind": "signal", "id": "other"}}, "different subject"),
    ({"evidence": ["bar:invented"]}, "not in its context"),
    ({"assessed_at": (NOW - timedelta(hours=5)).isoformat()}, "stale"),
    ({"assessed_at": (NOW + timedelta(minutes=5)).isoformat()}, "future"),
    ({"recommendation": "buy"}, "recommendation"), ({"confidence": 1.5}, "confidence"),
    ({"valid_for_minutes": 1000}, "valid_for_minutes"), ({"assessment": "x" * 2001}, "assessment"),
    ({"levels": [4010.0]}, "Chart role"), ({"kronos_forecast_id": "k1"}, "Kronos Analyst"),
])
def test_invalid_or_overreaching_outputs_are_rejected(tmp_path, bad, match):
    with pytest.raises(ValidationError, match=match):
        parse_role_output(payload("scout", **bad), role="scout", context=_ctx(tmp_path), now=NOW)


def test_chart_levels_and_kronos_references_must_come_from_the_context(tmp_path):
    ctx = _ctx(tmp_path, kronos={"kronos:xau:20261117T1500": {"predicted_close": 4031.0}})
    lo, hi = ctx.price_range
    assert parse_role_output(payload("chart", levels=[lo, hi]), role="chart", context=ctx, now=NOW).extra["levels"]
    with pytest.raises(ValidationError, match="inside the supplied price range"):
        parse_role_output(payload("chart", levels=[hi + 100]), role="chart", context=ctx, now=NOW)
    ok = payload("kronos_analyst", kronos_forecast_id="kronos:xau:20261117T1500", recommendation="caution")
    assert parse_role_output(ok, role="kronos_analyst", context=ctx, now=NOW).extra["kronos_forecast_id"]
    with pytest.raises(ValidationError, match="not supplied"):
        parse_role_output(payload("kronos_analyst", kronos_forecast_id="invented"), role="kronos_analyst",
                          context=ctx, now=NOW)
    with pytest.raises(ValidationError, match="must give no_view"):
        parse_role_output(payload("kronos_analyst", recommendation="caution"), role="kronos_analyst",
                          context=_ctx(tmp_path / "b"), now=NOW)


def test_coordinator_must_report_insufficient_inputs_when_a_specialist_is_missing(tmp_path):
    ctx = _ctx(tmp_path)
    four = {r: parse_role_output(payload(r), role=r, context=ctx, now=NOW) for r in ("scout", "chart", "skeptic")}
    with pytest.raises(ValidationError, match="exactly the validated"):
        parse_role_output(payload("coordinator"), role="coordinator", context=ctx, now=NOW, validated_inputs=four)
    with pytest.raises(ValidationError, match="insufficient_inputs"):
        parse_role_output(payload("coordinator", role_inputs=sorted(four)), role="coordinator", context=ctx, now=NOW,
                          validated_inputs=four)


# --------------------------------------------------------------------------- activation gate and transport

def test_xai_transport_requires_every_activation_step():
    cfg = json.loads((ROOT / "research/configs/grok_advisory.json").read_text())
    assert cfg["activated"] is False and cfg["model"] is None and cfg["base_url"] == "https://api.x.ai"
    env = {"GROK_AGENTS_ACTIVATED": "1", "XAI_API_KEY": "k"}
    for config, e, approved in ((cfg, env, True), ({**cfg, "activated": True}, {}, True),
                                ({**cfg, "activated": True}, env, False)):
        with pytest.raises(GrokNotActivated):
            make_transport(config, "xai", approved=approved, env=e)
    with pytest.raises(ValueError, match="model"):
        make_transport({**cfg, "activated": True}, "xai", approved=True, env=env)
    t = make_transport({**cfg, "activated": True, "model": "grok-test"}, "xai", approved=True, env=env)
    assert isinstance(t, XaiTransport) and isinstance(make_transport(cfg, "dry-run", approved=False), DryRunTransport)
    with pytest.raises(ValueError, match="official xAI"):
        XaiTransport(model="m", base_url="https://api.openai.com")
    with pytest.raises(ValueError, match="official xAI"):
        XaiTransport(model="m", base_url="https://api.x.ai.evil.example")


class FakeOpener:
    def __init__(self, content: str | None = None, fail: Exception | None = None):
        self.content, self.fail, self.requests = content, fail, []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.fail:
            raise self.fail
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


def test_xai_request_shape_and_key_never_leaks(monkeypatch):
    secret = "xai-SECRET-123"
    monkeypatch.setenv("XAI_API_KEY", secret)
    opener = FakeOpener(content='{"ok": true}')
    t = XaiTransport(model="grok-test", opener=opener)
    assert t.complete("scout", "sys", '{"subject": {"id": "s"}}') == '{"ok": true}'
    (req,) = opener.requests
    assert req.full_url == "https://api.x.ai/v1/chat/completions" and req.get_method() == "POST"
    body = json.loads(req.data)
    assert body["model"] == "grok-test" and body["temperature"] == 0 and "tools" not in body
    assert req.get_header("Authorization") == f"Bearer {secret}"
    failing = XaiTransport(model="grok-test", opener=FakeOpener(fail=OSError(f"boom {secret}")))
    with pytest.raises(GrokNotActivated) as exc:
        failing.complete("scout", "sys", "{}")
    assert secret not in str(exc.value) and "***" in str(exc.value)
    monkeypatch.delenv("XAI_API_KEY")
    with pytest.raises(GrokNotActivated, match="XAI_API_KEY"):
        t.complete("scout", "sys", "{}")


# --------------------------------------------------------------------------- desk runs

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_replayed_desk_records_advisory_summary_and_never_touches_shadow_state(tmp_path):
    from trading.marketcal.events import EventStore
    db = _shadow_db(tmp_path)
    cal = tmp_path / "calendar.sqlite"
    EventStore(cal)._db.close()
    before = _sha(db), _sha(cal)
    (sig,) = recent_signals(db, signal_id="orb-20261117T1500-long")
    ctx = signal_context(db, sig, runner="xau_orb", market="xauusd_mt5", now=NOW)
    replies = {r: payload(r) for r in SPECIALISTS if r != "news"}
    replies["news"] = news_payload()
    replies["coordinator"] = payload("coordinator", recommendation="caution")
    store = AdvisoryStore(tmp_path / "advisory" / "advisory.sqlite")
    events = calendar_snapshot(cal, tmp_path)
    summary = run_desk(ctx, ReplayTransport(replies), store, runner="xau_orb", now=NOW, model="grok-test",
                       event_store=events)
    events._db.close()
    assert summary["source"] == "grok" and summary["recommendation"] == "caution" and summary["advisory_only"]
    assert summary["valid_inputs"] == sorted(SPECIALISTS) and not summary["errors"]
    assert store.counts()["valid_outputs"] == 6 and store.counts()["summaries"] == {"caution": 1}
    assert (_sha(db), _sha(cal)) == before  # shadow state byte-identical
    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM signals")
    ro.close()
    assert ctx.facts["bars"][-1]["open_time"] < sig["bar_close"]  # no bar after the signal close


def test_invalid_or_missing_outputs_fall_back_to_insufficient_inputs(tmp_path):
    ctx = _ctx(tmp_path)
    store = AdvisoryStore(tmp_path / "a.sqlite")
    dry = run_desk(ctx, DryRunTransport(), store, runner="xau_orb", now=NOW, model=None)
    assert dry["source"] == "deterministic_fallback" and dry["recommendation"] == "insufficient_inputs"
    assert set(dry["invalid_or_missing"]) == set(SPECIALISTS) and store.counts()["valid_outputs"] == 0
    replies = {r: payload(r) for r in SPECIALISTS if r != "news"}
    replies["skeptic"] = payload("skeptic", stop_loss=1)  # over-reach -> rejected
    replies["news"] = news_payload()
    bad = run_desk(ctx, ReplayTransport(replies), store, runner="xau_orb", now=NOW + timedelta(seconds=1), model="m")
    assert bad["recommendation"] == "insufficient_inputs" and "controlled fields" in bad["errors"]["skeptic"]
    assert "no verified calendar" in bad["errors"]["news"]


def test_daily_call_budget_caps_live_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    ctx = _ctx(tmp_path)
    opener = FakeOpener(content=json.dumps(payload("scout")))
    t = XaiTransport(model="grok-test", opener=opener)
    store = AdvisoryStore(tmp_path / "a.sqlite")
    run_desk(ctx, t, store, runner="xau_orb", now=NOW, model="grok-test", daily_call_budget=2)
    assert len(opener.requests) == 2
    statuses = [r[0] for r in store.db.execute("SELECT status FROM calls ORDER BY id")]
    assert statuses[:2] == ["ok", "ok"] and set(statuses[2:]) == {"skipped"}


def test_frozen_runners_and_risk_engine_do_not_import_the_agents():
    paths = [ROOT / "research/phase2d_shadow.py", ROOT / "research/phase2e_binance_shadow.py",
             ROOT / "research/xau_selective_a_shadow.py", *(ROOT / "trading/shadow").glob("*.py"), ROOT / "trading/risk.py",
             ROOT / "trading/gate.py", *(ROOT / "trading/mtf").glob("*.py")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "grok_advisory" not in text and "trading.agents" not in text, path
    desk = (ROOT / "trading/agents/desk.py").read_text(encoding="utf-8")
    assert not any(t in desk for t in ("urlopen", "import requests", "import httpx", "openai", "anthropic"))

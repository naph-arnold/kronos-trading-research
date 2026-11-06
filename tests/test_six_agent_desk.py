"""Six agent desk safety and audit tests (offline, no Grok calls)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from research.six_agent_desk import (
    ROLES,
    PaperLimits,
    PaperProposal,
    RoleReview,
    append_audit,
    decide,
    verify_audit,
)


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
TIME = NOW.isoformat()
SHA = "a" * 64


def proposal(**changes):
    original = PaperProposal(
        opportunity_id="study-1", symbol="BTCUSDT", side="long",
        entry=100.0, stop=99.0, target=103.0, notional_usdt=200.0,
        forecast_sha256=SHA, created_at=TIME,
    )
    return replace(original, **changes)


def reviews():
    return [
        RoleReview(role=role, verdict="approve", evidence_ref=f"evidence:{role}",
                   rationale="Reviewed sample fixture", observed_at=TIME)
        for role in ROLES
    ]


def limits(**changes):
    original = PaperLimits(simulation_enabled=True, kill_switch=False)
    return replace(original, **changes)


def test_default_is_disabled_even_with_unanimous_votes():
    outcome = decide(proposal(), reviews(), PaperLimits(), now=NOW)
    assert outcome["decision"] == "vetoed"
    assert outcome["live_trading_enabled"] is False
    assert not outcome["approved_for_paper"]


def test_unanimous_complete_valid_reviews_only_create_eligibility():
    outcome = decide(proposal(), reviews(), limits(), now=NOW)
    assert outcome["decision"] == "paper_eligible"
    assert outcome["approved_for_paper"] is True
    assert outcome["mode"] == "paper_only"
    assert "order_id" not in outcome


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_independent_veto(role):
    entries = [replace(r, verdict="reject") if r.role == role else r for r in reviews()]
    outcome = decide(proposal(), entries, limits(), now=NOW)
    assert not outcome["approved_for_paper"]
    assert any(role in reason for reason in outcome["reasons"])


def test_missing_duplicate_abstain_or_expired_role_vetoes():
    assert not decide(proposal(), reviews()[:-1], limits(), now=NOW)["approved_for_paper"]
    duplicate = reviews()
    duplicate[-1] = duplicate[0]
    assert not decide(proposal(), duplicate, limits(), now=NOW)["approved_for_paper"]
    expired = reviews()
    expired[0] = replace(expired[0], observed_at=(NOW - timedelta(hours=2)).isoformat())
    assert not decide(proposal(), expired, limits(), now=NOW)["approved_for_paper"]
    abstain = reviews()
    abstain[1] = replace(abstain[1], verdict="abstain")
    assert not decide(proposal(), abstain, limits(), now=NOW)["approved_for_paper"]


def test_risk_limit_vetoes_model_consensus():
    for case in (
        limits(kill_switch=True),
        limits(open_positions=1),
        limits(daily_realized_loss_usdt=200),
        limits(equity_usdt=100),
        limits(max_risk_fraction=0.00001),
    ):
        outcome = decide(proposal(), reviews(), case, now=NOW)
        assert outcome["decision"] == "vetoed"


@pytest.mark.parametrize("change", [
    {"symbol": "ETHUSDT"}, {"side": "short"}, {"stop": 101.0},
    {"target": 99.0}, {"notional_usdt": float("nan")},
    {"forecast_sha256": "bad"}, {"created_at": (NOW - timedelta(hours=2)).isoformat()},
])
def test_invalid_proposals_fail_closed(change):
    with pytest.raises(ValueError):
        decide(proposal(**change), reviews(), limits(), now=NOW)


def test_hash_chained_audit_detects_tampering(tmp_path):
    location = tmp_path / "audit.jsonl"
    first = decide(proposal(), reviews(), limits(), now=NOW)
    first_hash = append_audit(location, first)
    second = decide(proposal(opportunity_id="study-2"), reviews(), PaperLimits(), now=NOW)
    second_hash = append_audit(location, second)
    assert first_hash != second_hash
    assert verify_audit(location) == second_hash
    location.write_text(location.read_text().replace("study-1", "forged"))
    with pytest.raises(ValueError, match="Tampered"):
        verify_audit(location)


def test_no_real_trading_config_exists():
    assert "exchange_api_key" not in PaperLimits.__dataclass_fields__
    assert "live_execution_enabled" not in PaperLimits.__dataclass_fields__

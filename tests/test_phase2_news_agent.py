"""Grok News Agent contract (Section 10): interpretation only, cannot override verified controls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading.agents.news import PROVIDER, NewsAgentUnavailable, advisory_guard, parse_news_assessment
from trading.common import Market, ValidationError
from trading.marketcal.fomc import parse_fomc_calendar
from trading.marketcal.events import EventStore
from trading.marketcal.service import CalendarProvider, CalendarService
from research.six_agent_desk import AI_PROVIDER, ROLES

UTC = timezone.utc
NOW = datetime(2026, 12, 9, 17, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "data" / "fomc_calendar_2026.html"


def store():
    s = EventStore()
    s.upsert(parse_fomc_calendar(FIXTURE.read_text(encoding="utf-8"), retrieved_at=NOW - timedelta(days=1)))
    return s


def payload(**overrides):
    base = {"schema_version": 1, "role": "news", "provider": PROVIDER, "model": "grok-x (placeholder id)",
            "assessed_at": NOW.isoformat(), "markets": ["xauusd_mt5"], "event_ids": ["fomc-2026-12-09"],
            "headlines": [], "interpretation": "FOMC decision at 19:00 UTC; markets price a hold.",
            "implications": {"xauusd_mt5": "volatility likely"}, "geopolitical_risks": [],
            "abnormal_conditions": [], "conflicts": ["Two sources disagree on dot-plot expectations"],
            "confidence": 0.6, "recommendation": "avoid_new_entries", "valid_for_minutes": 120}
    base.update(overrides)
    return base


def test_valid_assessment_parses_and_records_conflicts():
    a = parse_news_assessment(payload(), store=store(), now=NOW)
    assert a.recommendation == "avoid_new_entries" and a.conflicts


@pytest.mark.parametrize("bad", [
    {"stop_loss": None}, {"risk_fraction": 0.02}, {"closures": []}, {"execution_mode": "live"},
    {"schedule": {"fomc": "tomorrow"}}, {"override": True},
])
def test_controlled_fields_are_rejected(bad):
    with pytest.raises(ValidationError, match="controlled fields"):
        parse_news_assessment(payload(**bad), store=store(), now=NOW)


@pytest.mark.parametrize("bad,match", [
    ({"event_ids": ["fomc-2026-12-10"]}, "not in the verified calendar"),
    ({"provider": "Claude"}, "official xAI Grok"),
    ({"assessed_at": (NOW - timedelta(hours=5)).isoformat()}, "stale"),
    ({"recommendation": "go_long"}, "recommendation"),
    ({"confidence": 2}, "confidence"),
    ({"valid_for_minutes": 10_000}, "valid_for_minutes"),
    ({"markets": ["eurusd"]}, "markets"),
    ({"surprise": 1}, "schema"),
])
def test_invalid_assessments_rejected(bad, match):
    with pytest.raises(ValidationError, match=match):
        parse_news_assessment(payload(**bad), store=store(), now=NOW)


def test_advisory_guard_only_adds_restriction_for_its_market_and_window():
    a = parse_news_assessment(payload(), store=store(), now=NOW)
    gold, crypto = advisory_guard(a, "xauusd_mt5"), advisory_guard(a, "btcusdt_binance_spot")
    assert "avoid new entries" in gold(NOW + timedelta(minutes=10)) and crypto(NOW) is None
    assert gold(NOW + timedelta(minutes=121)) is None
    calm = parse_news_assessment(payload(recommendation="no_view"), store=store(), now=NOW)
    assert advisory_guard(calm, "xauusd_mt5")(NOW) is None
    assert advisory_guard(None, "xauusd_mt5")(NOW) is None


def test_calendar_blackout_enforced_when_grok_unavailable():
    s = store()
    events = s.between(NOW - timedelta(days=400), NOW + timedelta(days=400))
    service = CalendarService(s, [CalendarProvider("federalreserve.gov/fomccalendars", lambda: events,
                                                   frozenset({"FOMC_DECISION"}), timedelta(days=14))])
    service.refresh(NOW)
    guard = service.guard(Market.XAUUSD_MT5, required_series=("FOMC_DECISION",))

    def grok_down():
        raise NewsAgentUnavailable("api.x.ai timeout")

    with pytest.raises(NewsAgentUnavailable):
        grok_down()
    assert "FOMC" in guard(datetime(2026, 12, 9, 18, 30, tzinfo=UTC))  # still enforced without Grok


def test_six_approved_grok_roles():
    assert ROLES == ("scout", "news", "chart", "kronos_analyst", "skeptic", "coordinator")
    assert "xAI Grok" in AI_PROVIDER

"""Grok News Agent contract (Section 10). Interface and validation only — no xAI API calls here.

The News Agent (official xAI Grok API) *interprets* calendar events and market news and
reports to the Coordinator. Its structured output is validated against this schema:

* It may reference only events that exist in the deterministic `EventStore` (it cannot
  invent schedules) and may only flag concerns / recommend caution.
* It has no fields for, and the validator rejects any attempt at, changing schedules,
  closures, blackout buffers, stop-losses, risk fractions, limits or execution mode.
* `advisory_restrictions()` can only ADD a temporary no-new-entry recommendation that the
  operator may opt into; nothing it returns can remove or shorten a verified restriction.
* The calendar service and risk engine never depend on it: if Grok is unavailable,
  `NewsAgentUnavailable` is recorded and verified closures/blackouts keep working.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trading.common import ValidationError, require_utc
from trading.marketcal.events import EventStore

PROVIDER = "xAI Grok API (official, https://api.x.ai)"
ROLE = "news"
RECOMMENDATIONS = ("no_view", "caution", "avoid_new_entries")
ALLOWED_KEYS = {"schema_version", "role", "provider", "model", "assessed_at", "markets", "event_ids", "headlines",
                "interpretation", "implications", "geopolitical_risks", "abnormal_conditions", "conflicts",
                "confidence", "recommendation", "valid_for_minutes"}
FORBIDDEN_KEYS = {"schedule", "scheduled_at", "closures", "open_market", "blackout", "buffer", "stop", "stop_loss",
                  "risk_fraction", "max_risk", "position_size", "size", "limits", "execution_mode", "live",
                  "approve_trade", "override"}
MAX_VALIDITY = timedelta(hours=4)


class NewsAgentUnavailable(RuntimeError):
    """Grok could not be reached or returned invalid output. Deterministic controls continue."""


@dataclass(frozen=True)
class NewsAssessment:
    model: str
    assessed_at: datetime
    markets: tuple[str, ...]
    event_ids: tuple[str, ...]
    headlines: tuple[dict, ...]  # {"title", "url", "published_at"} - unverified sources
    interpretation: str
    implications: dict  # {"xauusd_mt5": "...", "btcusdt_binance_spot": "..."}
    geopolitical_risks: tuple[str, ...]
    abnormal_conditions: tuple[str, ...]
    conflicts: tuple[str, ...]  # conflicting or uncertain information, stated explicitly
    confidence: float
    recommendation: str
    valid_for_minutes: int


def parse_news_assessment(payload: object, *, store: EventStore, now: datetime) -> NewsAssessment:
    """Validate model output strictly. Any unknown or forbidden key, unknown event or invalid value rejects it."""
    require_utc(now, "now")
    if not isinstance(payload, dict):
        raise ValidationError("News assessment must be an object")
    keys = set(payload)
    forbidden = keys & FORBIDDEN_KEYS
    if forbidden:
        raise ValidationError(f"News agent attempted to set controlled fields: {sorted(forbidden)}")
    if keys - ALLOWED_KEYS or payload.get("schema_version") != 1 or payload.get("role") != ROLE:
        raise ValidationError("News assessment does not match schema v1")
    if payload.get("provider") != PROVIDER:
        raise ValidationError("News assessment must come from the official xAI Grok API provider")
    assessed = datetime.fromisoformat(str(payload["assessed_at"]).replace("Z", "+00:00"))
    require_utc(assessed, "assessed_at")
    if assessed > now + timedelta(seconds=5) or now - assessed > MAX_VALIDITY:
        raise ValidationError("News assessment is future-dated or stale")
    window = store.between(now - timedelta(days=7), now + timedelta(days=30))
    known = {e.event_id for e in window}
    unknown = [e for e in payload.get("event_ids", []) if e not in known]
    if unknown:
        raise ValidationError(f"News assessment references events not in the verified calendar: {unknown}")
    recommendation = payload.get("recommendation")
    confidence = payload.get("confidence")
    minutes = payload.get("valid_for_minutes")
    if recommendation not in RECOMMENDATIONS:
        raise ValidationError("Unknown recommendation")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValidationError("confidence must be in [0, 1]")
    if type(minutes) is not int or not 1 <= minutes <= int(MAX_VALIDITY.total_seconds() // 60):
        raise ValidationError("valid_for_minutes out of range")
    markets = tuple(payload.get("markets", ()))
    if not set(markets) <= {"xauusd_mt5", "btcusdt_binance_spot"} or not markets:
        raise ValidationError("Unknown or missing markets")
    text = str(payload.get("interpretation", ""))
    if not text.strip() or len(text) > 4000:
        raise ValidationError("interpretation required (max 4000 chars)")
    return NewsAssessment(
        model=str(payload["model"]), assessed_at=assessed, markets=markets,
        event_ids=tuple(payload.get("event_ids", ())), headlines=tuple(payload.get("headlines", ())),
        interpretation=text, implications=dict(payload.get("implications", {})),
        geopolitical_risks=tuple(payload.get("geopolitical_risks", ())),
        abnormal_conditions=tuple(payload.get("abnormal_conditions", ())),
        conflicts=tuple(payload.get("conflicts", ())), confidence=float(confidence),
        recommendation=recommendation, valid_for_minutes=minutes)


def advisory_guard(assessment: NewsAssessment | None, market: str):
    """Optional, operator-enabled guard: an `avoid_new_entries` recommendation can only ADD a
    temporary restriction for its market. It never removes or relaxes anything."""

    def guard(now: datetime) -> str | None:
        if assessment is None or market not in assessment.markets:
            return None
        if assessment.recommendation != "avoid_new_entries":
            return None
        if assessment.assessed_at <= now < assessment.assessed_at + timedelta(minutes=assessment.valid_for_minutes):
            return f"News agent advisory ({assessment.model}): avoid new entries"
        return None

    return guard


class NewsAgentPort(Protocol):
    """Phase 4 adapter interface. Implementations call the official xAI Grok API with the
    calendar context and return raw JSON for `parse_news_assessment`."""

    def assess(self, *, events: list[dict], markets: list[str], now: datetime) -> dict: ...

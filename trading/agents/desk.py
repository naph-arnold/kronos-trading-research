"""Six Grok reasoning agents - advisory-only contracts (Phase 4 preparation). No network code lives here.

Roles (docs/ARCHITECTURE.md): Scout, News, Chart, Kronos Analyst, Skeptic, Coordinator. Every role runs on the
official xAI Grok API; Kronos stays the only forecaster (the Kronos Analyst interprets recorded Kronos output,
it never forecasts). Outputs are UNTRUSTED model text until validated here, and even then they are advisory:

* a validated output can only describe, flag concerns, or recommend `no_view` / `caution` /
  `avoid_new_entries`; there are no fields for, and the validator rejects any attempt at, setting entries,
  stops, targets, sizes, risk limits, parameters, schedules or execution mode;
* every reference to a signal, Kronos forecast or calendar event must exist in the read-only context the
  agent was given (no invented evidence ids);
* the Coordinator only summarises validated role outputs; if any input is missing or invalid its result is
  `insufficient_inputs` (fail closed to "no view", never to relaxed controls);
* records are written to a separate advisory store; nothing in `trading/` outside this package imports it, the
  shadow engines and `trading.risk.evaluate` cannot receive it, and frozen strategy decisions are unchanged.

The News role keeps its richer calendar contract in `trading/agents/news.py` (`parse_news_assessment`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading.common import ValidationError, require_utc

SCHEMA_VERSION = 1
PROVIDER = "xAI Grok API (official, https://api.x.ai)"
ROLES = ("scout", "news", "chart", "kronos_analyst", "skeptic", "coordinator")
SPECIALISTS = ROLES[:-1]
RECOMMENDATIONS = ("no_view", "caution", "avoid_new_entries")
COORDINATOR_RECOMMENDATIONS = RECOMMENDATIONS + ("insufficient_inputs",)
MAX_VALIDITY = timedelta(hours=4)
MAX_TEXT = 2000

ROLE_BRIEFS = {
    "scout": "Detect unusual market conditions (volatility, gaps, spread, liquidity, data quality) in the supplied "
             "recent bars and health flags. Cite only supplied evidence ids.",
    "news": "Interpret verified calendar events and supplied headlines; flag event risk. Never invent schedules.",
    "chart": "Describe market structure around the supplied signal (trend, ranges, nearby levels) using only the "
             "supplied bars. Levels you mention must lie inside the supplied price range.",
    "kronos_analyst": "Interpret the supplied recorded Kronos forecast (direction, magnitude, uncertainty) relative "
                      "to the signal. You do not forecast; if no forecast is supplied, say so and give no_view.",
    "skeptic": "Argue the strongest case AGAINST acting on the signal using the supplied evidence; list failure "
               "modes. You cannot veto anything; the deterministic risk engine decides.",
    "coordinator": "Summarise the five validated specialist outputs faithfully, state disagreements, and give one "
                   "advisory recommendation. You cannot approve, size or block trades.",
}

ALLOWED_KEYS = {"schema_version", "role", "provider", "model", "assessed_at", "subject", "assessment", "evidence",
                "concerns", "confidence", "recommendation", "valid_for_minutes", "levels", "kronos_forecast_id",
                "role_inputs", "disagreements"}
FORBIDDEN_KEYS = {"entry", "entry_price", "stop", "stop_loss", "stop_price", "take_profit", "target", "size",
                  "position_size", "lots", "quantity", "risk", "risk_fraction", "max_risk", "limits", "params",
                  "parameters", "schedule", "closures", "blackout", "execution_mode", "live", "order", "orders",
                  "approve_trade", "approve", "veto", "override", "side", "action"}


@dataclass(frozen=True)
class AdvisoryContext:
    """Read-only facts handed to the agents. Evidence ids are the only references an output may cite."""

    subject_kind: str  # "signal" | "market_brief"
    subject_id: str
    market: str
    as_of: datetime
    evidence_ids: frozenset[str]
    price_range: tuple[float, float] | None = None
    kronos_forecast_ids: frozenset[str] = frozenset()
    facts: dict = field(default_factory=dict, hash=False, compare=False)

    def digest(self) -> str:
        body = {"subject": [self.subject_kind, self.subject_id], "market": self.market,
                "as_of": self.as_of.isoformat(), "evidence": sorted(self.evidence_ids),
                "kronos": sorted(self.kronos_forecast_ids), "price_range": self.price_range, "facts": self.facts}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class RoleOutput:
    role: str
    model: str
    assessed_at: datetime
    subject: dict
    assessment: str
    evidence: tuple[str, ...]
    concerns: tuple[str, ...]
    confidence: float
    recommendation: str
    valid_for_minutes: int
    extra: dict
    advisory_only: bool = True


def _forbidden(obj, path="") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                found.append(f"{path}{k}")
            found += _forbidden(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _forbidden(v, f"{path}{i}.")
    return found


def _texts(values, name: str, limit: int = 20) -> tuple[str, ...]:
    if not isinstance(values, list) or len(values) > limit or not all(isinstance(v, str) and 0 < len(v) <= 500
                                                                          for v in values):
        raise ValidationError(f"{name} must be a list of up to {limit} short strings")
    return tuple(values)


def parse_role_output(payload: object, *, role: str, context: AdvisoryContext, now: datetime,
                      validated_inputs: dict[str, RoleOutput] | None = None) -> RoleOutput:
    """Validate one model output strictly. Any unknown/forbidden key, foreign provider, wrong role or subject,
    stale timestamp, uncited/invented evidence or out-of-range value rejects it."""
    require_utc(now, "now")
    if role not in ROLES or role == "news":
        raise ValidationError("Use parse_news_assessment for the News role; unknown role otherwise")
    if not isinstance(payload, dict):
        raise ValidationError("Agent output must be a JSON object")
    bad = _forbidden(payload)
    if bad:
        raise ValidationError(f"Agent attempted to set controlled fields: {sorted(bad)}")
    if set(payload) - ALLOWED_KEYS or payload.get("schema_version") != SCHEMA_VERSION or payload.get("role") != role:
        raise ValidationError("Agent output does not match schema v1 for this role")
    if payload.get("provider") != PROVIDER:
        raise ValidationError("Agent output must come from the official xAI Grok API provider")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        raise ValidationError("model required")
    try:
        assessed = datetime.fromisoformat(str(payload.get("assessed_at")).replace("Z", "+00:00"))
        require_utc(assessed, "assessed_at")
    except (ValueError, ValidationError) as exc:
        raise ValidationError(f"assessed_at invalid: {exc}") from None
    if assessed > now + timedelta(seconds=5) or now - assessed > MAX_VALIDITY or assessed < context.as_of - MAX_VALIDITY:
        raise ValidationError("Agent output is future-dated or stale")
    subject = payload.get("subject")
    if subject != {"kind": context.subject_kind, "id": context.subject_id}:
        raise ValidationError("Agent output refers to a different subject")
    text = payload.get("assessment")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValidationError(f"assessment required (max {MAX_TEXT} chars)")
    evidence = _texts(payload.get("evidence", []), "evidence")
    unknown = [e for e in evidence if e not in context.evidence_ids]
    if unknown:
        raise ValidationError(f"Agent cited evidence not in its context: {unknown}")
    concerns = _texts(payload.get("concerns", []), "concerns")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValidationError("confidence must be in [0, 1]")
    minutes = payload.get("valid_for_minutes")
    if type(minutes) is not int or not 1 <= minutes <= int(MAX_VALIDITY.total_seconds() // 60):
        raise ValidationError("valid_for_minutes out of range")
    allowed = COORDINATOR_RECOMMENDATIONS if role == "coordinator" else RECOMMENDATIONS
    recommendation = payload.get("recommendation")
    if recommendation not in allowed:
        raise ValidationError("Unknown recommendation")
    extra = {}
    if "levels" in payload:
        if role != "chart" or context.price_range is None:
            raise ValidationError("Only the Chart role may cite price levels, inside the supplied range")
        levels = payload["levels"]
        lo, hi = context.price_range
        if not isinstance(levels, list) or len(levels) > 10 or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and lo <= x <= hi for x in levels):
            raise ValidationError("Chart levels must be numbers inside the supplied price range")
        extra["levels"] = list(levels)
    if role == "kronos_analyst":
        fid = payload.get("kronos_forecast_id")
        if fid is None:
            if recommendation != "no_view":
                raise ValidationError("Without a supplied Kronos forecast the Kronos Analyst must give no_view")
        elif fid not in context.kronos_forecast_ids:
            raise ValidationError("Kronos Analyst cited a forecast that was not supplied")
        extra["kronos_forecast_id"] = fid
    elif "kronos_forecast_id" in payload:
        raise ValidationError("Only the Kronos Analyst may cite a Kronos forecast")
    if role == "coordinator":
        inputs = payload.get("role_inputs")
        valid = validated_inputs or {}
        if not isinstance(inputs, list) or sorted(inputs) != sorted(valid):
            raise ValidationError("Coordinator must list exactly the validated specialist outputs it received")
        if len(valid) < len(SPECIALISTS) and recommendation != "insufficient_inputs":
            raise ValidationError("Coordinator must report insufficient_inputs when a specialist output is missing")
        extra["role_inputs"] = sorted(inputs)
        extra["disagreements"] = list(_texts(payload.get("disagreements", []), "disagreements"))
    elif "role_inputs" in payload or "disagreements" in payload:
        raise ValidationError("Only the Coordinator may summarise role inputs")
    return RoleOutput(role=role, model=model, assessed_at=assessed, subject=subject, assessment=text,
                      evidence=evidence, concerns=concerns, confidence=float(confidence),
                      recommendation=recommendation, valid_for_minutes=minutes, extra=extra)


def deterministic_coordinator_fallback(context: AdvisoryContext, outputs: dict[str, RoleOutput],
                                       errors: dict[str, str], *, now: datetime) -> dict:
    """Recorded when the Coordinator cannot run (missing/invalid inputs or unavailable). It never relaxes
    anything; it only records that there is no complete advisory view."""
    return {"role": "coordinator", "source": "deterministic_fallback", "assessed_at": now.isoformat(),
            "subject": {"kind": context.subject_kind, "id": context.subject_id},
            "recommendation": "insufficient_inputs", "valid_inputs": sorted(outputs),
            "invalid_or_missing": {r: errors.get(r, "missing") for r in SPECIALISTS if r not in outputs},
            "advisory_only": True}


def output_schema(role: str) -> dict:
    """JSON shape the model is instructed to return (also used to build prompts)."""
    rec = list(COORDINATOR_RECOMMENDATIONS if role == "coordinator" else RECOMMENDATIONS)
    shape = {"schema_version": 1, "role": role, "provider": PROVIDER, "model": "<model id>",
             "assessed_at": "<UTC ISO-8601>", "subject": {"kind": "<as given>", "id": "<as given>"},
             "assessment": f"<= {MAX_TEXT} chars", "evidence": ["<ids from the context only>"],
             "concerns": ["<short strings>"], "confidence": "0..1", "recommendation": rec,
             "valid_for_minutes": "1..240"}
    if role == "chart":
        shape["levels"] = ["<prices inside the supplied range, optional>"]
    if role == "kronos_analyst":
        shape["kronos_forecast_id"] = "<supplied id or null>"
    if role == "coordinator":
        shape["role_inputs"] = ["<specialist roles received>"]
        shape["disagreements"] = ["<short strings>"]
    return shape

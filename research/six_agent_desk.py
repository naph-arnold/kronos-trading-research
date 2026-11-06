"""Six-role research desk with fail-closed paper-only approval and audit log.

This is a *local orchestrator contract*, not six connected Grok accounts.
Role records are untrusted advisory inputs. Deterministic limits, not model
votes, control whether a simulated position is ever opened.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Approved six Grok reasoning roles (docs/ARCHITECTURE.md). Position sizing/risk is deterministic code,
# not an AI role, so the former "strategist"/"risk" roles were replaced by "kronos_analyst"/"coordinator".
ROLES = ("scout", "news", "chart", "kronos_analyst", "skeptic", "coordinator")
AI_PROVIDER = "xAI Grok API (official, https://api.x.ai)"
VERDICTS = ("approve", "reject", "abstain")


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
        raise ValueError("Decision timestamps must be in UTC")
    return moment


@dataclass(frozen=True)
class RoleReview:
    role: str
    verdict: str
    evidence_ref: str
    rationale: str
    observed_at: str

    def validate(self, now: datetime, max_age_seconds: int) -> None:
        if self.role not in ROLES or self.verdict not in VERDICTS:
            raise ValueError("Unknown role or verdict")
        if not self.evidence_ref.strip() or not self.rationale.strip():
            raise ValueError("Every review requires a rationale and evidence reference")
        if len(self.evidence_ref) > 256 or len(self.rationale) > 2000:
            raise ValueError("Review text too long")
        age = (now - _utc(self.observed_at)).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise ValueError("Review is stale or future dated")


@dataclass(frozen=True)
class PaperProposal:
    opportunity_id: str
    symbol: str
    side: str
    entry: float
    stop: float
    target: float
    notional_usdt: float
    forecast_sha256: str
    created_at: str

    def validate(self, now: datetime) -> None:
        if not self.opportunity_id.strip() or len(self.opportunity_id) > 120:
            raise ValueError("Missing or invalid opportunity ID")
        if self.symbol != "BTCUSDT" or self.side != "long":
            raise ValueError("Only Binance BTCUSDT spot long is supported")
        if len(self.forecast_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.forecast_sha256):
            raise ValueError("Missing or invalid forecast provenance")
        values = (self.entry, self.stop, self.target, self.notional_usdt)
        if any(not isinstance(x, (int, float)) or isinstance(x, bool)
               or not math.isfinite(x) or x <= 0 for x in values):
            raise ValueError("Prices and notional must be finite positive numbers")
        if not self.stop < self.entry < self.target:
            raise ValueError("A long proposal needs stop below entry and target above")
        age = (now - _utc(self.created_at)).total_seconds()
        if age < 0 or age > 3600:
            raise ValueError("Proposal timestamp stale or future dated")


@dataclass(frozen=True)
class PaperLimits:
    equity_usdt: float = 10_000
    max_notional_fraction: float = 0.10
    max_risk_fraction: float = 0.005
    max_daily_loss_fraction: float = 0.02
    daily_realized_loss_usdt: float = 0.0
    open_positions: int = 0
    max_open_positions: int = 1
    simulation_enabled: bool = False
    kill_switch: bool = True

    def check(self, proposal: PaperProposal) -> list[str]:
        reasons = []
        if not self.simulation_enabled:
            reasons.append("Paper simulation not enabled")
        if self.kill_switch:
            reasons.append("Kill switch active")
        if (not math.isfinite(self.equity_usdt) or self.equity_usdt <= 0
                or not 0 < self.max_notional_fraction <= 1
                or not 0 < self.max_risk_fraction <= 1
                or not 0 < self.max_daily_loss_fraction <= 1):
            return reasons + ["Invalid risk configuration"]
        if self.open_positions < 0 or self.open_positions >= self.max_open_positions:
            reasons.append("Maximum open positions reached")
        if (not math.isfinite(self.daily_realized_loss_usdt)
                or self.daily_realized_loss_usdt < 0
                or self.daily_realized_loss_usdt >= self.equity_usdt * self.max_daily_loss_fraction):
            reasons.append("Daily realized loss limit reached or invalid")
        if proposal.notional_usdt > self.equity_usdt * self.max_notional_fraction:
            reasons.append("Notional exceeds exposure limit")
        potential_loss = proposal.notional_usdt * (proposal.entry - proposal.stop) / proposal.entry
        if potential_loss > self.equity_usdt * self.max_risk_fraction:
            reasons.append("Stop-loss risk exceeds allowed fraction")
        return reasons


def decide(
    proposal: PaperProposal,
    reviews: list[RoleReview],
    limits: PaperLimits,
    *,
    now: datetime | None = None,
    evidence_max_age_seconds: int = 3600,
) -> dict:
    """Fail closed on absent, duplicate, stale or dissenting reviews.

    This does not validate that an external AI actually authored a role or
    that evidence references correspond to real news. A trusted future
    adapter must authenticate each source and verify evidence separately.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset().total_seconds() != 0:
        raise ValueError("now must be UTC")
    proposal.validate(now)
    reasons = limits.check(proposal)
    if len(reviews) != len(ROLES) or {r.role for r in reviews} != set(ROLES):
        reasons.append("All six unique role reviews are required")
    for review in reviews:
        try:
            review.validate(now, evidence_max_age_seconds)
        except (ValueError, TypeError) as exc:
            reasons.append(f"{review.role}: {exc}")
        if review.verdict != "approve":
            reasons.append(f"{review.role}: {review.verdict}")
    # Risk engine is deterministic, and no external exchange execution exists.
    return {
        "schema_version": 1,
        "opportunity_id": proposal.opportunity_id,
        "symbol": proposal.symbol,
        "mode": "paper_only",
        "live_trading_enabled": False,
        "approved_for_paper": not reasons,
        "decision": "paper_eligible" if not reasons else "vetoed",
        "reasons": reasons,
        "checked_at": now.isoformat(),
        "proposal": asdict(proposal),
        "reviews": [asdict(r) for r in reviews],
        "limits": asdict(limits),
        "warning": "No real execution. Role identity and external evidence require a trusted adapter.",
    }


def append_audit(path: Path, decision: dict) -> str:
    """Single-writer hash-linked JSONL. Not safe for concurrent writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    last_hash = "0" * 64
    if path.exists():
        last_hash = verify_audit(path)
    record = {"previous_sha256": last_hash, "decision": decision}
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    record_hash = hashlib.sha256(canonical.encode()).hexdigest()
    record["sha256"] = record_hash
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record_hash


def verify_audit(path: Path) -> str:
    previous = "0" * 64
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                checksum = record.pop("sha256")
                canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
                expected = hashlib.sha256(canonical.encode()).hexdigest()
                if checksum != expected or record.get("previous_sha256") != previous:
                    raise ValueError("Audit checksum or chain mismatch")
                previous = checksum
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Tampered or incomplete audit record") from exc
    return previous

"""Research-only advisory interfaces (Kronos forecasts, six Grok agents, TradingView alerts).

Advisory inputs are RECORDED next to candidates for later analysis. They are never passed to the simulator or
the risk engine (neither accepts them), cannot create, resize or approve a trade, and the deterministic risk
engine keeps final veto authority. In this milestone no paid AI call is made: the Grok interface is inactive and
only accepts pre-recorded responses; Kronos forecasts come from recorded files; TradingView alerts are optional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

GROK_ROLES = ("scout", "news", "chart", "kronos_analyst", "skeptic", "coordinator")
OFFICIAL_XAI_BASE_URL = "https://api.x.ai"


class GrokNotActivated(RuntimeError):
    """Live Grok calls are not authorized in this milestone."""


@dataclass(frozen=True)
class AdvisoryRecord:
    source: str  # "kronos" | "grok:<role>" | "tradingview"
    time_ns: int
    payload: dict
    advisory_only: bool = True


class AdvisoryProvider(Protocol):
    name: str

    def advise(self, candidates: pd.DataFrame) -> list[AdvisoryRecord]: ...


@dataclass
class RecordedKronos:
    """Kronos forecasts from a recorded file: {target open time (ns): predicted close}."""

    forecasts: dict
    name: str = "kronos"

    def advise(self, candidates: pd.DataFrame) -> list[AdvisoryRecord]:
        return [AdvisoryRecord("kronos", int(t), {"predicted_close": self.forecasts[int(t)]})
                for t in candidates.time_ns if int(t) in self.forecasts]


@dataclass
class InactiveGrokAgent:
    """Placeholder for one of the six reasoning agents on the official xAI Grok API. Only replays recorded
    responses; any live request raises `GrokNotActivated`."""

    role: str
    recorded: dict = field(default_factory=dict)  # time_ns -> response dict
    base_url: str = OFFICIAL_XAI_BASE_URL

    def __post_init__(self):
        if self.role not in GROK_ROLES:
            raise ValueError(f"Unknown agent role {self.role!r}")
        if self.base_url != OFFICIAL_XAI_BASE_URL:
            raise ValueError("Reasoning agents must use the official xAI Grok API")

    @property
    def name(self) -> str:
        return f"grok:{self.role}"

    def advise(self, candidates: pd.DataFrame) -> list[AdvisoryRecord]:
        out = []
        for t in candidates.time_ns:
            if int(t) in self.recorded:
                out.append(AdvisoryRecord(self.name, int(t), dict(self.recorded[int(t)])))
        return out

    def live_request(self, *args, **kwargs):
        raise GrokNotActivated("Grok agents are not activated in this milestone (no paid AI calls)")


def annotate(candidates: pd.DataFrame, providers: list) -> pd.DataFrame:
    """Return a COPY of the candidates with an `advisory` column (list of records). The input is not modified
    and the result is for analysis only; `simulate` ignores unknown columns and never reads this one."""
    by_time: dict[int, list] = {}
    for provider in providers:
        for record in provider.advise(candidates):
            by_time.setdefault(record.time_ns, []).append(record)
    out = candidates.copy()
    out["advisory"] = [by_time.get(int(t), []) for t in out.time_ns]
    return out

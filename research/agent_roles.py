"""Provider-neutral task specifications for six Grok-compatible research bots.

These are *instructions/contracts*, not deployed Grok bots. The model-generated
RoleReview fields are untrusted; a future trusted adapter must bind identity,
verify cited evidence and authenticate callbacks.
"""
from __future__ import annotations

import json

from research.six_agent_desk import ROLES

ROLE_TASKS = {
    "scout": (
        "Monitor independently verified Binance spot BTCUSDT 1h data. Identify a "
        "specific anomaly or opportunity and cite candle timestamps and dataset hash. "
        "Never assert a profitable edge from the model alone."
    ),
    "news": (
        "Look up the relevant primary-source market news and official announcements. "
        "Include timestamped URLs and explicitly mark unknown or unverified catalysts. "
        "Do not invent explanations for a candle move."
    ),
    "chart": (
        "Examine verified historical candles and optionally Kronos forecasts. Explain "
        "trend, volatility and nearby price levels based on observed, not future, data. "
        "Never claim forecasts are certainties."
    ),
    "strategist": (
        "Write a concrete *hypothetical* BTCUSDT spot long/cash setup with entry, stop, "
        "target, expiry and invalidation. No shorting or leverage. Abstain if evidence "
        "cannot support a setup; do not place orders."
    ),
    "risk": (
        "Objectively challenge the sizing and exposure of the proposed simulation. "
        "Reject stale data, unclear stop, excessive notional, open exposure or losses. "
        "Do not override deterministic paper limits."
    ),
    "skeptic": (
        "Actively challenge the proposed trade. Seek opposing evidence, regime changes, "
        "backtest failures, news timing problems, slippage and overfitting. Reject or "
        "abstain unless the setup survives scrutiny."
    ),
}


def tool_spec(role: str) -> dict:
    """JSON-schema-compatible single role review function definition."""
    if role not in ROLES:
        raise ValueError("Unsupported role")
    return {
        "name": "submit_" + role + "_review",
        "description": ROLE_TASKS[role],
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["approve", "reject", "abstain"]},
                "evidence_ref": {"type": "string", "minLength": 1, "maxLength": 256},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
                "observed_at": {"type": "string", "description": "Evidence observation UTC ISO 8601"},
            },
            "required": ["verdict", "evidence_ref", "rationale", "observed_at"],
        },
    }


def all_tool_specs() -> str:
    """Serialise the six roles for a trusted host's function-call registration."""
    return json.dumps([tool_spec(role) for role in ROLES], indent=2)


if __name__ == "__main__":
    print(all_tool_specs())

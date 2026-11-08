# Six-agent Kronos + Grok research desk (Phase 1)

> **Legacy prototype (2026-11-09).** The Phase 4 advisory-only desk is `trading/agents/desk.py` +
> `research/grok_advisory.py` ([phase4-grok-advisory.md](phase4-grok-advisory.md)). In the new desk agents can
> only advise and their outputs are recorded separately. This Phase 1 prototype, where role verdicts gate paper
> eligibility, is kept for reference and is not used by any runner.

This code establishes **contracts and safety gates** for six agents. It does **not** create six Grok accounts, create an xAI group chat, connect to Grok's services, call news feeds, or place exchange orders.

The six agent slots are:

| Role | Assignment | Must provide |
| --- | --- | --- |
| Scout | Find unusual Binance BTCUSDT conditions | Time-bounded candle evidence and provenance |
| News | Verify news and catalysts from primary sources | Reliable timestamped source reference or abstention |
| Chart | Inspect market structure and Kronos forecast | Historical support for price levels and uncertainty |
| Strategist | Propose hypothetical entry/stop/target | Explicit BTCUSDT spot long setup |
| Risk Manager | Challenge sizing and loss exposure | Approve/reject with independent rationale |
| Skeptic | Find reasons not to trade | Opposing evidence and explicit veto option |

Use `python -m research.agent_roles` to print six function-calling-style tool schemas for a future trusted Grok adapter. The schemas are **not** evidence of an actual Grok connection.

## Decision policy

1. Each opportunity has an ID, a fresh UTC timestamp, symbol `BTCUSDT`, long spot direction, positive entry/stop/target/notional and a 64-character forecast data SHA-256.
2. Six **different** role reviews must be present and recent, with non-empty referenced evidence and explanations. Any reject, abstain, duplicate, missing, malformed or stale review **vetoes** paper eligibility.
3. Independent deterministic limits cap notional (default 10% of equity), stop-loss exposure (0.5%), open positions (1), and daily realized losses (2%). These are illustrative conservative research limits, **not a validated risk model**.
4. **Default configuration: simulation disabled and kill switch on.** A trusted test harness must explicitly enable simulations after review. Even then, the output is only `paper_eligible`, not an order or simulated fill. There is **no broker connection**.
5. Every completed decision can be appended to a single-writer SHA-256 hash-chained JSONL log; tampering is detected on subsequent verification. Treat it as local research audit evidence, not as an immutable database.

### Important security limits

- Role messages and `evidence_ref` are **untrusted**. The library cannot prove an actual Grok bot created a message or that a linked news source is true. The future host adapter must authenticate bot identities, fetch/validate sources and reject reused or misattributed evidence.
- No external news API, xAI API key, group-chat membership management or scheduled 24/7 monitor has been configured.
- The audit log is single-writer and has no cross-process locking, digital signatures, or independent archival service; do not expose it as a public API.
- A model's consensus **cannot** disable the hard kill switch or deterministic risk limits.
- Historical Kronos evaluation **underperformed** buy-and-hold and cash in the tested window. Do not use model agreement to override the no-live-trading decision.

## Next steps to make the six Grok bots real

1. Set up or connect an authorized Grok Bot group and securely configure each specialist; check the provider's current bot/group-chat features and limits.
2. Create a trusted provider adapter for webhook/function calls, per-role identity binding, validated source evidence, idempotency and replay prevention.
3. Connect local `ForecastService` read-only (using authenticated access), Binance candles and primary-source news to the agents.
4. Persist decisions in a transactional database and add a paper-only execution engine, monitoring, restart recovery and alerts. Validate in an isolated sandbox before 24/7 operation.
5. Keep live trading, financial credentials and real exchange order placement disabled unless a separate empirical and security review explicitly changes the decision.

The six-agent group is a **coordination method**, not proof of predictive performance or profitability.

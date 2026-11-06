# Claude Code — Kronos Trading Research

Before making changes, read:
1. `docs/ARCHITECTURE.md` — approved dual-market boundaries, risk, and safety rules.
2. `docs/ROADMAP.md` — phased implementation, gates, and acceptance tests.
3. `docs/PROGRESS.md` — verified progress and next work items.
4. `README.md` — upstream Kronos usage; preserve upstream content unless needed.

## Non-negotiable configuration
- Two separately tested pipelines: XAUUSD (MetaTrader 5 demo) and BTC/USDT (Binance Spot Testnet).
- Automated **demo** execution only. Never place live orders, use production trading API keys, or enable automatic live mode.
- Pre-trade risk budget: intended risk <= 0.5% of current account equity per trade; fail closed if sizing or market/account metadata are uncertain. Losses may exceed this risk due to gaps/slippage.
- AI agents (Scout, News, Chart, Kronos Analyst, Skeptic, Coordinator), Kronos, and TradingView are inputs only; deterministic risk checks have veto authority.

## AI architecture (authoritative)
- **Trading intelligence = xAI Grok.** All six reasoning agents (Scout, News, Chart, Kronos Analyst, Skeptic, Coordinator) must call the **official xAI Grok API** (`https://api.x.ai`, key in `XAI_API_KEY`, never committed). No other LLM provider may act as a trading agent.
- **Forecasting = Kronos.** The Kronos model in `model/` is the only financial time-series forecaster; agents consume its output, they do not replace it.
- **Claude Code is the development tool only.** Claude writes, tests and documents code in this repository; it is not a runtime trading-intelligence provider and must not be wired into the decision path.
- **No AI bypass.** Agent, Kronos and TradingView outputs are advisory inputs recorded for provenance. Only `trading.risk.evaluate` (via `trading.gate.PreTradeGate`) can approve a demo order; no agent consensus, prompt or tool call may skip, override or relax it.
- Proposed additional per-account guardrails to test: daily drawdown halt 1.5%, aggregate stop-risk 1%; avoid presenting as validated strategies.

## Workflow on every session
1. Check `git status`, branch and repository state; do not overwrite uncommitted user work.
2. Read the docs listed above; independently verify claimed completed work in source and tests.
3. Implement the next **uncompleted** roadmap milestone in small reviewable increments.
4. Run actual tests; report failures, skipped checks, and why. Never fabricate performance or success claims.
5. Update `docs/PROGRESS.md` with evidence (files, tests, commit references), blockers and next action.
6. Stop for review before changing execution permissions, broker connections, API permissions, or any high-impact action.

## Current assignment
Start with Phase 0 repository inventory and gap analysis, then Phase 1 foundations/risk unit tests. Do not proceed to live trading. Preserve upstream Kronos history and functionality. Existing documentation PR is draft #9.

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

## Market calendar and data safety
- Calendar data must come from identified, verifiable providers (`trading/marketcal/`): CME gold holiday rules, Federal Reserve, BEA, ECB, BLS API values, operator/licensed structured files. Never invent schedules; closures are unioned; stale, missing or contradictory calendar data rejects new entries.
- The calendar service and risk engine run without Grok. The News Agent may interpret and recommend, but its output is validated by `trading/agents/news.py` and can never change schedules, closures, stops, risk limits or execution mode.
- Venue connectors stay read-only until Phase 3 is explicitly authorized (MT5 allowlist proxy; Binance GET-only).
- Never assume a broker's server time zone, tick value or session hours: verify against a fresh tick, the terminal's own P&L calculation (`order_calc_profit`) and the broker's bar history (MetaQuotes-Demo: EET/EU DST, misreported XAUUSD tick value, 2026 session change).
- Tests needing live network, a broker terminal or credentials are marked `integration` and are deselected by default; report them separately and never count skips as passes.

## Current assignment
Phases 0–1 are done (PR #10); Phase 2/2A/2C are in review (PR #11, stacked on #10 → #9); Phase 2D shadow forward testing (read-only, simulation only) is on `feat/phase2d-shadow-forward-test`, stacked on PR #11. Strategy parameters under forward test are frozen by `research/forward_test/phase2c_xauusd_preregistration.json`; never edit that file or the shadow config's strategy parameters during the evaluation. Phase 2E BTC/USDT shadow trading (public data, simulation only) is on `feat/phase2e-binance-shadow` (worktree `../kronos-binance-shadow`, stacked on #12) and frozen by `research/forward_test/phase2e_btcusdt_preregistration.json`. Do not start Phase 3 execution adapters, merge PRs, or enable order placement without explicit authorization. Progress status vocabulary in `docs/PROGRESS.md`: Completed, Verified, Partially implemented, Blocked, Not started.

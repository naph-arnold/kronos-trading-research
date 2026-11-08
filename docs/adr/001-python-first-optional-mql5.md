# ADR-001: Python-first dual-market research; MQL5 only when justified

**Status:** Approved architecture decision (2026-11-08). **Scope:** design and milestone sequencing; does not authorize order execution.

## Decision

**Python is the central runtime for orchestration, strategies, Kronos forecasting, eventual official xAI Grok reasoning agents, trading calendar, deterministic risk engine, audit, and monitoring.** Treat MT5/XAUUSD and Binance/BTCUSDT as independent market-specific pipelines with independent state, guards, breakers, and performance measures.

1. **XAUUSD now:** Python connects to a *verified demo-only* MetaTrader 5 terminal for **read-only** quotes, symbol/contract specifications, sessions, tick history and OHLCV bars. Historical backtests and an isolated, frozen New York ORB shadow forward test remain simulation-only. Do not call MT5 trade/order APIs.
2. **BTCUSDT now:** Python uses official Binance public, read-only market data for research and a separately isolated crypto shadow pipeline. Binance Spot Testnet signed **GET** account metadata is permissible only with testnet credentials and explicit connection review. No live or testnet order submission in this research/shadow stage.
3. **Later, not now:** Introduce a native **MQL5 Expert Advisor** only if an evidence-based comparison shows that direct MetaTrader Strategy Tester integration, local order handling, execution latency/reliability, or native protective controls provide a material, tested advantage over a Python-backed MT5 demo adapter. MQL5 is an **optional venue-specific execution/testing component**, never a replacement for Python orchestration, Kronos or Grok.

## Explicit decision gate for considering MQL5

Before authoring or enabling a new execution EA:
- Finish and separately verify both Python-based shadow pipelines and record their reproducible test results.
- Produce a short design comparison: (a) terminal-backed Python MT5 adapter versus (b) minimal MQL5 EA, including Strategy Tester coverage, latency, operational reliability, maintenance and failure modes. No unsupported claim of zero latency.
- Define one signed/versioned decision envelope approved by the **existing deterministic Python risk engine**; any MQL5 component must validate identity, TTL, instrument and risk constraints itself, reject unknown or stale state, and reconcile every result. No independent EA strategy that bypasses the Python risk gate.
- Demonstrate correct handling of MT5 rejections, restarts, disconnections, time zones, duplicate requests, stops, slippage and account reconciliation on **demo only**.
- Seek explicit approval for Phase 3 demo execution. Do not enable any real-money account or trade permission. Full AI+Python integration cannot be assumed to run inside MT5 Strategy Tester; use offline recorded/replayed AI decisions for comparable tests.

## Verification and implementation status (2026-11-08)

- Python MT5 read-only connector: implemented in `trading/connectors/mt5.py` on the Phase 2 stack; live demo read-only smoke checks reported. No MT5 order routing.
- Python Binance research connector: implemented in `trading/connectors/binance.py` on the Phase 2 stack. BTCUSDT backtesting exists. A continuously running **crypto shadow** pipeline is **not confirmed complete** in the reviewed branch; it is separate ongoing work.
- Gold shadow system: implemented on PR #12 but genuine forward evidence is still being collected; do not modify frozen parameters.
- MQL5 execution EA: **not implemented and not currently authorized**.
- The 0.5% intended equity-risk ceiling, 1.5% daily halt, 1% aggregate open-risk guardrails, no-order research restriction, and separate state per market remain mandatory.

## Acceptance check for future changes

A future change complies only if: Python stays the central engine; both venue connectors remain read-only until separately approved execution; XAUUSD and BTCUSDT shadow trading do not share account state or kill-switch scope; MQL5 is introduced only after the documented comparison and authorization; automated tests show no route from AI or TradingView to broker order submission without the deterministic risk gate.

Related: [Architecture](../ARCHITECTURE.md), [Roadmap](../ROADMAP.md), [Progress](../PROGRESS.md).

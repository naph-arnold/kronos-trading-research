"""Phase 2D shadow forward-testing (XAUUSD). Simulation only: this package has no order-routing code.

Live market data arrives through the read-only MT5 connector; signals pass through the unchanged
deterministic risk engine; fills, stops, targets, spreads, slippage, commission and swap are simulated
against live ticks; every decision and simulated fill is persisted in SQLite.
"""

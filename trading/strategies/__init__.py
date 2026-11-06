"""Research strategies. Each is a hypothesis to evaluate, not a validated or profitable system.

Strategies are stateful callables fed one closed bar at a time through
`trading.replay.HistoryView`; create a fresh instance per run. They emit
`TradeSignal` candidates only; the deterministic risk engine decides.
"""

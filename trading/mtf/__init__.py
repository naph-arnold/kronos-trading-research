"""Multi-timeframe research framework (research only; no orders).

`frames`   causal resampling of base candles to higher timeframes and completed-candle alignment.
`features` mathematically defined, causal structure features (EMA, ATR, fractal swings, structure trend).
`strategies` Strategy A (selective 4H->1H->15M->5M), Strategy B (scalping 1H->15M->5M->1M), baselines.
`simulator` event simulation on the execution timeframe; every entry goes through `trading.risk.evaluate`.
Hybrid (A+B under one account) = simulate() on merged candidates with AccountRules (priority, conflicts, caps).
Advisory interfaces (Kronos / Grok / TradingView, recorded only) live in trading/agents/mtf_advisory.py.
"""

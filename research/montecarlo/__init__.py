"""Kronos Monte Carlo validation (research only).

Resamples *recorded, closed, net-of-cost* trades to study path risk (drawdown, losing streaks, recovery, threshold
crossings) and sensitivity to ordering, dependence, regime mix, execution costs and position sizing.

It never changes strategy signals, shadow runners, trading permissions or operational risk controls, and it can
never turn a failed historical validation into a pass. See docs/monte-carlo-validation.md.
"""

ALGORITHM_VERSION = "kronos-mc-1.0.0"
SCHEMA_VERSION = 1

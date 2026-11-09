"""Kronos Trading Command Center V1: a read-only monitoring dashboard.

Python stays the engine: this package only READS the shadow runners' SQLite stores (`mode=ro`) and recorded market
history (CSV), reusing `research.dual_health_report` and `trading.shadow.report`. It has no write endpoint, cannot
create STOP/KILL files, start or stop runners, call a venue or place an order. It runs as its own process
(`python -m command_center`), independent of the runners.
"""

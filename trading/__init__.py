"""Shared, deterministic foundations for the dual-market *demo* trading system.

This package contains no broker/exchange order routing. Live execution is
deliberately unrepresentable: `trading.config.ExecutionMode` has only a DEMO
member, and every risk evaluation re-verifies the demo environment.
"""

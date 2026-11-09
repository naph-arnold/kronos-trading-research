"""Adverse execution scenarios: explicitly assumed EXTRA costs applied on top of each trade's recorded costs.

These are assumptions for stress testing, never observations. Each scenario states its units:
  XAUUSD (MT5, lots; contract size from the report's broker specification):
    extra_spread       price units per round trip
    extra_slippage     price units per side (entry and exit)
    latency_slippage   price units per side (delayed fill)
    extra_commission   USD per lot round trip
  BTC/USDT (base units):
    extra_bps_per_side fee + slippage + latency, basis points of notional on entry and exit
The adjusted R = recorded net R - extra cost / planned risk (initial risk in account currency).
"""
from __future__ import annotations

import numpy as np

from research.montecarlo.records import TradeDataset


def extra_cost_usd(ds: TradeDataset, scenario: dict) -> np.ndarray:
    if not scenario or scenario.get("kind") == "recorded":
        return np.zeros(len(ds.trades))
    if ds.market == "xauusd":
        contract = float(scenario["contract_size"])
        per_price = (float(scenario.get("extra_spread", 0)) + 2 * float(scenario.get("extra_slippage", 0))
                     + 2 * float(scenario.get("latency_slippage", 0)))
        return np.array([t.size * contract * per_price + t.size * float(scenario.get("extra_commission", 0))
                         for t in ds.trades])
    bps = float(scenario.get("extra_bps_per_side", 0)) / 1e4
    return np.array([bps * (t.entry + t.exit) * t.size for t in ds.trades])


def adjusted_r(ds: TradeDataset, scenario: dict) -> np.ndarray:
    base = np.array([t.r_net for t in ds.trades], dtype=float)
    risk = np.array([t.planned_risk for t in ds.trades], dtype=float)
    return base - extra_cost_usd(ds, scenario) / risk

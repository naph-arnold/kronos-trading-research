"""Strictly chronological, cost-aware *simulated* spot backtesting.

Forecasts are created from candles strictly before the target candle. Signals
are decided before the target opens; fills are approximated at its open and
close. No exchange order routes, leverage, shorting or real funds.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from research.datasets import load_verified
from research.market_data import DataValidationError, parse_time


def _safe_number(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"Invalid {name}") from exc
    if not math.isfinite(result):
        raise DataValidationError(f"Nonfinite {name}")
    return result


def _drawdown(equity_curve: list[float]) -> float:
    peak, worst = equity_curve[0], 0.0
    for value in equity_curve:
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    return worst


def backtest(
    candles: list[dict], forecasts: list[dict], *, initial_equity: float = 10_000.0,
    fee_bps: float = 10, spread_bps: float = 2, slippage_bps: float = 3,
    min_forecast_edge_bps: float = 20,
) -> dict:
    """Spot long-or-cash one-bar round-trip with fills at target open/close.

    entry=target_open*(1+(spread/2+slippage)/10000)
    exit=target_close*(1-(spread/2+slippage)/10000)
    both sides pay fee_bps; never assume free frictionless execution.

    A risk-free cash return of zero and passive buy-and-hold are benchmarks.
    """
    costs = [initial_equity, fee_bps, spread_bps, slippage_bps, min_forecast_edge_bps]
    if not all(math.isfinite(x) for x in costs) or initial_equity <= 0 or any(x < 0 for x in costs[1:]):
        raise ValueError("Initial equity must be positive and all costs finite/nonnegative")
    if fee_bps >= 10_000 or (spread_bps / 2 + slippage_bps) >= 10_000:
        raise ValueError("Cost assumptions are too large")
    if len(candles) < 2 or not forecasts:
        raise ValueError("Need historical candles and out-of-sample forecasts")

    by_timestamp = {row["timestamps"]: idx for idx, row in enumerate(candles)}
    if len(by_timestamp) != len(candles):
        raise DataValidationError("Duplicate candle timestamp")
    prev_idx = None
    equity = initial_equity
    curve = [equity]
    trades = []
    decisions = []
    one_way_price = (spread_bps / 2 + slippage_bps) / 10_000
    fee = fee_bps / 10_000

    for item in forecasts:
        timestamp = item["timestamp"]
        if timestamp not in by_timestamp:
            raise DataValidationError("Forecast target missing from dataset")
        idx = by_timestamp[timestamp]
        if idx == 0 or (prev_idx is not None and idx <= prev_idx):
            raise DataValidationError("Forecasts must be strictly chronological, after one full candle")
        prev_idx = idx
        target = candles[idx]
        prev_close = _safe_number(candles[idx - 1]["close"], "previous close")
        claimed_prev = _safe_number(item["previous_close"], "claimed previous close")
        predicted = _safe_number(item["predicted_close"], "forecast close")
        claimed_actual = _safe_number(item["actual_close"], "claimed actual close")
        opened = _safe_number(target["open"], "open")
        closed = _safe_number(target["close"], "close")
        if min(prev_close, claimed_prev, predicted, claimed_actual, opened, closed) <= 0:
            raise DataValidationError("Prices must be positive")
        if not math.isclose(prev_close, claimed_prev, rel_tol=1e-10, abs_tol=1e-7) or not math.isclose(
            claimed_actual, closed, rel_tol=1e-10, abs_tol=1e-7
        ):
            raise DataValidationError("Forecast data does not match validated OHLC history")

        edge_bps = (predicted / prev_close - 1) * 10_000
        entered = edge_bps > min_forecast_edge_bps
        starting_equity = equity
        if entered:
            buy = opened * (1 + one_way_price)
            sell = closed * (1 - one_way_price)
            gross_factor = (sell / buy) * (1 - fee) ** 2
            equity *= gross_factor
            trades.append({
                "timestamp": timestamp, "signal_edge_bps": edge_bps,
                "entry_fill": buy, "exit_fill": sell,
                "gross_candle_return": closed / opened - 1,
                "net_return": gross_factor - 1,
                "equity_after": equity,
            })
        decisions.append({"timestamp": timestamp, "long": entered, "equity_after": equity,
                          "signal_edge_bps": edge_bps, "net_change": equity / starting_equity - 1})
        curve.append(equity)

    first = candles[by_timestamp[forecasts[0]["timestamp"]]]
    last = candles[by_timestamp[forecasts[-1]["timestamp"]]]
    first_open = _safe_number(first["open"], "benchmark start")
    last_close = _safe_number(last["close"], "benchmark end")
    # A single buy at first target open and sell at last target close.
    passive_factor = (last_close * (1 - one_way_price) / (first_open * (1 + one_way_price))) * (1 - fee) ** 2
    return {
        "research_only": True, "execution": "hypothetical next-candle open / same-candle close",
        "initial_equity": initial_equity, "final_equity": equity,
        "net_return_pct": 100 * (equity / initial_equity - 1),
        "max_drawdown_pct": 100 * _drawdown(curve),
        "trades": len(trades), "evaluated_candles": len(forecasts),
        "win_rate_pct": 100 * sum(t["net_return"] > 0 for t in trades) / len(trades) if trades else 0.0,
        "buy_hold_return_pct": 100 * (passive_factor - 1),
        "cash_return_pct": 0.0,
        "cost_assumptions_bps": {
            "per_side_fee": fee_bps, "total_spread": spread_bps,
            "per_side_slippage": slippage_bps, "minimum_signal_edge": min_forecast_edge_bps,
        },
        "decisions": decisions, "trade_ledger": trades,
        "limitations": "Research only: assumes a fill at the target bar open without latency, no market impact, no orders, no funding; tiny samples are unreliable.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate long/cash Binance spot strategy; no live orders")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--forecast-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--spread-bps", type=float, default=2)
    parser.add_argument("--slippage-bps", type=float, default=3)
    parser.add_argument("--edge-bps", type=float, default=20)
    args = parser.parse_args()

    manifest = json.loads(args.csv.with_suffix(args.csv.suffix + ".json").read_text(encoding="utf-8"))
    candles = load_verified(args.csv, symbol="BTCUSDT", interval="1h",
                            start_ms=manifest["start_ms"], source="binance_spot")
    report = json.loads(args.forecast_json.read_text(encoding="utf-8"))
    if (report.get("dataset_sha256") != manifest["sha256"]
            or report.get("symbol") != "BTCUSDT" or report.get("interval") != "1h"):
        raise DataValidationError("Forecast report does not belong to this Binance dataset")
    result = backtest(candles, report["forecasts"], fee_bps=args.fee_bps,
                      spread_bps=args.spread_bps, slippage_bps=args.slippage_bps,
                      min_forecast_edge_bps=args.edge_bps)
    result["dataset_sha256"] = manifest["sha256"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("trade_ledger", "decisions")}, indent=2))


if __name__ == "__main__":
    main()

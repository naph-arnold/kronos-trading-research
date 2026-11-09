"""Market-regime labels for recorded trades, from verified data only.

The label is the research code's own causal rule (`trading.evaluation.regime_labels`: close vs EMA200 for trend,
ATR(14)% vs its rolling 500-bar median for volatility), evaluated on the hash-verified dataset that produced the
trades and keyed at the trade's opening bar close, exactly as `trading.evaluation.by_regime` does. Labels are
accepted only if they reproduce the source report's per-regime trade counts and net P&L; otherwise the dataset
keeps no regime labels and regime-conditional sampling is reported as unsupported.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from research.montecarlo.records import RecordError, TradeDataset


def _bars_xau(csv_path: Path, expected_sha: str):
    from research.phase2_xauusd import load_mt5_csv

    rows, manifest = load_mt5_csv(csv_path)
    if manifest["sha256"] != expected_sha:
        raise RecordError("XAUUSD dataset differs from the report's dataset")
    return [r.bar for r in rows]


def _bars_btc(csv_path: Path, expected_sha: str):
    from research.datasets import load_verified
    from trading.marketdata import bar_from_research_row

    manifest = json.loads(csv_path.with_suffix(csv_path.suffix + ".json").read_text(encoding="utf-8"))
    if manifest["sha256"] != expected_sha or hashlib.sha256(csv_path.read_bytes()).hexdigest() != expected_sha:
        raise RecordError("BTC/USDT dataset differs from the report's dataset")
    rows = load_verified(csv_path, symbol="BTCUSDT", interval=manifest.get("interval", "1h"),
                         start_ms=manifest["start_ms"], source=manifest.get("source", "binance_spot"))
    return [bar_from_research_row(r, symbol="BTCUSDT", interval=manifest.get("interval", "1h")) for r in rows]


def attach_report_regimes(ds: TradeDataset, csv_path: Path, report_by_regime: dict) -> dict:
    """Label each trade; accept only if per-regime counts and net P&L match the report exactly (4 dp)."""
    from trading.evaluation import regime_labels

    sha = ds.provenance.get("dataset_sha256")
    bars = (_bars_xau if ds.market == "xauusd" else _bars_btc)(Path(csv_path), sha)
    labels = regime_labels(bars)
    del bars
    labelled = [replace(t, regime=labels.get(datetime.fromisoformat(t.opened_at), "unknown")) for t in ds.trades]
    groups: dict[str, list[float]] = {}
    for t in labelled:
        groups.setdefault(t.regime, []).append(t.net_pnl)
    mine = {k: {"trades": len(v), "net_pnl": round(sum(v), 4)} for k, v in groups.items()}
    theirs = {k: {"trades": v["trades"], "net_pnl": round(v["net_pnl"], 4)} for k, v in report_by_regime.items()}
    ok = mine.keys() == theirs.keys() and all(
        mine[k]["trades"] == theirs[k]["trades"] and abs(mine[k]["net_pnl"] - theirs[k]["net_pnl"]) < 1e-3 for k in mine)
    result = {"rule": "trading.evaluation.regime_labels (EMA200 trend x ATR14% vs rolling-500 median), causal, "
                      "keyed at the opening bar close", "dataset_sha256": sha, "reproduces_report": ok,
              "counts": {k: v["trades"] for k, v in sorted(mine.items())}}
    if ok:
        ds.trades = labelled
        ds.regime_source = result
    else:
        result["report"] = theirs
        result["computed"] = mine
        ds.regime_source = {**result, "used": False}
    return result

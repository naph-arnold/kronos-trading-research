"""Leakage-safe, one-step Kronos forecasting evaluation for verified Binance candles.

Research only: predictions and baseline comparison. No order execution.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from research.datasets import load_verified


def build_windows(row_count: int, lookback: int, evaluations: int) -> list[tuple[int, int]]:
    """Each target at index t is predicted using only rows [t-lookback, t)."""
    if lookback < 16 or evaluations < 1 or row_count < lookback + evaluations:
        raise ValueError("Not enough history for nonoverlapping chronological target indices")
    return [(t - lookback, t) for t in range(row_count - evaluations, row_count)]


def evaluate_rows(rows: list[dict], *, lookback: int = 128, evaluations: int = 8,
                  predictor=None) -> dict:
    """Walk forward one candle at a time; ground truth is never in the model input.

    A custom predictor is accepted for offline tests. With no predictor, a
    compatible pretrained Kronos-mini model is fetched from Hugging Face.
    """
    import pandas as pd
    import numpy as np

    windows = build_windows(len(rows), lookback, evaluations)
    frame = pd.DataFrame(rows)
    frame["timestamps"] = pd.to_datetime(frame["timestamps"], utc=True)
    for field in ("open", "high", "low", "close"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    if not np.isfinite(frame[["open", "high", "low", "close"]].to_numpy()).all():
        raise ValueError("Nonfinite input candles")
    if predictor is None:
        import torch
        from model import Kronos, KronosPredictor, KronosTokenizer
        random.seed(123)
        np.random.seed(123)
        torch.manual_seed(123)
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        model.eval()
        tokenizer.eval()
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)

    forecasts = []
    for begin, t in windows:
        context = frame.iloc[begin:t]
        target = frame.iloc[t]
        predicted = predictor.predict(
            df=context[["open", "high", "low", "close"]].reset_index(drop=True),
            x_timestamp=context["timestamps"].reset_index(drop=True),
            y_timestamp=frame["timestamps"].iloc[t:t + 1].reset_index(drop=True),
            pred_len=1,
            T=1.0,
            top_k=1,
            top_p=1.0,
            sample_count=1,
            verbose=False,
        )
        predicted_close = float(predicted["close"].iloc[0])
        if not np.isfinite(predicted_close) or predicted_close <= 0:
            raise ValueError("Model returned invalid predicted close")
        forecasts.append({
            "timestamp": target["timestamps"].isoformat(),
            "previous_close": float(context["close"].iloc[-1]),
            "actual_close": float(target["close"]),
            "predicted_close": predicted_close,
        })

    actual = np.asarray([x["actual_close"] for x in forecasts])
    model_values = np.asarray([x["predicted_close"] for x in forecasts])
    baseline = np.asarray([x["previous_close"] for x in forecasts])
    model_mae = float(np.mean(np.abs(model_values - actual)))
    baseline_mae = float(np.mean(np.abs(baseline - actual)))
    return {
        "model": "NeoQuasar/Kronos-mini",
        "baseline": "previous_close",
        "evaluations": len(forecasts),
        "lookback": lookback,
        "model_mae": model_mae,
        "baseline_mae": baseline_mae,
        "model_better_than_baseline": model_mae < baseline_mae,
        "forecasts": forecasts,
        "warning": "Research-only; small evaluation samples do not establish tradable profitability.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate pretrained Kronos-mini against previous-close baseline")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--lookback", type=int, default=128)
    parser.add_argument("--evaluations", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.csv.with_suffix(args.csv.suffix + ".json").read_text(encoding="utf-8"))
    rows = load_verified(args.csv, symbol=args.symbol, interval=args.interval,
                         start_ms=manifest["start_ms"], source="binance_spot")
    result = evaluate_rows(rows, lookback=args.lookback, evaluations=args.evaluations)
    result["dataset_sha256"] = manifest["sha256"]
    result["symbol"] = args.symbol
    result["interval"] = args.interval
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "forecasts"}, indent=2))


if __name__ == "__main__":
    main()

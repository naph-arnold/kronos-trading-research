"""Deterministic offline tests for no-lookahead model evaluation."""
import pytest

from research.evaluate_kronos import build_windows, evaluate_rows


def rows(n=48):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{
        "timestamps": (start + timedelta(hours=i)).isoformat(),
        "open": str(100 + i),
        "high": str(102 + i),
        "low": str(99 + i),
        "close": str(101 + i),
        "volume": "1",
        "amount": "100",
    } for i in range(n)]


class FakePredictor:
    def __init__(self):
        self.calls = []

    def predict(self, **kwargs):
        import pandas as pd
        inputs = kwargs["df"]
        x_ts = kwargs["x_timestamp"]
        y_ts = kwargs["y_timestamp"]
        assert len(y_ts) == 1
        assert x_ts.max() < y_ts.iloc[0]
        assert len(inputs) == 32
        self.calls.append((float(inputs["close"].iloc[-1]), y_ts.iloc[0]))
        return pd.DataFrame({"close": [float(inputs["close"].iloc[-1])]})


def test_window_bounds_and_no_lookahead():
    assert build_windows(48, 32, 3) == [(13, 45), (14, 46), (15, 47)]
    with pytest.raises(ValueError):
        build_windows(10, 32, 1)
    with pytest.raises(ValueError):
        build_windows(48, 32, 17)


def test_evaluation_is_held_out_and_baseline_is_fair():
    fake = FakePredictor()
    result = evaluate_rows(rows(), lookback=32, evaluations=3, predictor=fake)
    assert len(fake.calls) == 3
    assert result["evaluations"] == 3
    assert result["model_mae"] == pytest.approx(1)
    assert result["baseline_mae"] == pytest.approx(1)
    assert result["model_better_than_baseline"] is False
    assert [f["actual_close"] for f in result["forecasts"]] == [146, 147, 148]


def test_nonfinite_rejected_before_inference():
    data = rows()
    data[-1]["close"] = "NaN"
    with pytest.raises(ValueError, match="Nonfinite"):
        evaluate_rows(data, lookback=32, evaluations=3, predictor=FakePredictor())

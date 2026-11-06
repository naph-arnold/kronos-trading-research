"""Read-only, deterministic Kronos forecast adapter for automated research agents.

The adapter loads only a locally configured, SHA-256 verified Binance dataset.
No trade execution, arbitrary file selection, Grok credentials or broker keys.
"""
from __future__ import annotations

import math
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research.datasets import load_verified
from research.market_data import DataValidationError, INTERVAL_MS, parse_time

MODEL_ID = "NeoQuasar/Kronos-mini"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-2k"
MODEL_CONFIG = {"temperature": 1.0, "top_k": 1, "top_p": 1.0, "sample_count": 1}
MODEL_LOCK = threading.Lock()


class ForecastService:
    def __init__(self, dataset: Path, *, lookback: int = 128, predictor=None):
        if lookback < 16 or lookback > 512:
            raise ValueError("Lookback must be between 16 and 512")
        self.dataset = dataset
        self.lookback = lookback
        self._predictor = predictor

    def _get_predictor(self):
        if self._predictor is None:
            import numpy as np
            import torch
            from model import Kronos, KronosPredictor, KronosTokenizer
            random.seed(123)
            np.random.seed(123)
            torch.manual_seed(123)
            tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
            model = Kronos.from_pretrained(MODEL_ID)
            tokenizer.eval()
            model.eval()
            self._predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
        return self._predictor

    def forecast(self, *, horizon: int = 1) -> dict:
        """Forecast future *closed* Binance spot candles, without trade advice.

        horizon must be 1..24 and is always after the latest stored candle.
        Dataset path is server-configured, never selected by a remote client.
        """
        if type(horizon) is not int or not 1 <= horizon <= 24:
            raise ValueError("horizon must be an integer between 1 and 24")
        import json

        import numpy as np
        import pandas as pd

        metadata = json.loads(self.dataset.with_suffix(self.dataset.suffix + ".json").read_text(encoding="utf-8"))
        if metadata.get("symbol") != "BTCUSDT" or metadata.get("interval") != "1h" or metadata.get("source") != "binance_spot":
            raise DataValidationError("Only verified Binance BTCUSDT hourly data is supported")
        rows = load_verified(
            self.dataset, symbol="BTCUSDT", interval="1h",
            start_ms=metadata["start_ms"], source="binance_spot",
        )
        if len(rows) < self.lookback:
            raise DataValidationError("Insufficient verified historical candles")
        step = INTERVAL_MS["1h"]
        last_ms = parse_time(rows[-1]["timestamps"])
        # Fail closed on data produced in the future or on stale market data.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if last_ms + step > now_ms:
            raise DataValidationError("Dataset includes a candle that has not yet closed")
        if now_ms - (last_ms + step) > 2 * step:
            raise DataValidationError("Dataset stale: refresh verified Binance candles before forecasting")
        frame = pd.DataFrame(rows[-self.lookback:])
        frame["timestamps"] = pd.to_datetime(frame["timestamps"], utc=True)
        for field in ("open", "high", "low", "close"):
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        if not np.isfinite(frame[["open", "high", "low", "close"]].to_numpy()).all():
            raise DataValidationError("Nonfinite market prices")
        future = pd.Series([pd.Timestamp(last_ms + (offset + 1) * step, unit="ms", tz="UTC")
                            for offset in range(horizon)])
        with MODEL_LOCK:
            predictor = self._get_predictor()
            output = predictor.predict(
                df=frame[["open", "high", "low", "close"]].reset_index(drop=True),
                x_timestamp=frame["timestamps"].reset_index(drop=True),
                y_timestamp=future, pred_len=horizon,
                verbose=False, **dict(T=1.0, top_k=1, top_p=1.0, sample_count=1),
            )
        if len(output) != horizon:
            raise DataValidationError("Model returned the wrong forecast horizon")
        forecasts = []
        for i in range(horizon):
            predicted = {}
            for field in ("open", "high", "low", "close"):
                value = float(output[field].iloc[i])
                if not math.isfinite(value) or value <= 0:
                    raise DataValidationError("Model returned invalid prices")
                predicted[field] = value
            forecasts.append({"timestamp": future.iloc[i].isoformat(), **predicted})
        return {
            "schema_version": 1,
            "mode": "research_only",
            "trading_enabled": False,
            "source": "binance_spot",
            "symbol": "BTCUSDT",
            "interval": "1h",
            "model": MODEL_ID,
            "tokenizer": TOKENIZER_ID,
            "sampling": MODEL_CONFIG.copy(),
            "lookback": self.lookback,
            "horizon": horizon,
            "last_closed_candle_timestamp": rows[-1]["timestamps"],
            "dataset_sha256": metadata["sha256"],
            "last_observed_close": float(rows[-1]["close"]),
            "forecast": forecasts,
            "recommendation": None,
            "warning": "Uncalibrated research forecast; no trade signal, risk clearance, or profitability guarantee.",
        }

"""Causal multi-timeframe frames.

Conventions (all UTC):
* A candle is identified by its open time; it is complete at `close_time = open_time + interval`.
* Higher timeframes are built from base candles by UTC-aligned buckets (4H buckets start at 00:00, 04:00, ...).
  A bucket is usable only after its close time; buckets with missing base candles (exchange gaps, market
  closures) aggregate what exists and are flagged `partial`.
* `align(base, htf)` maps each base candle to the most recent higher-timeframe candle whose close time is
  <= the base candle's close time. A higher-timeframe candle is therefore never visible before it has closed
  (no look-ahead).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


@dataclass
class Frame:
    """OHLCV candles of one timeframe as float arrays (research arithmetic; decisions re-check in Decimal)."""

    interval: str
    df: pd.DataFrame  # index: open_time (UTC, tz-aware); columns open, high, low, close, volume, [spread]

    @property
    def open_time(self) -> np.ndarray:
        return self.df.index.values.astype("datetime64[ns]").astype("int64")

    @property
    def close_ns(self) -> np.ndarray:
        return self.open_time + MINUTES[self.interval] * 60_000_000_000

    def __len__(self) -> int:
        return len(self.df)


def validate_base(df: pd.DataFrame, interval: str) -> None:
    if df.index.tz is None or str(df.index.tz) != "UTC":
        raise ValueError("Base candles must be indexed by UTC open time")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("Base candles must be strictly increasing")
    step = pd.Timedelta(minutes=MINUTES[interval])
    if ((df.index - df.index.normalize()) % step != pd.Timedelta(0)).any():
        raise ValueError(f"Base candles not aligned to {interval}")
    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    if bad.any():
        raise ValueError(f"{int(bad.sum())} inconsistent OHLC candles")


def resample(base: Frame, interval: str) -> Frame:
    """UTC-aligned aggregation of complete base candles. Only buckets whose close time has passed relative to
    the last base candle are emitted (the still-forming bucket is dropped)."""
    if MINUTES[interval] < MINUTES[base.interval] or MINUTES[interval] % MINUTES[base.interval]:
        raise ValueError("Target interval must be a multiple of the base interval")
    rule = f"{MINUTES[interval]}min"
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "spread" in base.df:
        agg["spread"] = "max"
    out = base.df.resample(rule, label="left", closed="left", origin="epoch").agg(agg)
    counts = base.df["close"].resample(rule, label="left", closed="left", origin="epoch").count()
    out = out[counts > 0].copy()
    out["partial"] = (counts[counts > 0] < MINUTES[interval] // MINUTES[base.interval]).values
    last_base_close = base.df.index[-1] + pd.Timedelta(minutes=MINUTES[base.interval])
    out = out[out.index + pd.Timedelta(minutes=MINUTES[interval]) <= last_base_close]
    return Frame(interval, out)


def align(base: Frame, htf: Frame) -> np.ndarray:
    """For each base candle, the index of the latest higher-timeframe candle closed at or before the base
    candle's close (-1 if none yet)."""
    return np.searchsorted(htf.close_ns, base.close_ns, side="right") - 1


def take(values: np.ndarray, index: np.ndarray, fill=np.nan) -> np.ndarray:
    """values[index] with -1 -> fill (for aligned higher-timeframe features)."""
    out = np.full(index.shape, fill, dtype=float)
    ok = index >= 0
    out[ok] = values[index[ok]]
    return out


def from_csv(path, *, time_col: str, columns: dict, spread_col: str | None = None, spread_scale: float = 1.0) -> pd.DataFrame:
    """Load a verified CSV (checksums are verified by the caller) into a UTC-indexed float frame."""
    raw = pd.read_csv(path)
    df = pd.DataFrame({k: pd.to_numeric(raw[v], errors="raise") for k, v in columns.items()})
    df.index = pd.to_datetime(raw[time_col], utc=True)
    df.index.name = "open_time"
    if spread_col:
        df["spread"] = pd.to_numeric(raw[spread_col]) * spread_scale
    return df

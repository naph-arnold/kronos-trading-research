"""Candles from recorded data only: research CSV history plus the runners' shadow stores (read-only).

No candle is ever synthesised from nothing. Larger timeframes may be *aggregated* from smaller real bars (for
example 4H from 1H); every response says which source and method produced each segment, where the data ends,
and which timeframes are unavailable for a market.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from command_center.readonly import connect_ro

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
UTC = timezone.utc


def tf_seconds(tf: str) -> int:
    if tf not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {tf!r}; available: {list(TIMEFRAMES)}")
    return TIMEFRAMES[tf]


def floor_time(t: datetime, seconds: int) -> datetime:
    epoch = int(t.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def parse_time(text: str) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Candle:
    time: datetime  # bar open, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str  # "history" | "live" | "warmup_history"

    def to_json(self) -> dict:
        return {"time": int(self.time.timestamp()), "open": self.open, "high": self.high, "low": self.low,
                "close": self.close, "volume": self.volume, "source": self.source}


# --------------------------------------------------------------------------- CSV history (sorted by time)

class CsvBars:
    """A time-sorted OHLC CSV read by byte-offset bisection, so a 200 MB file costs a few reads per request.
    Accepts the repository's two layouts: MT5 (`open_time_utc,...,tick_volume`) and Binance
    (`timestamps,open,high,low,close,volume,amount`)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        with self.path.open("rb") as fh:
            header = fh.readline().decode().strip().split(",")
            self._data_start = fh.tell()
        self.cols = {name: i for i, name in enumerate(header)}
        self.t_col = self.cols.get("open_time_utc", self.cols.get("timestamps"))
        self.v_col = self.cols.get("tick_volume", self.cols.get("volume"))
        if self.t_col is None or not {"open", "high", "low", "close"} <= set(self.cols):
            raise ValueError(f"unrecognised bar file layout: {self.path.name}")
        self.size = os.path.getsize(self.path)

    def _parse(self, line: bytes) -> Candle | None:
        parts = line.decode().strip().split(",")
        if len(parts) < len(self.cols):
            return None
        c = self.cols
        vol = float(parts[self.v_col]) if self.v_col is not None and parts[self.v_col] else None
        return Candle(parse_time(parts[self.t_col]), float(parts[c["open"]]), float(parts[c["high"]]),
                      float(parts[c["low"]]), float(parts[c["close"]]), vol, "history")

    def _time_at(self, fh, offset: int) -> tuple[int, datetime | None]:
        """First full line starting at or after `offset`: (its start offset, its time)."""
        if offset <= self._data_start:
            fh.seek(self._data_start)
        else:
            fh.seek(offset - 1)
            fh.readline()  # finish the partial line
        start = fh.tell()
        line = fh.readline()
        if not line.strip():
            return self.size, None
        bar = self._parse(line)
        return start, None if bar is None else bar.time

    def _offset_after(self, fh, until: datetime) -> int:
        """Byte offset of the first line whose time is > until."""
        # Predicate "first line starting at/after offset has time > until" is monotone in offset.
        lo, hi = self._data_start, self.size
        while lo < hi:
            mid = (lo + hi) // 2
            _, t = self._time_at(fh, mid)
            if t is None or t > until:
                hi = mid
            else:
                lo = mid + 1
        return self._time_at(fh, lo)[0]

    def last(self, limit: int, until: datetime | None = None, after: datetime | None = None) -> list[Candle]:
        """Up to `limit` bars with time <= until (and > after), oldest first."""
        with self.path.open("rb") as fh:
            end = self.size if until is None else self._offset_after(fh, until)
            chunk, bars = 128 * max(limit, 16), []
            while True:
                begin = max(self._data_start, end - chunk)
                fh.seek(begin)
                lines = fh.read(end - begin).split(b"\n")
                if begin > self._data_start:
                    lines = lines[1:]  # partial first line
                bars = [b for b in (self._parse(x) for x in lines if x.strip()) if b is not None]
                if len(bars) >= limit or begin == self._data_start:
                    break
                chunk *= 4
        if after is not None:
            bars = [b for b in bars if b.time > after]
        return bars[-limit:]

    def span(self) -> tuple[datetime | None, datetime | None]:
        with self.path.open("rb") as fh:
            _, first = self._time_at(fh, self._data_start)
        tail = self.last(1)
        return first, tail[-1].time if tail else None


# --------------------------------------------------------------------------- shadow store bars

def shadow_bars(db_path: Path, *, until: datetime | None = None, limit: int = 20000) -> list[Candle]:
    if not Path(db_path).exists():
        return []
    con = connect_ro(db_path)
    try:
        rows = con.execute("SELECT open_time, open, high, low, close, volume, source FROM bars "
                           "ORDER BY open_time DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    out = [Candle(parse_time(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                  None if r[5] is None else float(r[5]), r[6]) for r in reversed(rows)]
    return [b for b in out if until is None or b.time <= until]


# --------------------------------------------------------------------------- aggregation

def aggregate(bars: list[Candle], seconds: int, base_seconds: int) -> tuple[list[Candle], bool]:
    """Group real bars into `seconds` buckets (UTC-aligned). Returns (bars, last_bucket_partial)."""
    out: list[Candle] = []
    counts: list[int] = []
    for b in bars:
        start = floor_time(b.time, seconds)
        if out and out[-1].time == start:
            last = out[-1]
            vol = None if last.volume is None or b.volume is None else last.volume + b.volume
            out[-1] = Candle(start, last.open, max(last.high, b.high), min(last.low, b.low), b.close, vol,
                             "live" if "live" in (last.source, b.source) else last.source)
            counts[-1] += 1
        else:
            out.append(Candle(start, b.open, b.high, b.low, b.close, b.volume, b.source))
            counts.append(1)
    partial = bool(out) and counts[-1] < seconds // base_seconds and \
        out[-1].time + timedelta(seconds=seconds) > datetime.now(UTC)
    return out, partial


# --------------------------------------------------------------------------- resolver

def load_candles(market: dict, tf: str, *, limit: int = 500, until: datetime | None = None) -> dict:
    """Best recorded candles for a market/timeframe: CSV history, then shadow-store bars after the CSV ends.
    Returns candles plus provenance segments and limitations."""
    seconds = tf_seconds(tf)
    limit = max(10, min(int(limit), 5000))
    history = market.get("history", {})
    store_tf = market.get("store_interval")
    store_db = Path(market["state_dir"]) / "shadow.sqlite"
    notes: list[str] = []

    # 1) choose a native or finer CSV for this timeframe
    csv_tf = tf if tf in history else None
    if csv_tf is None:  # aggregate from the largest finer CSV
        finer = [t for t in history if TIMEFRAMES[t] < seconds and seconds % TIMEFRAMES[t] == 0]
        csv_tf = max(finer, key=lambda t: TIMEFRAMES[t]) if finer else None
    hist: list[Candle] = []
    hist_end = None
    if csv_tf:
        src = CsvBars(history[csv_tf])
        factor = seconds // TIMEFRAMES[csv_tf]
        hist = src.last(limit * factor + factor, until=until)
        hist_end = hist[-1].time if hist else None

    # 2) live / warm-up bars recorded by the runner after the CSV ends (store interval must divide tf)
    live: list[Candle] = []
    if store_tf and seconds >= TIMEFRAMES[store_tf] and seconds % TIMEFRAMES[store_tf] == 0:
        # only bars that start after the last history bar's period has ended (no double counting)
        cutoff = None if hist_end is None else hist_end + timedelta(seconds=TIMEFRAMES[csv_tf])
        live = [b for b in shadow_bars(store_db, until=until) if cutoff is None or b.time >= cutoff]

    segments = []
    if hist:
        segments.append({"kind": "recorded history (research CSV)", "file": Path(history[csv_tf]).name,
                         "base_timeframe": csv_tf, "from": hist[0].time.isoformat(), "to": hist[-1].time.isoformat(),
                         "aggregated": csv_tf != tf})
    if live:
        segments.append({"kind": "runner shadow store (observed by the running runner)", "file": "shadow.sqlite",
                         "base_timeframe": store_tf, "from": live[0].time.isoformat(), "to": live[-1].time.isoformat(),
                         "aggregated": store_tf != tf})

    candles: list[Candle] = []
    partial = False
    if hist:
        agg, _ = aggregate(hist, seconds, TIMEFRAMES[csv_tf]) if csv_tf != tf else (hist, False)
        candles += agg
    if live:
        agg, partial = aggregate(live, seconds, TIMEFRAMES[store_tf]) if store_tf != tf else (live, False)
        if candles and agg and agg[0].time == candles[-1].time:  # one bucket straddles the seam: combine
            h, s = candles[-1], agg[0]
            vol = None if h.volume is None or s.volume is None else h.volume + s.volume
            candles[-1] = Candle(h.time, h.open, max(h.high, s.high), min(h.low, s.low), s.close, vol, s.source)
            agg = agg[1:]
        candles += agg
    candles = candles[-limit:]

    if not candles:
        notes.append(f"No recorded {tf} data for this market. Nothing is displayed rather than invented.")
    else:
        last = candles[-1].time
        age = datetime.now(UTC) - last
        if age > timedelta(seconds=seconds * 3) and until is None:
            notes.append(f"Recorded {tf} data ends at {last.isoformat()} ({age.days} d "
                         f"{age.seconds // 3600} h ago). No newer {tf} bars were recorded for this market; "
                         f"live bars exist only at the runner's interval ({store_tf}).")
    if any(s["aggregated"] for s in segments):
        notes.append(f"{tf} candles are aggregated from recorded smaller bars (UTC-aligned buckets).")
    if partial:
        notes.append("The last candle is still forming (aggregated from the bars recorded so far).")
    return {"timeframe": tf, "candles": [c.to_json() for c in candles], "segments": segments, "notes": notes,
            "has_volume": any(c.volume is not None for c in candles), "volume_label": market.get("volume_label"),
            "last_candle_partial": partial}


def availability(market: dict) -> dict:
    """Which timeframes have recorded data, and over which span (cheap: two reads per CSV)."""
    out = {}
    history = market.get("history", {})
    spans = {tf: CsvBars(p).span() for tf, p in history.items() if Path(p).exists()}
    store_tf = market.get("store_interval")
    for tf, sec in TIMEFRAMES.items():
        sources = []
        for htf, (first, last) in spans.items():
            if TIMEFRAMES[htf] <= sec and sec % TIMEFRAMES[htf] == 0:
                sources.append({"kind": "history", "base": htf, "from": first and first.isoformat(),
                                "to": last and last.isoformat()})
        if store_tf and sec >= TIMEFRAMES[store_tf] and sec % TIMEFRAMES[store_tf] == 0:
            sources.append({"kind": "live shadow store", "base": store_tf})
        out[tf] = {"available": bool(sources), "sources": sources}
    return out

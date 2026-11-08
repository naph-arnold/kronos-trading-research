"""Read-only Binance public market-data streams for BTC/USDT shadow trading (Pipeline B).

* WebSocket: `wss://data-stream.binance.vision` (Binance's market-data-only stream host): best bid/ask
  (`<symbol>@bookTicker`). Receive-only; no subscription to user data, no credentials, nothing is ever sent
  except the protocol's own ping/pong frames.
* Reconnection: automatic with capped exponential back-off; Binance closes connections after 24 h and the
  runner falls back to REST `bookTicker` polling whenever the stream is down or silent.
* Timestamps: bookTicker carries no time. Each update is stamped with the local UTC clock corrected by the
  offset to Binance server time (`ClockSync`, measured over REST `/api/v3/time`), and forced to be strictly
  increasing.
* Volume control: updates are compressed per second (first, last, lowest/highest bid and ask, in time
  order), so stop and target crossings within a second are preserved while the engine processes a bounded
  number of quotes.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from trading.marketdata import Quote

STREAM_HOST = "wss://data-stream.binance.vision"


def stream_url(symbol: str = "BTCUSDT") -> str:
    if not symbol.isascii() or not symbol.isalnum():
        raise ValueError("Invalid symbol")
    return f"{STREAM_HOST}/stream?streams={symbol.lower()}@bookTicker"


@dataclass
class ClockSync:
    """Offset = Binance server time - local UTC time (measured at the midpoint of the request)."""

    server_time: Callable[[], datetime]
    local_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    offset: timedelta = timedelta(0)
    round_trip: timedelta | None = None
    measured_at: datetime | None = None
    max_round_trip: timedelta = timedelta(seconds=2)
    unreliable_samples: int = 0

    def measure(self, samples: int = 3) -> timedelta:
        """Keep the sample with the shortest round trip (least asymmetric-delay error). If every sample is slower
        than `max_round_trip`, the previous offset is kept and the measurement is counted as unreliable."""
        best = None
        for _ in range(samples):
            before = self.local_now()
            server = self.server_time()
            after = self.local_now()
            rtt = after - before
            if best is None or rtt < best[0]:
                best = (rtt, server - (before + rtt / 2), after)
        rtt, offset, at = best
        self.round_trip = rtt
        if rtt > self.max_round_trip:
            self.unreliable_samples += 1
            return self.offset
        self.offset, self.measured_at = offset, at
        return self.offset

    def now(self) -> datetime:
        return self.local_now() + self.offset


def compress(quotes: list[Quote]) -> list[Quote]:
    """Keep, per UTC second, the first and last quote and those carrying the lowest/highest bid and ask.
    Output stays in time order and never invents prices."""
    buckets: dict[datetime, list[Quote]] = {}
    for q in quotes:
        buckets.setdefault(q.time.replace(microsecond=0), []).append(q)
    out = []
    for second in sorted(buckets):
        group = buckets[second]
        keep = {id(group[0]): group[0], id(group[-1]): group[-1]}
        for pick in (min(group, key=lambda q: q.bid), max(group, key=lambda q: q.bid),
                     min(group, key=lambda q: q.ask), max(group, key=lambda q: q.ask)):
            keep[id(pick)] = pick
        out.extend(sorted(keep.values(), key=lambda q: q.time))
    return out


@dataclass
class BookTickerStream:
    """Background WebSocket reader. `drain()` returns compressed quotes received since the last call."""

    symbol: str = "BTCUSDT"
    clock: ClockSync | None = None
    connect: Callable | None = None  # injectable for tests; default websockets.sync.client.connect
    max_backoff: float = 60.0
    _buffer: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    connected: bool = False
    reconnects: int = 0
    messages: int = 0
    last_message_at: datetime | None = None
    last_error: str | None = None
    _last_stamp: datetime | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="binance-bookticker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _stamp(self) -> datetime:
        now = self.clock.now() if self.clock else datetime.now(timezone.utc)
        if self._last_stamp is not None and now <= self._last_stamp:
            now = self._last_stamp + timedelta(microseconds=1)
        self._last_stamp = now
        return now

    def handle(self, raw: str | bytes) -> Quote | None:
        """Parse one stream frame; malformed or foreign frames are ignored (counted as errors)."""
        try:
            message = json.loads(raw)
            data = message.get("data", message)
            if data.get("s") != self.symbol:
                return None
            quote = Quote.create(symbol=self.symbol, bid=Decimal(data["b"]), ask=Decimal(data["a"]), time=self._stamp())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.last_error = f"bad frame: {type(exc).__name__}"
            return None
        with self._lock:
            self._buffer.append(quote)
            self.messages += 1
            self.last_message_at = quote.time
        return quote

    def drain(self) -> list[Quote]:
        with self._lock:
            quotes, self._buffer = self._buffer, []
        return compress(quotes)

    def _run(self) -> None:
        connect = self.connect
        if connect is None:
            from websockets.sync.client import connect as ws_connect
            connect = ws_connect
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(stream_url(self.symbol), open_timeout=10, close_timeout=5) as ws:
                    self.connected, backoff = True, 1.0
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        self.handle(raw)
            except Exception as exc:  # network errors, server-initiated 24 h close, protocol errors
                self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    def status(self, now: datetime | None = None) -> dict:
        now = now or (self.clock.now() if self.clock else datetime.now(timezone.utc))
        age = None if self.last_message_at is None else (now - self.last_message_at).total_seconds()
        return {"connected": self.connected, "reconnects": self.reconnects, "messages": self.messages,
                "last_message_age_s": age, "last_error": self.last_error}

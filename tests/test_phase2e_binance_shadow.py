"""Phase 2E BTC/USDT shadow pipeline: public data integrity, simulated execution, risk, restart recovery,
duplicates, emergency stop, separation from XAUUSD, absence of order paths. All market data is SYNTHETIC."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from trading.breaker import CircuitBreaker
from trading.common import Market
from trading.config import load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.instruments import BinanceSpotSpec
from trading.marketdata import Bar, Quote
from trading.replay import HistoryView
from trading.risk import RiskLimits
from trading.sessions import BTCUSDT_ALWAYS_OPEN
from trading.shadow.engine import BookSpec, EngineSettings, ShadowEngine
from trading.shadow.store import ShadowStore
from trading.signals import TradeSignal

UTC = timezone.utc
BTC = Market.BTCUSDT_BINANCE_SPOT
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
H1 = timedelta(hours=1)
ROOT = Path(__file__).resolve().parents[1]


def spec(fee="0.001") -> BinanceSpotSpec:
    return BinanceSpotSpec(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", tick_size=D("0.01"),
                           step_size=D("0.00001"), min_qty=D("0.00001"), max_qty=D("9000"), min_notional=D("5"),
                           taker_fee_rate=D(fee))


def bar(i: int, price: float = 76000.0) -> Bar:
    p = D(str(price))
    return Bar.create(symbol="BTCUSDT", interval="1h", open_time=T0 + i * H1, open=p, high=p + 50, low=p - 50,
                      close=p, volume=100)


def q(at: datetime, bid: str, ask: str | None = None) -> Quote:
    return Quote.create(symbol="BTCUSDT", bid=D(bid), ask=D(ask) if ask else D(bid) + D("0.01"), time=at)


class Scripted:
    def __init__(self, key, plan):
        self.key, self.plan = key, plan

    def __call__(self, history):
        b = history[-1]
        if b.close_time not in self.plan:
            return None
        side, entry, stop, tp = self.plan[b.close_time]
        return TradeSignal.create(signal_id=f"{self.key}-{b.close_time:%Y%m%dT%H%M}-{side}", market=BTC.value,
                                  symbol="BTCUSDT", side=side, entry_price=D(entry), stop_price=D(stop),
                                  take_profit=D(tp) if tp else None, strategy_id=f"test-{self.key}",
                                  strategy_version="1", generated_at=b.close_time, bar_close_time=b.close_time)


def make_engine(tmp_path: Path, plan: dict, *, books=None, guard=None, unseen=T0 - timedelta(days=1)) -> ShadowEngine:
    books = books or [BookSpec("s.e10000.f0.001", "s", D("10000"), D("0.001"), 24)]
    health = MarketHealthMonitor(BTC, max_status_age=timedelta(days=60), max_quote_gap=timedelta(days=60),
                                 abnormal_spread_multiple=D("100"))
    health.record_binance_status("TRADING", T0 - timedelta(days=2))
    limits = RiskLimits(market=BTC, max_spread=D("100"), slippage_allowance=D("10"),
                        max_entry_deviation_fraction=D("0.005"))
    return ShadowEngine(
        store=ShadowStore(tmp_path / "shadow.sqlite"), books=books,
        strategy_factory=lambda key: Scripted(key, plan.get(key, {})),
        instruments={D("0.001"): spec("0.001"), D("0.002"): spec("0.002")}, spec_retrieved_at=T0 - H1,
        limits=limits, environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
        breaker=CircuitBreaker(BTC, kill_file=tmp_path / "KILL", state_path=tmp_path / "breaker.json"),
        daily=DailyLossLedger(tmp_path / "daily.sqlite", market=BTC, boundary=DayBoundary.utc(),
                              halt_fraction=D("0.015")),
        calendar=BTCUSDT_ALWAYS_OPEN, event_guard=guard or (lambda now: None), swap=None,
        settings=EngineSettings(unseen_start=unseen, market=BTC, interval="1h", fallback_half_spread=D("5")),
        health=health)


C0 = T0 + H1
LONG = {"s": {C0: ("long", "76000.00", "75500.00", "77000.00")}}


def _open(engine):
    engine.process([bar(0)], [], C0 + timedelta(seconds=2))
    engine.process([], [q(C0 + timedelta(milliseconds=300), "76000.00")], C0 + timedelta(seconds=3))


# --------------------------------------------------------------------------- simulated execution, risk, fees

def test_entry_sizing_fee_and_take_profit(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open(engine)
    p = engine.positions["s.e10000.f0.001"]
    assert p.entry == D("76010.01")  # ask + 10 USDT slippage
    # 0.5% of 10,000 = 50 USDT budget; loss/BTC = (76010.01 - 75490) + fee 0.1% x (76010.01 + 75490)
    loss = (D("76010.01") - D("75490.00")) + D("0.001") * (D("76010.01") + D("75490.00"))
    assert p.size == (D("50") / loss).quantize(D("0.00001"), rounding="ROUND_FLOOR") and p.planned_risk <= D("50")
    engine.process([], [q(C0 + timedelta(minutes=20), "77001.00")], C0 + timedelta(minutes=20, seconds=1))
    (trade,) = engine.store.trades()
    fee = p.size * (D("76010.01") + D("77000.00")) * D("0.001")
    assert trade["exit_reason"] == "take_profit" and D(trade["commission"]) == fee
    assert D(trade["pnl"]) == p.size * (D("77000.00") - D("76010.01")) - fee and D(trade["swap"]) == 0


def test_stop_fill_uses_bid_minus_slippage(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open(engine)
    engine.process([], [q(C0 + timedelta(minutes=5), "75499.00")], C0 + timedelta(minutes=5, seconds=1))
    trade = engine.store.trades()[0]
    assert trade["exit_reason"] == "stop" and D(trade["exit"]) == D("75489.00")


def test_spot_is_long_only_and_random_controls_refuse_shorts(tmp_path):
    from trading.strategies.baselines import RandomEntryParams
    from trading.strategies.crypto import BtcRandomEntry
    engine = make_engine(tmp_path, {"s": {C0: ("short", "76000.00", "76500.00", None)}})
    _open(engine)
    assert "Binance spot is long/flat only" in engine.store.decisions()[0]["reasons"]
    with pytest.raises(ValueError):
        BtcRandomEntry(RandomEntryParams(long_probability=0.5), tick_size=D("0.01"))


def test_fee_scenarios_are_separate_books(tmp_path):
    books = [BookSpec("s.f1", "s", D("10000"), D("0.001"), 24), BookSpec("s.f2", "s", D("10000"), D("0.002"), 24)]
    engine = make_engine(tmp_path, LONG, books=books)
    _open(engine)
    assert engine.positions["s.f2"].size < engine.positions["s.f1"].size  # higher fee -> smaller size, same budget
    assert all(p.planned_risk <= D("50") for p in engine.positions.values())


def test_time_exit_after_max_holding_bars(tmp_path):
    engine = make_engine(tmp_path, LONG, books=[BookSpec("s.e", "s", D("10000"), D("0.001"), 2)])
    _open(engine)
    for i in (1, 2):
        engine.process([], [q(C0 + (i - 1) * H1 + timedelta(minutes=30), "76100.00")],
                       C0 + (i - 1) * H1 + timedelta(minutes=30, seconds=1))
        engine.process([bar(i)], [], bar(i).close_time + timedelta(seconds=2))
    assert engine.store.trades()[0]["exit_reason"] == "time_exit"


def test_engine_rejects_foreign_market_components(tmp_path):
    from trading.clock import EET_EU
    xau_limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=D("1"), slippage_allowance=D("0.1"))
    with pytest.raises(ValueError):
        ShadowEngine(store=ShadowStore(tmp_path / "x.sqlite"), books=[BookSpec("s", "s", D("10000"), D("0.001"))],
                     strategy_factory=lambda k: Scripted(k, {}), instruments={D("0.001"): spec()},
                     spec_retrieved_at=T0, limits=xau_limits, environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
                     breaker=CircuitBreaker(BTC), daily=DailyLossLedger(":memory:", market=BTC,
                                                                        boundary=DayBoundary.server(EET_EU),
                                                                        halt_fraction=D("0.015")),
                     calendar=BTCUSDT_ALWAYS_OPEN, event_guard=None, swap=None,
                     settings=EngineSettings(unseen_start=T0, market=BTC, interval="1h"),
                     health=MarketHealthMonitor(BTC))


# --------------------------------------------------------------------------- integrity, restart, duplicates, emergency

def test_late_processing_and_stale_guard_reject_entries(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.process([bar(0)], [], C0 + timedelta(seconds=2))
    engine.process([], [q(C0 + timedelta(seconds=1), "76000.00")], C0 + timedelta(minutes=3))
    assert any(r.startswith("System not live") for r in engine.store.decisions()[0]["reasons"])
    engine = make_engine(tmp_path / "b", LONG, guard=lambda now: "closed klines stale (last close None)")
    _open(engine)
    assert any("klines stale" in r for r in engine.store.decisions()[0]["reasons"])


def test_restart_reprocessing_and_duplicates(tmp_path):
    plan = {"s": {C0: ("long", "76000.00", "75500.00", "77000.00"), C0 + 2 * H1: ("long", "76050.00", "75600.00", None)}}
    steps = [([bar(0)], [], C0 + timedelta(seconds=2)),
             ([], [q(C0 + timedelta(seconds=1), "76000.00")], C0 + timedelta(seconds=3)),
             ([bar(1)], [q(C0 + timedelta(minutes=30), "76100.00")], C0 + H1 + timedelta(seconds=2)),
             ([bar(2, 76050.0)], [q(C0 + H1 + timedelta(minutes=10), "76050.00")], C0 + 2 * H1 + timedelta(seconds=2)),
             ([], [q(C0 + 2 * H1 + timedelta(seconds=1), "77001.00")], C0 + 2 * H1 + timedelta(seconds=3))]
    one = make_engine(tmp_path / "one", plan)
    for s in steps:
        one.process(*s)
    two = make_engine(tmp_path / "two", plan)
    for s in steps[:2]:
        two.process(*s)
    two.store.close()
    again = make_engine(tmp_path / "two", plan)
    assert again.positions and again.reconcile()["ok"]
    for s in steps[2:] + steps:  # remaining steps, then everything again (duplicates must be ignored)
        again.process(*s)
    snap = lambda e: (e.store.trades(), e.store.decisions(), e.store.positions(), dict(e.realized))  # noqa: E731
    assert snap(again) == snap(one)


def test_kill_file_blocks_entries_and_keeps_stops(tmp_path):
    plan = {"s": {C0: ("long", "76000.00", "75500.00", None), C0 + H1: ("long", "76000.00", "75500.00", None)}}
    engine = make_engine(tmp_path, plan, books=[BookSpec("s.e", "s", D("10000"), D("0.001"), None)])
    _open(engine)
    (tmp_path / "KILL").write_text("x", encoding="utf-8")
    engine.process([], [q(C0 + timedelta(minutes=9), "75400.00")], C0 + timedelta(minutes=9, seconds=1))
    assert engine.store.trades()[0]["exit_reason"].startswith("stop")
    engine.process([bar(1)], [], C0 + H1 + timedelta(seconds=2))
    engine.process([], [q(C0 + H1 + timedelta(seconds=1), "76000.00")], C0 + H1 + timedelta(seconds=2))
    assert any("Kill file present" in r for r in engine.store.decisions()[-1]["reasons"])


def test_daily_loss_halt_applies_to_crypto_books(tmp_path):
    plan = {"s": {C0: ("long", "76000.00", "75500.00", None), C0 + H1: ("long", "70000.00", "69500.00", None)}}
    engine = make_engine(tmp_path, plan, books=[BookSpec("s.e", "s", D("10000"), D("0.001"), None)])
    _open(engine)
    engine.process([bar(1, 70000.0)], [q(C0 + timedelta(minutes=10), "70000.00")], C0 + H1 + timedelta(seconds=2))
    assert -D(engine.store.trades()[0]["pnl"]) / D("10000") >= D("0.015")  # gap far through the stop
    engine.process([], [q(C0 + H1 + timedelta(seconds=1), "70000.00")], C0 + H1 + timedelta(seconds=2))
    assert any(r.startswith("Daily loss halt") for r in engine.store.decisions()[-1]["reasons"])


def test_xauusd_and_btc_state_breakers_are_independent(tmp_path):
    from trading.clock import EET_EU
    btc = CircuitBreaker(BTC, kill_file=tmp_path / "btc" / "KILL", state_path=tmp_path / "btc" / "breaker.json")
    xau = CircuitBreaker(Market.XAUUSD_MT5, kill_file=tmp_path / "xau" / "KILL",
                         state_path=tmp_path / "xau" / "breaker.json")
    btc.trip("btc test trip")
    (tmp_path / "btc" / "KILL").write_text("x", encoding="utf-8")
    assert btc.is_tripped and not xau.is_tripped
    # A breaker state file of one market is never accepted by the other's breaker (fail closed, not shared).
    foreign = CircuitBreaker(Market.XAUUSD_MT5, state_path=tmp_path / "btc" / "breaker.json").reasons()
    assert foreign and "unreadable" in foreign[0]
    xau_daily = DailyLossLedger(tmp_path / "xau" / "daily.sqlite", market=Market.XAUUSD_MT5,
                                boundary=DayBoundary.server(EET_EU), halt_fraction=D("0.015"))
    assert xau_daily.market is not BTC


# --------------------------------------------------------------------------- strategies

def _signals(strategy, bars):
    return [s for i in range(len(bars)) if (s := strategy(HistoryView(bars, i + 1))) is not None]


def _walk(n=1500, seed=4):
    from phase2_helpers import btc_h1
    return btc_h1(n, seed=seed)


@pytest.mark.parametrize("make", [
    lambda: __import__("trading.strategies.crypto", fromlist=["x"]).DonchianBreakout(
        __import__("trading.strategies.crypto", fromlist=["x"]).DonchianParams(entry_lookback=24), tick_size=D("0.01")),
    lambda: __import__("trading.strategies.crypto", fromlist=["x"]).VwapMeanReversion(
        __import__("trading.strategies.crypto", fromlist=["x"]).VwapReversionParams(entry_atr=1.0), tick_size=D("0.01")),
])
def test_new_strategies_are_causal_long_only_and_reproducible(make):
    bars = _walk()
    full = _signals(make(), bars)
    prefix = _signals(make(), bars[:900])
    cutoff = bars[899].close_time
    assert full and {s.side.value for s in full} == {"long"}
    assert [s.signal_id for s in prefix] == [s.signal_id for s in full if s.bar_close_time <= cutoff]
    assert [s.signal_id for s in _signals(make(), bars)] == [s.signal_id for s in full]
    assert all(s.stop_price < s.entry_price for s in full)


def test_vwap_target_is_the_vwap_and_donchian_excludes_current_bar():
    from trading.strategies.crypto import DonchianBreakout, DonchianParams, VwapMeanReversion, VwapReversionParams
    bars = _walk()
    for s in _signals(VwapMeanReversion(VwapReversionParams(entry_atr=1.0), tick_size=D("0.01")), bars):
        assert s.take_profit > s.entry_price
        assert abs(float(s.take_profit) - s.provenance["context"]["vwap"]) < 0.02
    for s in _signals(DonchianBreakout(DonchianParams(entry_lookback=24), tick_size=D("0.01")), bars):
        assert s.entry_price > D(str(round(s.provenance["context"]["channel_high"], 2))) - D("0.01")


# --------------------------------------------------------------------------- public market data

class FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if "/api/v3/time" in url:
            return 200, json.dumps({"serverTime": int((T0 + 5 * H1).timestamp() * 1000)}).encode()
        if "/api/v3/klines" in url:
            return 200, json.dumps(self.pages.pop(0) if self.pages else []).encode()
        if "/api/v3/ticker/bookTicker" in url:
            return 200, json.dumps({"bidPrice": "76000.00", "askPrice": "76000.01"}).encode()
        return 404, b"{}"


def _kline(i, close="76000"):
    t = int((T0 + i * H1).timestamp() * 1000)
    return [t, "76000", "76100", "75900", close, "10", t + 3_599_999, "760000"]


def test_closed_klines_drop_forming_candle_and_detect_gaps():
    from trading.connectors.binance import BinanceReadOnlyClient
    http = FakeHttp([[_kline(0), _kline(1), _kline(3), _kline(4), _kline(5)]])
    client = BinanceReadOnlyClient.public(http_get=http)
    bars = client.closed_klines(T0, T0 + 6 * H1, now=T0 + 5 * H1 + timedelta(minutes=10))
    assert [b.open_time for b in bars] == [T0, T0 + H1, T0 + 3 * H1, T0 + 4 * H1]  # bar 5 still forming
    assert all("X-MBX-APIKEY" not in h for _, h in http.calls)  # public: no credentials
    with pytest.raises(PermissionError):
        client._request("/api/v3/order", {})


def test_websocket_frames_timestamps_and_compression():
    from trading.connectors.binance_stream import BookTickerStream, ClockSync, compress
    times = iter([T0 + timedelta(milliseconds=i) for i in (0, 0, 0, 5, 900, 1500)])
    clock = ClockSync(server_time=lambda: T0, local_now=lambda: next(times))
    stream = BookTickerStream(clock=clock)
    frame = lambda b, a, s="BTCUSDT": json.dumps({"stream": "btcusdt@bookTicker", "data": {"s": s, "b": b, "a": a}})  # noqa
    assert stream.handle(frame("76000.00", "76000.01")).time == T0
    assert stream.handle(frame("76000.00", "76000.01")).time > T0  # strictly increasing stamps
    assert stream.handle(frame("1", "2", s="ETHUSDT")) is None and stream.handle("not json") is None
    stream.handle(frame("75990.00", "75990.01"))
    stream.handle(frame("76005.00", "76005.01"))
    quotes = stream.drain()
    assert min(x.bid for x in quotes) == D("75990.00") and [x.time for x in quotes] == sorted(x.time for x in quotes)
    many = [Quote.create(symbol="BTCUSDT", bid=D(76000 + (i % 7)), ask=D(76001 + (i % 7)),
                         time=T0 + timedelta(milliseconds=10 * i)) for i in range(100)]
    kept = compress(many)
    assert len(kept) <= 6 and min(x.bid for x in kept) == min(x.bid for x in many)
    assert max(x.bid for x in kept) == max(x.bid for x in many)


def test_clock_sync_uses_fastest_sample_and_ignores_slow_ones():
    from trading.connectors.binance_stream import ClockSync
    ms = lambda x: timedelta(milliseconds=x)  # noqa: E731
    # three samples: round trips 8 s (asymmetric), 200 ms, 900 ms; true offset +2 s
    local = iter([T0, T0 + ms(8000), T0 + ms(9000), T0 + ms(9200), T0 + ms(10000), T0 + ms(10900)])
    server = iter([T0 + ms(2000 + 7900), T0 + ms(2000 + 9100), T0 + ms(2000 + 10450)])
    sync = ClockSync(server_time=lambda: next(server), local_now=lambda: next(local))
    assert sync.measure() == timedelta(seconds=2) and sync.round_trip == ms(200)
    slow = iter([T0, T0 + ms(5000)] * 3)
    sync.local_now, sync.server_time = (lambda: next(slow)), (lambda: T0 + timedelta(seconds=30))
    assert sync.measure() == timedelta(seconds=2) and sync.unreliable_samples == 1  # previous offset kept


def test_websocket_reconnects_after_failures():
    import threading
    from trading.connectors.binance_stream import BookTickerStream
    attempts, got = [], threading.Event()

    class FakeWs:
        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def recv(self, timeout=None):
            if self.sent:
                got.set()
                raise ConnectionError("server closed")
            self.sent = True
            return json.dumps({"data": {"s": "BTCUSDT", "b": "76000.00", "a": "76000.01"}})

    def connect(url, **kw):
        assert url.startswith("wss://data-stream.binance.vision/")
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("network down")
        return FakeWs()

    stream = BookTickerStream(connect=connect, max_backoff=0.01)
    stream.start()
    assert got.wait(10)
    stream.stop()
    assert stream.reconnects >= 1 and stream.messages >= 1 and len(attempts) >= 2


def test_feed_falls_back_to_rest_when_websocket_silent():
    from research.phase2e_binance_shadow import BinanceFeed
    from trading.connectors.binance import BinanceReadOnlyClient
    from trading.connectors.binance_stream import BookTickerStream, ClockSync
    client = BinanceReadOnlyClient.public(http_get=FakeHttp([]))
    clock = ClockSync(server_time=client.server_time)
    feed = BinanceFeed(client=client, stream=BookTickerStream(clock=clock), clock=clock)
    quotes, source = feed.quotes()
    assert source == "rest_fallback" and quotes[-1].bid == D("76000.00") and feed.rest_fallbacks == 1


# --------------------------------------------------------------------------- no order paths; testnet preparation

def test_no_order_submission_code_in_btc_pipeline():
    sources = [ROOT / "research/phase2e_binance_shadow.py", ROOT / "research/phase2e_btcusdt_research.py",
               ROOT / "research/phase2e_btc_history.py", ROOT / "trading/connectors/binance_stream.py",
               ROOT / "trading/connectors/binance.py", *(ROOT / "trading/execution_prep").glob("*.py"),
               *(ROOT / "trading/shadow").glob("*.py")]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("/api/v3/order", "order_send", 'method="POST"', "method='POST'", "urlopen(", "api.x.ai",
                          "BINANCE_API_KEY"):
            if forbidden == "urlopen(" and path.name == "binance.py":
                continue  # read-only GET helper (method fixed to GET)
            assert forbidden not in text, f"{forbidden} in {path.name}"
    from trading.connectors.binance import ALLOWED_PATHS
    assert not any("order" in p for p in ALLOWED_PATHS)


def _approved(signal, size):
    from trading.risk import RiskDecision
    return RiskDecision(approved=True, market=BTC.value, signal_id=signal.signal_id, dedup_key=signal.dedup_key,
                        size=D(size), reasons=(), evaluated_at=T0.isoformat())


def test_testnet_preparation_validates_filters_and_never_submits():
    from trading.execution_prep.binance_testnet import (ExecutionNotAuthorized, build_entry_intent, detect_reset,
                                                         reconcile, submit)
    signal = Scripted("s", LONG["s"])(HistoryView([bar(0)], 1))
    intent = build_entry_intent(signal, _approved(signal, "0.00065"), spec())
    assert intent.new_client_order_id.startswith("krn-") and len(intent.new_client_order_id) <= 36
    assert intent.new_client_order_id == build_entry_intent(signal, _approved(signal, "0.00065"), spec()).new_client_order_id
    with pytest.raises(Exception):
        build_entry_intent(signal, _approved(signal, "0.000655"), spec())  # off the LOT_SIZE step
    with pytest.raises(Exception):
        build_entry_intent(signal, _approved(signal, "0.00005"), spec())  # below NOTIONAL (3.8 USDT)
    with pytest.raises(ExecutionNotAuthorized):
        submit(intent)
    before = {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}], "updateTime": 5}
    after = {"balances": [{"asset": "BTC", "free": "1.00065", "locked": "0"}], "updateTime": 6}
    assert detect_reset(before, after, expected_change={"BTC": D("0.00065")}) is None
    assert "testnet reset" in detect_reset(after, before)
    assert reconcile(D("1.00065"), after)["reconciled"] and not reconcile(D("1"), after)["reconciled"]


# --------------------------------------------------------------------------- runner end-to-end (fake public feed)

class StubKronos:
    def predict(self, df, x_timestamp, y_timestamp, **kw):
        import pandas as pd
        return pd.DataFrame({"close": [float(df["close"].iloc[-1]) * 1.01]})  # +100 bps: above any edge threshold


class FakeFeed:
    """SYNTHETIC public feed. No credentials, no order methods."""

    def __init__(self, bars, now):
        from trading.connectors.binance_stream import BookTickerStream, ClockSync
        self.bars, self._now = bars, now
        self.clock = ClockSync(server_time=lambda: self._now[0], local_now=lambda: self._now[0])
        self.stream = BookTickerStream(clock=self.clock)
        self.fail_quotes = False
        self.cursor = None

    def start(self):
        self.clock.measure()

    def stop(self):
        pass

    def now(self):
        return self._now[0]

    def exchange_info(self):
        return json.loads((ROOT / "tests/data/binance_exchangeinfo_btcusdt_20261106.json").read_text())["symbol"]

    def closed_bars(self, start, end):
        return [b for b in self.bars if b.open_time >= start and b.close_time <= end]

    def quotes(self, stale_after=10.0):
        if self.fail_quotes:
            return [], "unavailable: URLError"
        now = self._now[0]
        start = self.cursor or now - timedelta(seconds=5)
        self.cursor = now
        out, t = [], start + timedelta(seconds=1)
        while t <= now:
            ref = next((b for b in reversed(self.bars) if b.open_time <= t), self.bars[0])
            out.append(Quote.create(symbol="BTCUSDT", bid=ref.close, ask=ref.close + D("0.01"), time=t))
            t += timedelta(seconds=1)
        return out, "websocket"


def _btc_config(tmp_path):
    from research.phase2e_binance_shadow import normalized_sha256
    from trading.strategies.base import params_hash
    from trading.strategies.crypto import DonchianParams, KronosParams
    candidates = {"donchian": {"params": {"entry_lookback": 6, "atr_period": 14, "atr_stop_mult": 2.0,
                                          "reward_to_risk": 2.0}, "max_holding_bars": 12},
                  "kronos": {"params": {"min_edge_bps": 20.0, "trend_ema": None, "atr_period": 14,
                                        "atr_stop_mult": 1.5, "reward_to_risk": None}, "max_holding_bars": 1}}
    controls = {"donchian": {"params": {"entry_probability": 0.05, "atr_period": 14, "atr_stop_mult": 2.0,
                                        "reward_to_risk": 2.0, "long_probability": 1.0}, "seeds": [1, 2],
                             "max_holding_bars": 12}}
    prereg = {"unseen_window_starts": (T0 + 30 * H1).isoformat(), "rules": {"risk_fraction": "0.005"},
              "candidates": [{"key": "donchian", "version": "1.0.0", "params": candidates["donchian"]["params"],
                              "params_hash": params_hash(DonchianParams(**candidates["donchian"]["params"])),
                              "max_holding_bars": 12},
                             {"key": "kronos", "version": "1.0.0", "params": candidates["kronos"]["params"],
                              "params_hash": params_hash(KronosParams(**candidates["kronos"]["params"])),
                              "max_holding_bars": 1}],
              "controls": controls,
              "evaluation": {"success_criteria_all_required": ["c1", "c2", "c3", "c4", "c5"], "fee_scenario": "0.001",
                             "final_checkpoint_min_trades": 100,
                             "final_checkpoint_not_before": "2027-05-09T00:00:00+00:00"}}
    path = tmp_path / "prereg.json"
    path.write_text(json.dumps(prereg), encoding="utf-8")
    config = {"preregistration": str(path), "preregistration_sha256": normalized_sha256(path),
              "candidates": candidates, "random_controls": controls, "equity": "10000", "tick_size": "0.01",
              "fee_scenarios": ["0.001", "0.002"], "warmup_start": T0.isoformat(), "fallback_half_spread_usdt": "5",
              "risk": {"risk_fraction": "0.005", "daily_loss_halt_fraction": "0.015", "aggregate_risk_fraction": "0.01",
                       "max_spread_usdt": "100", "slippage_allowance_usdt": "10",
                       "max_entry_deviation_fraction": "0.005"},
              "health": {"max_quote_age_s": 15, "max_quote_gap_s": 60, "abnormal_spread_multiple": "100",
                         "max_status_age_min": 30},
              "calendar": {"official_providers": False, "refresh_hours": 12},
              "runner": {"poll_seconds": 2, "max_decision_latency_s": 30, "spec_refresh_hours": 12, "kline_settle_s": 3,
                         "max_kline_lag_s": 300, "max_clock_offset_s": 2, "ws_stale_s": 10, "max_buffer": 50000,
                         "heartbeat_seconds": 60, "exchange_status_minutes": 5, "clock_sync_minutes": 10}}
    return config, prereg


def _calendar_service(tmp_path):
    from trading.marketcal.events import EconomicEvent, EventStore
    from trading.marketcal.service import CalendarProvider, CalendarService

    def ev(provider, series):
        return EconomicEvent(event_id=f"{series.lower()}-1", name=series, country="US", currency="USD",
                             scheduled_at=datetime(2026, 12, 9, 19, tzinfo=UTC), impact="high", provider=provider,
                             retrieved_at=T0, updated_at=T0, series=series)
    providers = [CalendarProvider("fake-fomc", lambda: [ev("fake-fomc", "FOMC_DECISION")],
                                  frozenset({"FOMC_DECISION"}), timedelta(days=14)),
                 CalendarProvider("fake-bls", lambda: [ev("fake-bls", "US_CPI")], frozenset({"US_CPI", "US_NFP"}),
                                  timedelta(days=31))]
    return CalendarService(EventStore(tmp_path / "cal.sqlite"), providers)


def test_runner_end_to_end_with_restart_heartbeats_and_kline_gap(tmp_path, monkeypatch):
    from phase2_helpers import random_walk
    from research.phase2e_binance_shadow import LiveKronos, Runner, check_preregistration, report
    monkeypatch.setenv("EXECUTION_MODE", "demo")
    config, prereg = _btc_config(tmp_path)
    check_preregistration(config, prereg)
    bars = random_walk("BTCUSDT", "1h", T0, 80, price=76000.0, vol=0.004, seed=9)
    bars = [b for b in bars if b.open_time != T0 + 50 * H1]  # exchange outage: one missing candle
    now = [T0 + 40 * H1 + timedelta(seconds=10)]
    feed = FakeFeed(bars, now)
    state = tmp_path / "state"
    runner = Runner(config, prereg, feed, state, calendar_service=_calendar_service(tmp_path),
                    kronos=LiveKronos(state / "kronos.jsonl", lookback=16, predictor=StubKronos()))
    runner.start()
    assert runner.store.bar_count("warmup_history") == 29 and runner.store.bar_count("live") == 11
    assert len(runner.engine.books) == (2 + 2) * 2  # 2 candidates + 2 random seeds, x 2 fee scenarios
    for step in range(30 * 6):  # 30 hours of polls every 10 minutes; quotes stream continuously
        now[0] += timedelta(minutes=10)
        feed.fail_quotes = 40 <= step < 44
        runner.poll()
        runner.housekeeping()
    kinds = {e["kind"] for e in runner.store.events()}
    assert {"kline_gap", "quote_source", "reconciliation", "calendar_refresh"} <= kinds
    beats = runner.store.heartbeats()
    assert len(beats) >= 150 and any(b[1]["status"] == "entries_blocked" for b in beats)
    assert runner.kronos.forecasts and (state / "kronos.jsonl").exists()
    assert runner.store.decisions() and runner.engine.reconcile()["ok"]
    assert all(datetime.fromisoformat(s["bar_close"]) >= datetime.fromisoformat(prereg["unseen_window_starts"])
               for s in runner.store.signals() if s["status"] != "warmup")
    rep = report(config, prereg, runner.store, now[0])
    assert set(rep["preregistered_criteria"]) == {"donchian", "kronos"} and rep["orders_placed"] == 0
    runner.store.close()
    again = Runner(config, prereg, FakeFeed(bars, now), state, calendar_service=_calendar_service(tmp_path / "c"),
                   kronos=LiveKronos(state / "kronos.jsonl", lookback=16, predictor=StubKronos()))
    restored = again.start()["restored"]
    assert restored["signals_mismatch"] == 0 and restored["reconciliation"]["ok"] and not again.breaker.reasons()


def test_committed_btc_config_matches_preregistration():
    from research.phase2e_binance_shadow import build_books, load_config
    config, prereg = load_config(ROOT / "research/configs/phase2e_binance_shadow.json")
    keys = {b.strategy_key for b in build_books(config)}
    assert keys == {"donchian", "ema_trend", "kronos", *(f"random_{k}_s{s}" for k in ("donchian", "kronos")
                                                         for s in (1, 2, 3))}
    assert config["risk"]["risk_fraction"] == "0.005" and config["fee_scenarios"] == ["0.001", "0.002"]
    assert prereg["unseen_window_starts"] > prereg["registered_at"]
    assert "vwap_reversion" in prereg["not_forward_tested"]
    assert (ROOT / config["calendar"]["cpi_nfp_file"]).exists()
    assert config["state_dir"] != "state/phase2d_shadow"  # never shares the XAUUSD state


def test_btc_config_drift_is_refused(tmp_path):
    from research.phase2e_binance_shadow import check_preregistration
    from trading.common import ValidationError
    config, prereg = _btc_config(tmp_path)
    drift = json.loads(json.dumps(config))
    drift["candidates"]["donchian"]["params"]["entry_lookback"] = 7
    with pytest.raises(ValidationError, match="frozen"):
        check_preregistration(drift, prereg)
    drift = json.loads(json.dumps(config))
    drift["random_controls"]["donchian"]["seeds"] = [1, 2, 3]
    with pytest.raises(ValidationError):
        check_preregistration(drift, prereg)

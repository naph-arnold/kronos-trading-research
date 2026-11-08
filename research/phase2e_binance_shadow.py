"""Phase 2E — BTC/USDT continuous shadow trading (public market data only; simulation only; no orders).

Subcommands:
  run            Live loop (24/7): public WebSocket best bid/ask (REST fallback) + closed 1h klines (REST, gap
                 detection) -> frozen candidates (Donchian, EMA trend, VWAP reversion, Kronos) and matched
                 random controls -> unchanged risk engine -> simulated fills, fees and slippage. State persists in
                 the state directory (restart-safe). Completely separate from the XAUUSD runner.
  report         Metrics, comparison, rejections, health, pre-registered criteria per candidate.
  status         Store / breaker summary.
  reset-breaker  Manual breaker reset (records the operator).

No credentials are read or sent. Emergency stop: <state>/KILL (new simulated entries rejected; open simulated
positions keep their stops). Graceful exit: <state>/STOP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from trading.audit import to_jsonable
from trading.breaker import CircuitBreaker
from trading.common import Market, ValidationError
from trading.config import load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.marketcal.events import EventStore
from trading.marketcal.service import CalendarService, official_providers, structured_file_provider
from trading.marketdata import Bar, Quote
from trading.risk import RiskLimits
from trading.sessions import BTCUSDT_ALWAYS_OPEN
from trading.shadow.engine import BookSpec, EngineSettings, ShadowEngine
from trading.shadow.report import build_report, candidate_criteria
from trading.shadow.store import ShadowStore
from trading.strategies.base import params_hash
from trading.strategies.baselines import RandomEntryParams
from trading.strategies.crypto import (BtcRandomEntry, DonchianBreakout, DonchianParams, KronosForecastStrategy,
                                       KronosParams, TrendFollowing, TrendParams, VwapMeanReversion,
                                       VwapReversionParams)

MARKET = Market.BTCUSDT_BINANCE_SPOT
H1 = timedelta(hours=1)
log = logging.getLogger("research.phase2e_binance_shadow")
CANDIDATE_CLASSES = {"donchian": (DonchianBreakout, DonchianParams), "ema_trend": (TrendFollowing, TrendParams),
                     "vwap_reversion": (VwapMeanReversion, VwapReversionParams),
                     "kronos": (KronosForecastStrategy, KronosParams)}


# --------------------------------------------------------------------------- configuration / pre-registration

def normalized_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_config(path: Path) -> tuple[dict, dict]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    prereg_path = Path(config["preregistration"])
    if normalized_sha256(prereg_path) != config["preregistration_sha256"]:
        raise ValidationError("Pre-registration file changed since the shadow test was configured (frozen)")
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    check_preregistration(config, prereg)
    return config, prereg


def check_preregistration(config: dict, prereg: dict) -> None:
    registered = {c["key"]: c for c in prereg["candidates"]}
    if set(registered) != set(config["candidates"]):
        raise ValidationError("Candidate set differs from the pre-registration")
    for key, spec in config["candidates"].items():
        reg = registered[key]
        cls, params_cls = CANDIDATE_CLASSES[key]
        if spec["params"] != reg["params"] or params_hash(params_cls(**reg["params"])) != reg["params_hash"] \
                or spec["max_holding_bars"] != reg["max_holding_bars"] or cls.version != reg["version"]:
            raise ValidationError(f"{key} parameters differ from the pre-registration (frozen)")
    for key, control in config["random_controls"].items():
        reg = prereg["controls"][key]
        if control != reg:
            raise ValidationError(f"Random control for {key} differs from the pre-registration")
    if config["risk"]["risk_fraction"] != prereg["rules"]["risk_fraction"]:
        raise ValidationError("Risk fraction differs from the pre-registration")


def strategy_table(config: dict) -> dict[str, tuple[str, dict, int | None]]:
    table = {}
    for key, spec in config["candidates"].items():
        table[key] = (key, spec["params"], spec["max_holding_bars"])
    for key, control in config["random_controls"].items():
        for seed in control["seeds"]:
            params = {**control["params"], "seed": seed}
            table[f"random_{key}_s{seed}"] = ("random", params, control["max_holding_bars"])
    return table


def strategy_factory(config: dict, forecasts: dict) -> Callable[[str], Callable]:
    table, tick = strategy_table(config), Decimal(config["tick_size"])

    def make(key: str):
        kind, params, _ = table[key]
        if kind == "random":
            return BtcRandomEntry(RandomEntryParams(**params), tick_size=tick, dataset_id="binance-live-shadow")
        cls, params_cls = CANDIDATE_CLASSES[kind]
        if kind == "kronos":
            return cls(params_cls(**params), forecasts=forecasts, tick_size=tick, dataset_id="binance-live-shadow")
        return cls(params_cls(**params), tick_size=tick, dataset_id="binance-live-shadow")
    return make


def build_books(config: dict) -> list[BookSpec]:
    return [BookSpec(f"{key}.e{config['equity']}.f{fee}", key, Decimal(config["equity"]), Decimal(fee), hold)
            for key, (_, _, hold) in strategy_table(config).items() for fee in config["fee_scenarios"]]


def limits_from(config: dict) -> RiskLimits:
    r = config["risk"]
    limits = RiskLimits(market=MARKET, max_spread=Decimal(r["max_spread_usdt"]),
                        slippage_allowance=Decimal(r["slippage_allowance_usdt"]),
                        risk_fraction=Decimal(r["risk_fraction"]),
                        daily_loss_halt_fraction=Decimal(r["daily_loss_halt_fraction"]),
                        aggregate_risk_fraction=Decimal(r["aggregate_risk_fraction"]),
                        max_entry_deviation_fraction=Decimal(r["max_entry_deviation_fraction"]))
    limits.validate()
    return limits


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- live feed (public, read-only)

class BinanceFeed:
    """Public market data only: REST (exchangeInfo, time, klines, bookTicker) + WebSocket bookTicker."""

    def __init__(self, client=None, stream=None, clock=None):
        from trading.connectors.binance import BinanceReadOnlyClient
        from trading.connectors.binance_stream import BookTickerStream, ClockSync
        self.client = client or BinanceReadOnlyClient.public()
        self.clock = clock or ClockSync(server_time=self.client.server_time)
        self.stream = stream or BookTickerStream(clock=self.clock)
        self.rest_fallbacks = 0

    def start(self) -> None:
        self.clock.measure()
        self.stream.start()

    def stop(self) -> None:
        self.stream.stop()

    def now(self) -> datetime:
        return self.clock.now()

    def exchange_info(self) -> dict:
        return self.client.exchange_info("BTCUSDT")

    def closed_bars(self, start: datetime, end: datetime) -> list[Bar]:
        return self.client.closed_klines(start, end, now=self.now())

    def quotes(self, *, stale_after: float = 10.0) -> tuple[list[Quote], str]:
        quotes = self.stream.drain()
        status = self.stream.status(self.now())
        age = status["last_message_age_s"]
        if quotes and status["connected"] and age is not None and age <= stale_after:
            return quotes, "websocket"
        try:  # WebSocket down or silent: REST snapshot stamped with exchange server time
            quote = self.client.book_ticker("BTCUSDT")
        except Exception as exc:  # noqa: BLE001 - reported to the runner as an interruption
            return quotes, f"unavailable: {type(exc).__name__}"
        self.rest_fallbacks += 1
        return quotes + [quote], "rest_fallback"


# --------------------------------------------------------------------------- live Kronos forecasts

class LiveKronos:
    """One-step Kronos-mini forecasts for each newly closed bar (same settings as KR-05 / the research run),
    persisted to JSONL so restarts replay identical forecasts."""

    def __init__(self, path: Path, *, lookback: int = 128, predictor=None):
        self.path, self.lookback, self._predictor = Path(path), lookback, predictor
        self.forecasts: dict[datetime, float] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line:
                    item = json.loads(line)
                    self.forecasts[datetime.fromisoformat(item["timestamp"])] = item["predicted_close"]

    def _get(self):
        if self._predictor is None:
            import random

            import numpy as np
            import torch

            from model import Kronos, KronosPredictor, KronosTokenizer
            random.seed(123)
            np.random.seed(123)
            torch.manual_seed(123)
            tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
            model = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
            model.eval()
            tokenizer.eval()
            self._predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
        return self._predictor

    def forecast(self, history: list[Bar]) -> float:
        """Forecast the close of the bar after history[-1] from the last `lookback` closed bars only."""
        import pandas as pd
        target = history[-1].close_time
        if target in self.forecasts:
            return self.forecasts[target]
        if len(history) < self.lookback:
            raise ValidationError("Not enough history for a Kronos forecast")
        context = history[-self.lookback:]
        frame = pd.DataFrame({"open": [float(b.open) for b in context], "high": [float(b.high) for b in context],
                              "low": [float(b.low) for b in context], "close": [float(b.close) for b in context]})
        stamps = pd.Series(pd.to_datetime([b.open_time for b in context], utc=True))
        predicted = self._get().predict(df=frame, x_timestamp=stamps,
                                        y_timestamp=pd.Series(pd.to_datetime([target], utc=True)), pred_len=1, T=1.0,
                                        top_k=1, top_p=1.0, sample_count=1, verbose=False)
        value = float(predicted["close"].iloc[0])
        if not value > 0:
            raise ValidationError("Kronos returned an invalid forecast")
        self.forecasts[target] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": target.isoformat(), "predicted_close": value,
                                     "context_last_close": float(context[-1].close)}) + "\n")
        return value


# --------------------------------------------------------------------------- runner

@dataclass
class Runner:
    config: dict
    prereg: dict
    feed: object
    state_dir: Path
    clock: Callable[[], datetime] | None = None
    calendar_service: CalendarService | None = None
    kronos: LiveKronos | None = None
    engine: ShadowEngine | None = None
    _last: dict = field(default_factory=dict)
    _buffer: list = field(default_factory=list)

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = self.clock or self.feed.now
        self.store = ShadowStore(self.state_dir / "shadow.sqlite")
        self.breaker = CircuitBreaker(MARKET, kill_file=self.state_dir / "KILL",
                                      state_path=self.state_dir / "breaker.json")
        self.runner_cfg = self.config["runner"]
        self.kronos = self.kronos or LiveKronos(self.state_dir / "kronos_forecasts.jsonl")
        self.data_status = {"quotes": "not started", "klines": "not started"}

    def start(self) -> dict:
        environment = load_demo_environment()
        self.feed.start()
        now = self.clock()
        stored = self.store.get_meta("preregistration_sha256")
        if stored is None:
            self.store.set_meta("preregistration_sha256", self.config["preregistration_sha256"])
            self.store.set_meta("created_at", now.isoformat())
        elif stored != self.config["preregistration_sha256"]:
            raise ValidationError("State was created under a different pre-registration; refusing to continue")
        instruments, info = self._specification(now)
        self._calendar(now)
        limits = limits_from(self.config)
        h = self.config["health"]
        health = MarketHealthMonitor(MARKET, max_quote_age=timedelta(seconds=h["max_quote_age_s"]),
                                     max_quote_gap=timedelta(seconds=h["max_quote_gap_s"]),
                                     abnormal_spread_multiple=Decimal(h["abnormal_spread_multiple"]),
                                     max_status_age=timedelta(minutes=h["max_status_age_min"]))
        health.record_binance_status(info["status"], now)
        settings = EngineSettings(unseen_start=datetime.fromisoformat(self.prereg["unseen_window_starts"]),
                                  max_decision_latency=timedelta(seconds=self.runner_cfg["max_decision_latency_s"]),
                                  spec_max_age=timedelta(hours=self.runner_cfg["spec_refresh_hours"] * 2),
                                  fallback_half_spread=Decimal(self.config["fallback_half_spread_usdt"]),
                                  market=MARKET, interval="1h")
        daily = DailyLossLedger(self.state_dir / "daily.sqlite", market=MARKET, boundary=DayBoundary.utc(),
                                halt_fraction=limits.daily_loss_halt_fraction)
        self.engine = ShadowEngine(store=self.store, books=build_books(self.config),
                                   strategy_factory=strategy_factory(self.config, self.kronos.forecasts),
                                   instruments=instruments, spec_retrieved_at=now, limits=limits,
                                   environment=environment, breaker=self.breaker, daily=daily,
                                   calendar=BTCUSDT_ALWAYS_OPEN, event_guard=self._guard, swap=None,
                                   settings=settings, health=health)
        restored = self.engine.restore()
        self.store.event(now, "start", restored=restored, books=len(self.engine.books),
                         fee_verified=False, clock_offset_ms=self.feed.clock.offset.total_seconds() * 1000)
        if self.engine.last_bar_close is None:
            start = datetime.fromisoformat(self.config["warmup_start"])
            bars = self.feed.closed_bars(start, floor_hour(now))
            self._ingest(bars, [], now)
            self.store.event(now, "warmup_loaded", bars=len(bars), first=bars[0].open_time.isoformat() if bars else None,
                             last=bars[-1].close_time.isoformat() if bars else None)
        return {"restored": restored, "status": self.engine.status()}

    def _specification(self, now: datetime):
        from trading.connectors.binance import parse_symbol
        info = self.feed.exchange_info()
        instruments = {}
        for fee in self.config["fee_scenarios"]:
            discovery = parse_symbol(info, source="binance_public_market_data", taker_fee=Decimal(fee),
                                     fee_source="assumed")
            instruments[Decimal(fee)] = discovery.to_instrument_spec(allow_assumed_fee=True)
        spec = next(iter(instruments.values()))
        if str(spec.tick_size.normalize()) != str(Decimal(self.config["tick_size"]).normalize()):
            raise ValidationError(f"Exchange tick size {spec.tick_size} differs from the frozen config")
        self.store.event(now, "specification", status=info.get("status"), tick_size=spec.tick_size,
                         step_size=spec.step_size, min_qty=spec.min_qty, min_notional=spec.min_notional,
                         fee_scenarios=self.config["fee_scenarios"],
                         fee_note="Binance published base-tier spot taker fee (0.1%) and a stress scenario; "
                                  "not verified for any account")
        self._last["spec"] = now
        return instruments, {"status": info.get("status")}

    def _calendar(self, now: datetime) -> None:
        if self.calendar_service is None:
            cal = self.config["calendar"]
            providers = official_providers() if cal["official_providers"] else []
            providers.append(structured_file_provider(Path(cal["cpi_nfp_file"]), provider=cal["cpi_nfp_provider"],
                                                      series={"US_CPI", "US_NFP"}))
            self.calendar_service = CalendarService(EventStore(self.state_dir / "calendar.sqlite"), providers)
        results = self.calendar_service.refresh(now)
        failed = {k: v["error"] for k, v in results.items() if not v["ok"]}
        self.store.event(now, "calendar_refresh_failed" if failed else "calendar_refresh",
                         results={k: v.get("ok") for k, v in results.items()}, errors=failed)
        self._event_guard = self.calendar_service.guard(MARKET)
        self._last["calendar"] = now

    def _guard(self, now: datetime) -> str | None:
        """Calendar blackout plus data-integrity checks (stale klines, clock offset). Market health is added by
        the engine. Any reason rejects new simulated entries; open positions keep their stops."""
        last_close = self.engine.last_bar_close if self.engine else None
        if last_close is None or now - last_close > H1 + timedelta(seconds=self.runner_cfg["max_kline_lag_s"]):
            return f"closed klines stale (last close {last_close})"
        offset = abs(self.feed.clock.offset.total_seconds())
        if offset > self.runner_cfg["max_clock_offset_s"]:
            return f"local clock differs from Binance server time by {offset:.2f} s"
        return self._event_guard(now)

    def _ingest(self, bars: list[Bar], quotes: list[Quote], now: datetime) -> dict:
        """Gap detection, live Kronos forecasts for new bars, then one atomic engine batch."""
        bars = sorted(bars, key=lambda b: b.open_time)
        previous = self.engine.last_bar_close
        for bar in bars:
            if previous is not None and bar.open_time > previous:
                self.store.event(now, "kline_gap", missing_from=previous.isoformat(), missing_to=bar.open_time.isoformat(),
                                 note="Binance returned no candle for this interval after re-request")
            previous = bar.close_time if previous is None or bar.close_time > previous else previous
        unseen = self.engine.settings.unseen_start
        history = list(self.engine.bars)
        for bar in bars:
            if self.engine.last_bar_close is not None and bar.close_time <= self.engine.last_bar_close:
                continue
            history.append(bar)
            if bar.close_time >= unseen - H1 and "kronos" in self.config["candidates"]:
                try:
                    self.kronos.forecast(history)
                except Exception as exc:  # Kronos unavailable: that candidate simply has no signal
                    self.store.event(now, "kronos_unavailable", bar=bar.open_time.isoformat(),
                                     error=f"{type(exc).__name__}: {str(exc)[:120]}")
        return self.engine.process(bars, quotes, now)

    def poll(self) -> dict:
        now = self.clock()
        engine = self.engine
        bars = []
        due = (engine.last_bar_close or now) + H1 + timedelta(seconds=self.runner_cfg["kline_settle_s"])
        if now >= due:
            try:
                bars = self.feed.closed_bars(engine.last_bar_close, floor_hour(now))
                self.data_status["klines"] = "ok"
            except Exception as exc:  # noqa: BLE001 - recorded; stale-kline guard rejects entries meanwhile
                self.data_status["klines"] = f"unavailable: {type(exc).__name__}"
                self.store.event(now, "data_interruption", source="klines", error=f"{type(exc).__name__}: {exc}")
        quotes, source = self.feed.quotes(stale_after=self.runner_cfg["ws_stale_s"])
        if source != self.data_status["quotes"]:
            self.store.event(now, "quote_source", source=source)
            self.data_status["quotes"] = source
        self._buffer.extend(quotes)
        counts = self._ingest(bars, self._buffer, now)
        floor = engine.last_tick.time if engine.last_tick else None
        self._buffer = [q for q in self._buffer if floor is None or q.time > floor][-self.runner_cfg["max_buffer"]:]
        return counts

    def housekeeping(self) -> None:
        now = self.clock()
        cfg, epoch = self.runner_cfg, datetime.min.replace(tzinfo=timezone.utc)
        if now - self._last.get("heartbeat", epoch) >= timedelta(seconds=cfg["heartbeat_seconds"]):
            guard = None
            try:
                guard = self.engine._guard()(now)
            except Exception as exc:  # noqa: BLE001
                guard = f"guard error: {exc}"
            self.store.heartbeat(now, status="ok" if guard is None else "entries_blocked", guard=guard,
                                 data=dict(self.data_status), stream=self.feed.stream.status(now),
                                 last_bar_close=str(self.engine.last_bar_close), breaker=self.breaker.reasons(),
                                 open_positions=len(self.engine.positions))
            self._last["heartbeat"] = now
        if now - self._last.get("exchange_status", epoch) >= timedelta(minutes=cfg["exchange_status_minutes"]):
            try:
                info = self.feed.exchange_info()
                self.engine.health.record_binance_status(info.get("status", "unknown"), now)
            except Exception as exc:  # noqa: BLE001 - status goes stale -> entries rejected
                self.store.event(now, "data_interruption", source="exchangeInfo", error=type(exc).__name__)
            self._last["exchange_status"] = now
        if now - self._last.get("clock", epoch) >= timedelta(minutes=cfg["clock_sync_minutes"]):
            try:
                offset = self.feed.clock.measure()
                if abs(offset.total_seconds()) > cfg["max_clock_offset_s"]:
                    self.store.event(now, "clock_offset", offset_s=offset.total_seconds())
            except Exception as exc:  # noqa: BLE001
                self.store.event(now, "data_interruption", source="server_time", error=type(exc).__name__)
            self._last["clock"] = now
        if now - self._last.get("calendar", now) >= timedelta(hours=self.config["calendar"]["refresh_hours"]):
            self._calendar(now)
        if now - self._last.get("spec", now) >= timedelta(hours=cfg["spec_refresh_hours"]):
            instruments, _ = self._specification(now)
            self.engine.instruments, self.engine.spec_retrieved_at = instruments, now
        if now - self._last.get("reconcile", epoch) >= timedelta(hours=1):
            self.store.event(now, "reconciliation", **self.engine.reconcile())
            self._last["reconcile"] = now
        if self.breaker.kill_file is not None and self.breaker.kill_file.exists() and not self._last.get("killed"):
            self.store.event(now, "kill_file", reasons=self.breaker.reasons())
            self._last["killed"] = True

    def run(self, *, once: bool = False, max_minutes: float | None = None,
            sleep: Callable[[float], None] = time.sleep) -> dict:
        started = self.clock()
        result = self.start()
        failures = 0
        try:
            while True:
                if (self.state_dir / "STOP").exists():
                    self.store.event(self.clock(), "stop_file")
                    break
                try:
                    result = self.poll()
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - recorded; back off and continue
                    failures += 1
                    self.store.event(self.clock(), "data_interruption", source="poll",
                                     error=f"{type(exc).__name__}: {str(exc)[:200]}", consecutive=failures)
                    sleep(min(60, 2 * failures))
                try:
                    self.housekeeping()
                except Exception as exc:  # noqa: BLE001
                    self.store.event(self.clock(), "housekeeping_error", error=f"{type(exc).__name__}: {exc}")
                if once or (max_minutes is not None and self.clock() - started >= timedelta(minutes=max_minutes)):
                    break
                sleep(self.runner_cfg["poll_seconds"])
        finally:
            self.feed.stop()
        return {"last_poll": result, "status": self.engine.status()}


# --------------------------------------------------------------------------- reports / CLI

def report(config: dict, prereg: dict, store: ShadowStore, now: datetime) -> dict:
    base = build_report_btc(store, prereg, now=now, poll_seconds=config["runner"]["poll_seconds"])
    books = store.books()
    criteria = {}
    for cand in prereg["candidates"]:
        key = cand["key"]
        controls = [k for k in strategy_table(config) if k.startswith(f"random_{key}_s")]
        criteria[key] = candidate_criteria(
            store, books, candidate_key=key, control_keys=controls,
            criteria_text=prereg["evaluation"]["success_criteria_all_required"],
            unseen_start=datetime.fromisoformat(prereg["unseen_window_starts"]),
            final_date=datetime.fromisoformat(prereg["evaluation"]["final_checkpoint_not_before"]),
            min_final_trades=prereg["evaluation"]["final_checkpoint_min_trades"],
            equity=Decimal(config["equity"]), cost_key=Decimal(prereg["evaluation"]["fee_scenario"]), now=now)
    base["preregistered_criteria"] = criteria
    return base


def build_report_btc(store: ShadowStore, prereg: dict, *, now: datetime, poll_seconds: float) -> dict:
    """Reuse the shared report body; the XAUUSD-specific criteria block is replaced by the per-candidate one."""
    shim = {"unseen_window_starts": prereg["unseen_window_starts"], "candidates": [{"id": "n/a"}],
            "evaluation": {"success_criteria_all_required": [""] * 5, "checkpoints": ["", ""]}}
    body = build_report(store, shim, now=now, poll_seconds=poll_seconds)
    body.pop("preregistered_criteria", None)
    body.pop("mt5_account_checks", None)
    body["notes"] = ["Simulated fills against live Binance public best bid/ask; no orders (real or testnet).",
                     "Fee scenarios are assumptions (Binance base-tier 0.1% and a 0.2% stress); not account-verified.",
                     "Bars closing before the unseen-data start are indicator warm-up only and never traded."]
    body["data_interruptions"] = {k: v for k, v in _count(store, ("data_interruption", "kline_gap", "quote_source",
                                                                  "kronos_unavailable", "clock_offset")).items()}
    return body


def _count(store: ShadowStore, kinds) -> dict:
    out = {}
    for e in store.events():
        if e["kind"] in kinds:
            out[e["kind"]] = out.get(e["kind"], 0) + 1
    return out


def to_markdown(rep: dict) -> str:
    lines = [f"# Phase 2E BTC/USDT shadow report - {rep['generated_at']}", "",
             "Simulation only (no orders). Fee scenarios are assumptions.", "",
             f"Unseen data from {rep['unseen_window_starts']}. Signals by status: {rep['signals']}", "",
             "| Book | Trades | Net % | Max DD % | Win % | PF | Mean R | Rejected | Fees | Spread+slip |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in rep["books"].values():
        c = m["costs"]
        lines.append(f"| {m['book_id']} | {m['trades']} | {m['net_return_pct']:.2f} | {m['max_drawdown_pct']:.2f} | "
                     f"{m['win_rate_pct']} | {m['profit_factor']} | {m['mean_r']} | {m['rejected']} | "
                     f"{c['commission']:.2f} | {c['spread_and_slippage']:.2f} |")
    for key, crit in rep["preregistered_criteria"].items():
        lines += ["", f"## {key}: {crit['verdict']} ({crit['trades']} trades)"]
        lines += [f"- {c['status']}: {c['criterion']}" for c in crit["criteria"]]
    u = rep["uptime"]
    lines += ["", f"Uptime {u.get('uptime_pct')}% over {u.get('monitored_hours')} h; interruptions "
              f"{rep['data_interruptions']}", ""]
    return "\n".join(lines)


def cmd_run(args) -> dict:
    config, prereg = load_config(Path(args.config))
    runner = Runner(config, prereg, BinanceFeed(), Path(args.state_dir or config["state_dir"]))
    return runner.run(once=args.once, max_minutes=args.max_minutes)


def cmd_report(args) -> dict:
    config, prereg = load_config(Path(args.config))
    store = ShadowStore(Path(args.state_dir or config["state_dir"]) / "shadow.sqlite")
    rep = report(config, prereg, store, datetime.now(timezone.utc))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_jsonable(rep), indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(rep), encoding="utf-8")
    return {"output": str(out), "verdicts": {k: v["verdict"] for k, v in rep["preregistered_criteria"].items()}}


def cmd_status(args) -> dict:
    config, _ = load_config(Path(args.config))
    state = Path(args.state_dir or config["state_dir"])
    store = ShadowStore(state / "shadow.sqlite")
    breaker = CircuitBreaker(MARKET, kill_file=state / "KILL", state_path=state / "breaker.json")
    beats = store.heartbeats()
    return {"bars": store.bar_count(), "live_bars": store.bar_count("live"), "signals": len(store.signals()),
            "decisions": len(store.decisions()), "trades": len(store.trades()), "positions": store.positions(),
            "breaker": breaker.reasons(), "last_heartbeat": beats[-1] if beats else None,
            "last_events": store.events()[-8:]}


def cmd_reset_breaker(args) -> dict:
    config, _ = load_config(Path(args.config))
    state = Path(args.state_dir or config["state_dir"])
    breaker = CircuitBreaker(MARKET, kill_file=state / "KILL", state_path=state / "breaker.json")
    breaker.reset(operator=args.operator)
    return {"reasons_after_reset": breaker.reasons()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("run", cmd_run), ("report", cmd_report), ("status", cmd_status),
                       ("reset-breaker", cmd_reset_breaker)):
        p = sub.add_parser(name)
        p.add_argument("--config", default="research/configs/phase2e_binance_shadow.json")
        p.add_argument("--state-dir", default=None)
        p.set_defaults(func=func)
        if name == "run":
            p.add_argument("--once", action="store_true")
            p.add_argument("--max-minutes", type=float, default=None)
        if name == "report":
            p.add_argument("--output", default="research/output/phase2e_binance_shadow_report.json")
        if name == "reset-breaker":
            p.add_argument("--operator", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(to_jsonable(args.func(args)), indent=2, default=str))


if __name__ == "__main__":
    main()

"""Phase 2D — XAUUSD shadow forward test (read-only MT5; simulation only; no orders).

Subcommands:
  build-calendar  Write the US CPI / Employment Situation schedule file (BLS release schedule, cross-checked
                  against the OMB "Schedule of Release Dates for Principal Federal Economic Indicators for 2027").
  run             Live loop: MT5 closed M15 bars + ticks -> ORB New York (frozen), H1 EMA-cross and 5 random
                  controls -> unchanged risk engine -> simulated fills. Books: 10,000 and 3,000 USD x commission
                  0 and 7 USD/lot round trip. State persists in state/phase2d_shadow/ (restart-safe).
  report          Metrics, comparison, rejections, risk checks, uptime and the pre-registered criteria.
  status          Engine/breaker status from the store.
  reset-breaker   Manual breaker reset (records the operator).

Safety: the MT5 connection is the allowlist `ReadOnlyMt5` proxy (no order functions are reachable); the
account must report the demo trade mode; EXECUTION_MODE must be 'demo'. Emergency stop: create the file
state/phase2d_shadow/KILL (rejects every new simulated entry; open simulated positions keep their stops).
Graceful exit: create state/phase2d_shadow/STOP.
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
from trading.clock import NEW_YORK, ServerTimeZone
from trading.common import Market, ValidationError
from trading.config import load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.marketcal.events import EconomicEvent, EventStore
from trading.marketcal.service import CalendarService, official_providers, structured_file_provider
from trading.marketdata import Bar, Quote
from trading.risk import RiskLimits
from trading.sessions import TradingCalendar
from trading.shadow.costs import WEEKDAYS, SwapSpec
from trading.shadow.engine import BookSpec, EngineSettings, ShadowEngine
from trading.shadow.report import build_report, to_markdown
from trading.shadow.store import ShadowStore
from trading.strategies.base import params_hash
from trading.strategies.baselines import (H1TrendBaseline, H1TrendBaselineParams, RandomEntryBaseline,
                                          RandomEntryParams)
from trading.strategies.xauusd import OpeningRangeBreakout, OpeningRangeParams

MARKET = Market.XAUUSD_MT5
M15 = timedelta(minutes=15)
log = logging.getLogger("research.phase2d_shadow")

# --------------------------------------------------------------------------- US CPI / NFP schedule
# Source of truth: BLS release schedule pages (https://www.bls.gov/schedule/news_release/cpi.htm,
# .../empsit.htm, .../schedule/2026/11_sched_list.htm, 12_sched_list.htm), retrieved 2026-11-07; all 08:30 AM
# Eastern Time. Cross-check: OMB/OIRA "Schedule of Release Dates for Principal Federal Economic Indicators for
# 2027" (https://www.whitehouse.gov/wp-content/uploads/2026/10/FY2027-PFEI.pdf, sha256 20482c7a...bde20) lists the
# same 2027 dates (it gives dates only, no times). Dates are subject to change by BLS; refresh before expiry.
CALENDAR_RETRIEVED_AT = "2026-11-07T12:00:00+00:00"
CPI_NFP_SCHEDULE = {
    "US_CPI": ("Consumer Price Index", "https://www.bls.gov/schedule/news_release/cpi.htm", [
        ("2026-10", "2026-11-10"), ("2026-11", "2026-12-10"), ("2026-12", "2027-01-13"), ("2027-01", "2027-02-11"),
        ("2027-02", "2027-03-10"), ("2027-03", "2027-04-13"), ("2027-04", "2027-05-12"), ("2027-05", "2027-06-10"),
        ("2027-06", "2027-07-14"), ("2027-07", "2027-08-11"), ("2027-08", "2027-09-14"), ("2027-09", "2027-10-14"),
        ("2027-10", "2027-11-10"), ("2027-11", "2027-12-10")]),
    "US_NFP": ("Employment Situation", "https://www.bls.gov/schedule/news_release/empsit.htm", [
        ("2026-10", "2026-11-06"), ("2026-11", "2026-12-04"), ("2026-12", "2027-01-08"), ("2027-01", "2027-02-05"),
        ("2027-02", "2027-03-05"), ("2027-03", "2027-04-02"), ("2027-04", "2027-05-07"), ("2027-05", "2027-06-04"),
        ("2027-06", "2027-07-02"), ("2027-07", "2027-08-06"), ("2027-08", "2027-09-03"), ("2027-09", "2027-10-08"),
        ("2027-10", "2027-11-05"), ("2027-11", "2027-12-03")]),
}


def cpi_nfp_events(provider: str) -> list[EconomicEvent]:
    retrieved = datetime.fromisoformat(CALENDAR_RETRIEVED_AT)
    events = []
    for series, (name, url, rows) in CPI_NFP_SCHEDULE.items():
        for period, day in rows:
            local = datetime.fromisoformat(day).replace(hour=8, minute=30)
            events.append(EconomicEvent(
                event_id=f"{series.lower()}-{period}", name=name, country="US", currency="USD",
                scheduled_at=NEW_YORK.to_utc(local), impact="high", provider=provider, retrieved_at=retrieved,
                updated_at=retrieved, category="inflation" if series == "US_CPI" else "employment",
                source_url=url, series=series, reference_period=period))
    for event in events:
        event.validate()
    return events


def cmd_build_calendar(args) -> dict:
    events = cpi_nfp_events(args.provider)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([e.to_json() for e in events], indent=2) + "\n", encoding="utf-8")
    return {"events": len(events), "output": str(out), "coverage_end": max(e.scheduled_at for e in events).isoformat()}


# --------------------------------------------------------------------------- configuration and pre-registration

def normalized_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_config(path: Path) -> tuple[dict, dict]:
    """Load the shadow config and its pre-registration; refuse any drift from the frozen parameters."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    prereg_path = Path(config["preregistration"])
    if normalized_sha256(prereg_path) != config["preregistration_sha256"]:
        raise ValidationError("Pre-registration file changed since Phase 2D was configured (frozen)")
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    check_preregistration(config, prereg)
    return config, prereg


def check_preregistration(config: dict, prereg: dict) -> None:
    s = config["strategies"]
    cand = prereg["candidates"][0]
    if (cand["id"], cand["version"]) != (s["orb_newyork"]["prereg_id"], OpeningRangeBreakout.version):
        raise ValidationError("Candidate id/version differs from the pre-registration")
    if s["orb_newyork"]["params"] != cand["params"] or params_hash(OpeningRangeParams(**cand["params"])) != \
            cand["params_hash"] or s["orb_newyork"]["max_holding_bars"] != cand["max_holding_bars"]:
        raise ValidationError("ORB New York parameters differ from the pre-registration (frozen)")
    trend, rnd = prereg["controls"][0], prereg["controls"][1]
    if s["h1_ema_cross"]["params"] != trend["params"] or \
            params_hash(H1TrendBaselineParams(**trend["params"])) != trend["params_hash"]:
        raise ValidationError("H1 EMA-cross control differs from the pre-registration")
    if s["random"]["prereg_family"] != rnd["family"] or s["random"]["seeds"] != rnd["seeds"]:
        raise ValidationError("Random-entry control differs from the pre-registration")
    if config["risk"]["risk_fraction"] != prereg["rules"]["risk_fraction"]:
        raise ValidationError("Risk fraction differs from the pre-registration")


def strategy_table(config: dict) -> dict[str, tuple[str, dict, int | None]]:
    s = config["strategies"]
    table = {"orb_newyork": ("orb", s["orb_newyork"]["params"], s["orb_newyork"]["max_holding_bars"]),
             "h1_ema_cross": ("h1_trend", s["h1_ema_cross"]["params"], s["h1_ema_cross"]["max_holding_bars"])}
    for seed in s["random"]["seeds"]:
        table[f"random_s{seed}"] = ("random", {**s["random"]["params"], "seed": seed}, s["random"]["max_holding_bars"])
    return table


def strategy_factory(config: dict) -> Callable[[str], Callable]:
    table, tick = strategy_table(config), Decimal("0.01")

    def make(key: str):
        kind, params, _ = table[key]
        if kind == "orb":
            return OpeningRangeBreakout(OpeningRangeParams(**params), tick_size=tick, dataset_id="mt5-live-shadow")
        if kind == "h1_trend":
            return H1TrendBaseline(H1TrendBaselineParams(**params), tick_size=tick, dataset_id="mt5-live-shadow")
        return RandomEntryBaseline(RandomEntryParams(**params), tick_size=tick, dataset_id="mt5-live-shadow")
    return make


def build_books(config: dict) -> list[BookSpec]:
    books = []
    for key, (_, _, holding) in strategy_table(config).items():
        for equity in config["equities"]:
            for commission in config["commission_scenarios_usd_per_lot_round_trip"]:
                books.append(BookSpec(f"{key}.e{equity}.c{commission}", key, Decimal(equity), Decimal(commission),
                                      holding))
    return books


def limits_from(config: dict) -> RiskLimits:
    r = config["risk"]
    limits = RiskLimits(market=MARKET, max_spread=Decimal(r["max_spread"]),
                        slippage_allowance=Decimal(r["slippage_allowance"]), risk_fraction=Decimal(r["risk_fraction"]),
                        daily_loss_halt_fraction=Decimal(r["daily_loss_halt_fraction"]),
                        aggregate_risk_fraction=Decimal(r["aggregate_risk_fraction"]))
    limits.validate()
    return limits


def swap_from_symbol_info(info: dict, *, server_tz: ServerTimeZone, usd_per_point: Decimal,
                          fallback: dict) -> SwapSpec:
    """Broker swap from MT5 symbol_info (swap_mode 1 = points). Anything else -> configured fallback,
    explicitly marked unverified."""
    mode, rollover = info.get("swap_mode"), info.get("swap_rollover3days")
    if mode == 1 and info.get("swap_long") is not None and info.get("swap_short") is not None \
            and isinstance(rollover, int) and 0 <= rollover <= 6:
        weekday = WEEKDAYS[(rollover - 1) % 7]  # MQL5 ENUM_DAY_OF_WEEK: SUNDAY=0 ... SATURDAY=6
        return SwapSpec(Decimal(repr(float(info["swap_long"]))), Decimal(repr(float(info["swap_short"]))),
                        usd_per_point, weekday, server_tz, "mt5.symbol_info (read-only)", True)
    return SwapSpec(Decimal(fallback["long_points_per_lot_night"]), Decimal(fallback["short_points_per_lot_night"]),
                    Decimal(fallback["usd_per_point_per_lot"]), fallback["triple_weekday_server"], server_tz,
                    f"fallback: {fallback['source']} (live swap_mode={mode!r} unsupported/unavailable)", False)


def floor15(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment - timedelta(minutes=moment.minute % 15)


# --------------------------------------------------------------------------- live feed (read-only MT5)

class Mt5Feed:
    """Thin read-only adapter over `Mt5ReadOnlyConnector` (allowlist proxy; demo account enforced)."""

    def __init__(self, connector):
        self.conn = connector
        self.name: str | None = None

    def connect(self) -> dict:
        summary = self.conn.connect()
        self.name = self.conn.discover_xauusd()
        return summary

    def reconnect(self) -> dict:
        try:
            self.conn.shutdown()
        except Exception:  # already disconnected
            pass
        return self.connect()

    def specification(self):
        return self.conn.symbol_specification(self.name)

    def symbol_info(self) -> dict:
        from trading.connectors.mt5 import _as_dict
        return _as_dict(self.conn._require().symbol_info(self.name))

    def verify_clock(self) -> dict:
        return self.conn.verify_server_time(self.name)

    def closed_bars(self, start: datetime, end: datetime) -> list[Bar]:
        return [b.bar for b in self.conn.history(self.name, "15m", start, end)]

    def ticks(self, start: datetime, end: datetime) -> list[Quote]:
        from trading.connectors.mt5 import read_ticks
        return [Quote.create(symbol="XAUUSD", bid=b, ask=a, time=t)
                for t, b, a in read_ticks(self.conn, self.name, start, end, chunk=timedelta(hours=1))]

    def account_state(self) -> dict:
        from trading.connectors.mt5 import _as_dict
        mt5 = self.conn._require()
        account = _as_dict(mt5.account_info())
        return {"balance": account.get("balance"), "equity": account.get("equity"),
                "trade_mode": account.get("trade_mode"), "positions_total": mt5.positions_total(),
                "orders_total": mt5.orders_total()}

    def shutdown(self) -> None:
        self.conn.shutdown()


# --------------------------------------------------------------------------- runner

@dataclass
class Runner:
    config: dict
    prereg: dict
    feed: object
    state_dir: Path
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    calendar_service: CalendarService | None = None
    server_tz: ServerTimeZone | None = None
    engine: ShadowEngine | None = None
    _last: dict = field(default_factory=dict)

    def __post_init__(self):
        from research.phase2_xauusd import server_tz_from_args
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.server_tz = self.server_tz or server_tz_from_args(self.config["server_tz"])
        self.store = ShadowStore(self.state_dir / "shadow.sqlite")
        self.breaker = CircuitBreaker(MARKET, kill_file=self.state_dir / "KILL",
                                      state_path=self.state_dir / "breaker.json")
        self.runner_cfg = self.config["runner"]

    # -- start-up ----------------------------------------------------------------------------
    def start(self) -> dict:
        environment = load_demo_environment()  # EXECUTION_MODE=demo, no live flags or production keys
        now = self.clock()
        stored = self.store.get_meta("preregistration_sha256")
        if stored is None:
            self.store.set_meta("preregistration_sha256", self.config["preregistration_sha256"])
            self.store.set_meta("config_sha256", hashlib.sha256(json.dumps(
                self.config, sort_keys=True).encode()).hexdigest())
            self.store.set_meta("created_at", now.isoformat())
        elif stored != self.config["preregistration_sha256"]:
            raise ValidationError("State was created under a different pre-registration; refusing to continue")
        summary = self.feed.connect()  # Mt5ReadOnlyConnector raises unless the account is a demo account
        baseline = self.feed.account_state()
        self.store.event(now, "mt5_account_baseline", account={k: summary.get(k) for k in (
            "server", "is_demo", "currency", "leverage")}, **baseline)
        self._last["account_baseline"] = baseline
        instruments, spec_meta = self._specification(now)
        self._calendar_service(now)
        calendar = self._provisional_calendar(now)
        settings = EngineSettings(unseen_start=datetime.fromisoformat(self.prereg["unseen_window_starts"]),
                                  max_decision_latency=timedelta(seconds=self.runner_cfg["max_decision_latency_s"]))
        limits = limits_from(self.config)
        daily = DailyLossLedger(self.state_dir / "daily.sqlite", market=MARKET,
                                boundary=DayBoundary.server(calendar.server_tz, calendar.risk_day_shift),
                                halt_fraction=limits.daily_loss_halt_fraction)
        h = self.config["health"]
        health = MarketHealthMonitor(MARKET, max_quote_age=timedelta(seconds=h["max_quote_age_s"]),
                                     max_quote_gap=timedelta(seconds=h["max_quote_gap_s"]),
                                     abnormal_spread_multiple=Decimal(h["abnormal_spread_multiple"]))
        self.engine = ShadowEngine(store=self.store, books=build_books(self.config),
                                   strategy_factory=strategy_factory(self.config), instruments=instruments,
                                   spec_retrieved_at=now, limits=limits, environment=environment,
                                   breaker=self.breaker, daily=daily, calendar=calendar,
                                   event_guard=self._event_guard, swap=spec_meta["swap"], settings=settings,
                                   health=health)
        restored = self.engine.restore()
        self.store.event(now, "start", restored=restored, swap=spec_meta["swap_record"],
                         commission_verified=False, books=len(self.engine.books))
        if self.engine.last_bar_close is None:  # first start: warm-up history (indicators only)
            warmup = datetime.fromisoformat(self.config["warmup_start"])
            bars = self.feed.closed_bars(warmup, floor15(now))
            self.engine.process(bars, [], now)
            self.store.event(now, "warmup_loaded", bars=len(bars),
                             first=bars[0].open_time.isoformat() if bars else None,
                             last=bars[-1].close_time.isoformat() if bars else None)
        self._refresh_sessions(now)
        return {"restored": restored, "status": self.engine.status()}

    def _specification(self, now: datetime):
        discovery = self.feed.specification()
        commissions = [Decimal(c) for c in self.config["commission_scenarios_usd_per_lot_round_trip"]]
        instruments = {c: discovery.to_instrument_spec(commission_assumption=c) for c in commissions}
        spec = instruments[commissions[0]]
        usd_per_point = spec.tick_value_loss / spec.tick_size * spec.point
        swap = swap_from_symbol_info(self.feed.symbol_info(), server_tz=self.server_tz, usd_per_point=usd_per_point,
                                     fallback=self.config["swap_fallback"])
        record = {"long": swap.long_points_per_lot_night, "short": swap.short_points_per_lot_night,
                  "usd_per_point_per_lot": swap.usd_per_point_per_lot, "triple": swap.triple_weekday_server,
                  "source": swap.source, "verified": swap.verified}
        self.store.event(now, "specification", missing=discovery.missing, problems=discovery.problems,
                         tick_value_loss_used=spec.tick_value_loss, margin_per_lot=spec.margin_per_lot,
                         volume_min=spec.volume_min, swap=record,
                         commission_note="MT5 does not expose commission; scenarios are assumptions")
        self._last["spec"] = now
        return instruments, {"swap": swap, "swap_record": record}

    def _calendar_service(self, now: datetime) -> None:
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
        self._guard = self.calendar_service.guard(MARKET)
        events = self.calendar_service.store.between(now - timedelta(days=1), now + timedelta(days=800))
        cpi_nfp = [e.scheduled_at for e in events if e.series in ("US_CPI", "US_NFP")]
        self._coverage_end = max(cpi_nfp) if cpi_nfp else None
        self._last["calendar"] = now

    def _event_guard(self, now: datetime) -> str | None:
        if self._coverage_end is None or now > self._coverage_end - timedelta(days=7):
            return "US CPI/NFP schedule coverage missing or ending within 7 days (fail closed); refresh the file"
        return self._guard(now)

    def _provisional_calendar(self, now: datetime) -> TradingCalendar:
        from trading.marketcal.holidays import build_xauusd_calendar
        from trading.sessions import XAUUSD_NY_MARKET_HOURS
        calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, (now - timedelta(days=120)).date(),
                                            (now + timedelta(days=120)).date())
        return calendar  # rule_based: entries are rejected until the broker verification succeeds

    def _refresh_sessions(self, now: datetime) -> dict:
        """Broker-verified session calendar: NY market hours + CME holidays, intersected with the broker's
        observed sessions, promoted to broker_verified only with a fresh-tick offset check."""
        from trading.marketcal.holidays import broker_xauusd_calendar, mark_broker_verified
        weeks = self.config["calendar"]["session_weeks"]
        bars = self.engine.bars
        result = {"verification": self.engine.calendar.verification}
        if not bars:
            return result
        calendar, provenance = broker_xauusd_calendar(bars, server_tz=self.server_tz,
                                                      start=(now - timedelta(weeks=weeks + 1)).date(),
                                                      end=(now + timedelta(days=60)).date(), weeks=weeks)
        try:
            clock = self.feed.verify_clock()
            offset_ok = True
        except Exception as exc:  # market closed or stale tick: stay unverified (entries rejected)
            clock, offset_ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        recent = [b for b in bars if b.open_time >= bars[-1].open_time - timedelta(weeks=weeks)]
        try:
            calendar = mark_broker_verified(calendar, recent, offset_verified=offset_ok, min_bars=1000)
        except ValidationError as exc:
            result["error"] = str(exc)
        self.engine.calendar = calendar
        result.update(verification=calendar.verification, clock=clock,
                      broker_profile=provenance.get("broker_profile", {}).get("differences_vs_server_day"))
        self.store.event(now, "session_calendar", **result)
        self._last["sessions"] = now
        return result

    # -- polling ---------------------------------------------------------------------------------
    def poll(self) -> dict:
        now = self.clock()
        engine = self.engine
        engine.health.record_mt5_trade_mode(int(self.feed.symbol_info().get("trade_mode", -1)), now)
        start = engine.last_bar_close or datetime.fromisoformat(self.config["warmup_start"])
        bars = self.feed.closed_bars(start, floor15(now))
        cursor_text = self.store.get_meta("tick_cursor")
        cursor = datetime.fromisoformat(cursor_text) if cursor_text else (engine.last_bar_close or now)
        if engine.last_tick is not None:
            cursor = max(cursor, engine.last_tick.time)
        cursor = max(cursor, now - timedelta(hours=self.runner_cfg["max_tick_backfill_hours"]))
        limit = now - timedelta(seconds=self.runner_cfg["tick_settle_seconds"])
        totals = {"bars": 0, "ticks": 0, "deferred_ticks": 0, "signals": 0, "entries": 0, "exits": 0}
        waiting = sorted(bars, key=lambda b: b.open_time)
        while True:
            # Bars are released together with the ticks of the same span so events stay in time order
            # across calls (a bar closing at C is processed before any tick stamped >= C).
            end = min(cursor + timedelta(minutes=self.runner_cfg["tick_chunk_minutes"]), limit)
            ticks = self.feed.ticks(cursor, end) if end > cursor else []
            release = [b for b in waiting if b.close_time <= max(end, cursor)]
            waiting = waiting[len(release):]
            counts = engine.process(release, ticks, now)
            for k in totals:
                totals[k] += counts[k]
            if counts["deferred_ticks"]:
                break  # bar not yet published: keep the cursor at this chunk's start and retry next poll
            cursor = max(cursor, end)
            if cursor >= limit:
                break
        self.store.set_meta("tick_cursor", cursor.isoformat())
        return totals

    def housekeeping(self) -> None:
        now = self.clock()
        cfg = self.runner_cfg
        if now - self._last.get("heartbeat", datetime.min.replace(tzinfo=timezone.utc)) >= \
                timedelta(seconds=cfg["heartbeat_seconds"]):
            self.store.heartbeat(now, last_bar_close=str(self.engine.last_bar_close),
                                 breaker=self.breaker.reasons(), calendar=self.engine.calendar.verification)
            self._last["heartbeat"] = now
        if now - self._last.get("calendar", now) >= timedelta(hours=self.config["calendar"]["refresh_hours"]):
            self._calendar_service(now)
        if now - self._last.get("spec", now) >= timedelta(hours=cfg["spec_refresh_hours"]):
            instruments, meta = self._specification(now)
            self.engine.instruments, self.engine.swap, self.engine.spec_retrieved_at = instruments, meta["swap"], now
        sessions_due = timedelta(minutes=10) if self.engine.calendar.verification != "broker_verified" \
            else timedelta(hours=24)
        if now - self._last.get("sessions", now) >= sessions_due:
            self._refresh_sessions(now)
        if now - self._last.get("account", datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(minutes=10):
            state = self.feed.account_state()
            base = self._last.get("account_baseline", {})
            unchanged = all(state.get(k) == base.get(k) for k in ("balance", "positions_total", "orders_total"))
            self.store.event(now, "mt5_account_check", unchanged=unchanged, **state)
            self._last["account"] = now
        if now - self._last.get("reconcile", datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(hours=1):
            report = self.engine.reconcile()
            self.store.event(now, "reconciliation", **report)
            self._last["reconcile"] = now
        if self.breaker.kill_file is not None and self.breaker.kill_file.exists() and not self._last.get("killed"):
            self.store.event(now, "kill_file", reasons=self.breaker.reasons())
            self._last["killed"] = True

    def run(self, *, once: bool = False, max_minutes: float | None = None,
            sleep: Callable[[float], None] = time.sleep) -> dict:
        started = self.clock()
        result = self.start()
        failures = 0
        while True:
            if (self.state_dir / "STOP").exists():
                self.store.event(self.clock(), "stop_file")
                break
            try:
                result = self.poll()
                self.housekeeping()
                failures = 0
            except Exception as exc:  # data interruption: record, back off, reconnect
                failures += 1
                self.store.event(self.clock(), "data_interruption", error=f"{type(exc).__name__}: {exc}",
                                 consecutive=failures)
                log.warning("poll failed: %s", exc)
                sleep(min(60, 5 * failures))
                try:
                    self.feed.reconnect()
                    self.store.event(self.clock(), "reconnect")
                except Exception as reconnect_exc:
                    self.store.event(self.clock(), "mt5_error", error=f"{type(reconnect_exc).__name__}")
            if once or (max_minutes is not None and self.clock() - started >= timedelta(minutes=max_minutes)):
                break
            sleep(self.runner_cfg["poll_seconds"])
        self.feed.shutdown()
        return {"last_poll": result, "status": self.engine.status()}


# --------------------------------------------------------------------------- CLI

def _mt5_feed(config: dict) -> Mt5Feed:
    import os

    from research.phase2_xauusd import server_tz_from_args
    from trading.connectors.mt5 import Mt5ReadOnlyConnector
    login = os.environ.get("MT5_DEMO_LOGIN")
    return Mt5Feed(Mt5ReadOnlyConnector(server_tz=server_tz_from_args(config["server_tz"]),
                                        terminal_path=os.environ.get("MT5_TERMINAL_PATH"),
                                        login=int(login) if login else None,
                                        password=os.environ.get("MT5_DEMO_PASSWORD"),
                                        server=os.environ.get("MT5_DEMO_SERVER")))


def cmd_run(args) -> dict:
    config, prereg = load_config(Path(args.config))
    runner = Runner(config, prereg, _mt5_feed(config), Path(args.state_dir or config["state_dir"]))
    return runner.run(once=args.once, max_minutes=args.max_minutes)


def cmd_commission_evidence(args) -> dict:
    """Read-only: is the XAUUSD commission verifiable from this demo account's own deal history?"""
    from trading.connectors.mt5 import commission_evidence
    config, _ = load_config(Path(args.config))
    feed = _mt5_feed(config)
    feed.connect()
    try:
        evidence = commission_evidence(feed.conn, feed.name)
    finally:
        feed.shutdown()
    evidence["scenarios_in_use"] = config["commission_scenarios_usd_per_lot_round_trip"]
    evidence["convention"] = config["commission_convention"]
    return evidence


def cmd_report(args) -> dict:
    config, prereg = load_config(Path(args.config))
    store = ShadowStore(Path(args.state_dir or config["state_dir"]) / "shadow.sqlite")
    report = build_report(store, prereg, now=datetime.now(timezone.utc),
                          poll_seconds=config["runner"]["poll_seconds"])
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_jsonable(report), indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")
    return {"output": str(out), "criteria": report["preregistered_criteria"]["verdict"],
            "trades": {b: m["trades"] for b, m in report["books"].items()}}


def cmd_status(args) -> dict:
    config, _ = load_config(Path(args.config))
    state = Path(args.state_dir or config["state_dir"])
    store = ShadowStore(state / "shadow.sqlite")
    breaker = CircuitBreaker(MARKET, kill_file=state / "KILL", state_path=state / "breaker.json")
    return {"bars": store.bar_count(), "live_bars": store.bar_count("live"), "positions": store.positions(),
            "signals": len(store.signals()), "decisions": len(store.decisions()), "trades": len(store.trades()),
            "breaker": breaker.reasons(), "last_events": store.events()[-10:]}


def cmd_reset_breaker(args) -> dict:
    config, _ = load_config(Path(args.config))
    state = Path(args.state_dir or config["state_dir"])
    breaker = CircuitBreaker(MARKET, kill_file=state / "KILL", state_path=state / "breaker.json")
    breaker.reset(operator=args.operator)
    return {"reasons_after_reset": breaker.reasons()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    default_config = "research/configs/phase2d_shadow_xauusd.json"
    p = sub.add_parser("build-calendar")
    p.add_argument("--output", default="research/forward_test/calendar/us_cpi_nfp_bls_2026_2027.json")
    p.add_argument("--provider", default="bls.gov/schedule")
    p.set_defaults(func=cmd_build_calendar)
    for name, func in (("run", cmd_run), ("report", cmd_report), ("status", cmd_status),
                       ("reset-breaker", cmd_reset_breaker), ("commission-evidence", cmd_commission_evidence)):
        p = sub.add_parser(name)
        p.add_argument("--config", default=default_config)
        p.add_argument("--state-dir", default=None)
        p.set_defaults(func=func)
        if name == "run":
            p.add_argument("--once", action="store_true", help="one poll after start-up, then exit")
            p.add_argument("--max-minutes", type=float, default=None)
        if name == "report":
            p.add_argument("--output", default="research/output/phase2d_shadow_report.json")
        if name == "reset-breaker":
            p.add_argument("--operator", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(to_jsonable(args.func(args)), indent=2, default=str))


if __name__ == "__main__":
    main()

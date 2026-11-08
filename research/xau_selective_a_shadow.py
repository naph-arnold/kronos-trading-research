"""XAUUSD Selective Strategy A - independent shadow forward test (read-only MT5; simulation only; no orders).

PREPARED, NOT LAUNCHED. Launching this third runner requires explicit operator approval (see
docs/xau-selective-a-shadow.md, "Launch readiness").

It reuses the hardened Phase 2D runner (heartbeat thread, MT5 read-only allowlist proxy, demo-account check,
broker-verified session calendar, CPI/NFP and FOMC guards, restart-safe store) with:
* its own pre-registration (research/forward_test/xau_selective_a_preregistration.json), config, state
  directory (state/xau_selective_a_shadow), books, daily-loss ledger, breaker and KILL/STOP files;
* closed M5 bars (Strategy A works on 4H -> 1H -> 15M -> 5M, all built causally from M5);
* Strategy A with its frozen research parameters (trading/mtf/shadow_adapter.py) and the research rule that
  the planned reward-to-risk after costs must be >= min_net_rr at the fill tick;
* five random-entry controls (seeds 1-5) on the same bars, exits and cost check.

The Phase 2D module and the shared engine are imported, not modified. Nothing here can place an order: the
MT5 connection is the allowlist proxy and the engine only simulates fills.

Subcommands: run | report | status | reset-breaker  (`run` refuses to start without --i-have-launch-approval)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.phase2d_shadow import MARKET, Mt5Feed, Runner, limits_from, normalized_sha256
from trading.audit import to_jsonable
from trading.breaker import CircuitBreaker
from trading.common import ValidationError
from trading.config import load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.mtf.shadow_adapter import SelectiveAShadow, net_rr_check
from trading.mtf.strategies import SelectiveParams
from trading.shadow.engine import BookSpec, EngineSettings, ShadowEngine
from trading.shadow.report import _blocks, _drawdown, bootstrap_ci, build_report, to_markdown
from trading.shadow.store import ShadowStore
from trading.strategies.base import params_hash
from trading.strategies.baselines import RandomEntryBaseline, RandomEntryParams

DEFAULT_CONFIG = "research/configs/xau_selective_a_shadow.json"
CANDIDATE_KEY = "selective_a"
INTERVAL = "5m"
log = logging.getLogger("research.xau_selective_a_shadow")


def floor5(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment - timedelta(minutes=moment.minute % 5)


# --------------------------------------------------------------------------- configuration and pre-registration

def load_config(path: Path) -> tuple[dict, dict]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    prereg_path = Path(config["preregistration"])
    if normalized_sha256(prereg_path) != config["preregistration_sha256"]:
        raise ValidationError("Strategy A pre-registration changed since the shadow config was written (frozen)")
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    check_preregistration(config, prereg)
    return config, prereg


def check_preregistration(config: dict, prereg: dict) -> None:
    s, cand = config["strategies"], prereg["candidates"][0]
    a = s[CANDIDATE_KEY]
    if (cand["id"], cand["version"]) != (a["prereg_id"], SelectiveAShadow.version):
        raise ValidationError("Candidate id/version differs from the pre-registration")
    if a["params"] != cand["params"] or params_hash(SelectiveParams(**cand["params"])) != cand["params_hash"]:
        raise ValidationError("Strategy A parameters differ from the pre-registration (frozen)")
    if a["max_holding_bars"] != cand["max_holding_bars"] or a["window_days"] != cand["window_days"]:
        raise ValidationError("Strategy A holding period / window differs from the pre-registration")
    rnd = prereg["controls"][0]
    if s["random"]["params"] != rnd["params"] or s["random"]["seeds"] != rnd["seeds"]:
        raise ValidationError("Random-entry control differs from the pre-registration")
    rules = prereg["rules"]
    for key in ("risk_fraction", "daily_loss_halt_fraction", "aggregate_risk_fraction"):
        if config["risk"][key] != rules[key]:
            raise ValidationError(f"{key} differs from the pre-registration")
    if (Decimal(rules["risk_fraction"]), Decimal(rules["daily_loss_halt_fraction"]),
            Decimal(rules["aggregate_risk_fraction"])) != (Decimal("0.005"), Decimal("0.015"), Decimal("0.01")):
        raise ValidationError("Risk limits must stay at 0.5% per trade, 1.5% daily halt, 1% aggregate")
    if config["state_dir"] in ("state/phase2d_shadow", "state/phase2e_binance_shadow"):
        raise ValidationError("Strategy A must use its own state directory")


def strategy_table(config: dict) -> dict[str, tuple[str, dict, int]]:
    s = config["strategies"]
    a = s[CANDIDATE_KEY]
    table = {CANDIDATE_KEY: ("selective_a", a["params"], a["max_holding_bars"])}
    for seed in s["random"]["seeds"]:
        table[f"random_s{seed}"] = ("random", {**s["random"]["params"], "seed": seed}, s["random"]["max_holding_bars"])
    return table


def strategy_factory(config: dict):
    table, tick = strategy_table(config), Decimal("0.01")
    window = config["strategies"][CANDIDATE_KEY]["window_days"]

    def make(key: str):
        kind, params, _ = table[key]
        if kind == "selective_a":
            return SelectiveAShadow(SelectiveParams(**params), tick_size=tick, window_days=window,
                                    dataset_id="mt5-live-shadow-m5")
        return RandomEntryBaseline(RandomEntryParams(**params), tick_size=tick, dataset_id="mt5-live-shadow-m5")
    return make


def build_books(config: dict) -> list[BookSpec]:
    return [BookSpec(f"{key}.e{equity}.c{commission}", key, Decimal(equity), Decimal(commission), holding)
            for key, (_, _, holding) in strategy_table(config).items()
            for equity in config["equities"] for commission in config["commission_scenarios_usd_per_lot_round_trip"]]


def entry_checks(config: dict) -> dict:
    """The frozen research rule (net reward-to-risk after costs at the fill tick) for the candidate AND the
    random controls, so the controls face the same cost filter."""
    check = net_rr_check(config["strategies"][CANDIDATE_KEY]["params"]["min_net_rr"])
    return {key: check for key in strategy_table(config)}


class CheckedShadowEngine(ShadowEngine):
    """ShadowEngine plus per-strategy entry checks that can only REJECT, evaluated before the risk engine (as in
    the research simulator). Approval, sizing and every other control remain `trading.risk.evaluate`'s."""

    checks = None  # set per runner: {strategy_key: check}

    def _decide(self, book, signal, tick, wall_now) -> int:
        if self.store.has_decision(book.book_id, signal.signal_id):
            return 0
        check = (self.checks or {}).get(book.strategy_key)
        if check is not None:
            try:
                reason = check(signal, tick, self.instruments[book.commission], self.limits)
            except Exception as exc:  # noqa: BLE001 - a failing check rejects (fail closed)
                reason = f"Entry check failed: {type(exc).__name__}: {exc}"
            if reason:
                self._record_rejection(book, signal, tick.time, [reason], {
                    "bid": tick.bid, "ask": tick.ask, "tick_time": tick.time, "processed_at": wall_now,
                    "signal_entry": signal.entry_price, "signal_stop": signal.stop_price,
                    "signal_take_profit": signal.take_profit, "commission_per_lot_round_trip": book.commission})
                return 0
        return super()._decide(book, signal, tick, wall_now)


class M5Feed(Mt5Feed):
    def closed_bars(self, start: datetime, end: datetime):
        return [b.bar for b in self.conn.history(self.name, INTERVAL, start, end)]


# --------------------------------------------------------------------------- runner

class SelectiveARunner(Runner):
    """Phase 2D runner with Strategy A's books, M5 bars and entry checks. `start` and `poll` mirror the Phase 2D
    versions (kept byte-identical there) with the 5-minute interval."""

    def start(self) -> dict:
        environment = load_demo_environment()
        now = self.clock()
        stored = self.store.get_meta("preregistration_sha256")
        if stored is None:
            self.store.set_meta("preregistration_sha256", self.config["preregistration_sha256"])
            self.store.set_meta("config_sha256", hashlib.sha256(json.dumps(
                self.config, sort_keys=True).encode()).hexdigest())
            self.store.set_meta("created_at", now.isoformat())
        elif stored != self.config["preregistration_sha256"]:
            raise ValidationError("State was created under a different pre-registration; refusing to continue")
        summary = self.feed.connect()
        baseline = self.feed.account_state()
        self.store.event(now, "mt5_account_baseline", account={k: summary.get(k) for k in (
            "server", "is_demo", "currency", "leverage")}, **baseline)
        self._last["account_baseline"] = baseline
        instruments, spec_meta = self._specification(now)
        self._calendar_service(now)
        calendar = self._provisional_calendar(now)
        settings = EngineSettings(unseen_start=datetime.fromisoformat(self.prereg["unseen_window_starts"]),
                                  max_decision_latency=timedelta(seconds=self.runner_cfg["max_decision_latency_s"]),
                                  market=MARKET, interval=INTERVAL)
        limits = limits_from(self.config)
        daily = DailyLossLedger(self.state_dir / "daily.sqlite", market=MARKET,
                                boundary=DayBoundary.server(calendar.server_tz, calendar.risk_day_shift),
                                halt_fraction=limits.daily_loss_halt_fraction)
        h = self.config["health"]
        health = MarketHealthMonitor(MARKET, max_quote_age=timedelta(seconds=h["max_quote_age_s"]),
                                     max_quote_gap=timedelta(seconds=h["max_quote_gap_s"]),
                                     abnormal_spread_multiple=Decimal(h["abnormal_spread_multiple"]))
        engine_cls = type("SelectiveAEngine", (CheckedShadowEngine,), {"checks": entry_checks(self.config)})
        self.engine = engine_cls(store=self.store, books=build_books(self.config),
                                 strategy_factory=strategy_factory(self.config), instruments=instruments,
                                 spec_retrieved_at=now, limits=limits, environment=environment,
                                 breaker=self.breaker, daily=daily, calendar=calendar,
                                 event_guard=self._event_guard, swap=spec_meta["swap"], settings=settings,
                                 health=health)
        restored = self.engine.restore()
        self.store.event(now, "start", restored=restored, swap=spec_meta["swap_record"],
                         commission_verified=False, books=len(self.engine.books))
        if self.engine.last_bar_close is None:
            warmup = datetime.fromisoformat(self.config["warmup_start"])
            bars = self.feed.closed_bars(warmup, floor5(now))
            self.engine.process(bars, [], now)
            self.store.event(now, "warmup_loaded", bars=len(bars),
                             first=bars[0].open_time.isoformat() if bars else None,
                             last=bars[-1].close_time.isoformat() if bars else None)
        self._refresh_sessions(now)
        return {"restored": restored, "status": self.engine.status()}

    def poll(self) -> dict:
        now = self.clock()
        engine = self.engine
        engine.health.record_mt5_trade_mode(int(self.feed.symbol_info().get("trade_mode", -1)), now)
        start = engine.last_bar_close or datetime.fromisoformat(self.config["warmup_start"])
        bars = self.feed.closed_bars(start, floor5(now))
        cursor_text = self.store.get_meta("tick_cursor")
        cursor = datetime.fromisoformat(cursor_text) if cursor_text else (engine.last_bar_close or now)
        if engine.last_tick is not None:
            cursor = max(cursor, engine.last_tick.time)
        cursor = max(cursor, now - timedelta(hours=self.runner_cfg["max_tick_backfill_hours"]))
        limit = now - timedelta(seconds=self.runner_cfg["tick_settle_seconds"])
        totals = {"bars": 0, "ticks": 0, "deferred_ticks": 0, "signals": 0, "entries": 0, "exits": 0}
        waiting = sorted(bars, key=lambda b: b.open_time)
        while True:
            end = min(cursor + timedelta(minutes=self.runner_cfg["tick_chunk_minutes"]), limit)
            ticks = self.feed.ticks(cursor, end) if end > cursor else []
            release = [b for b in waiting if b.close_time <= max(end, cursor)]
            waiting = waiting[len(release):]
            counts = engine.process(release, ticks, now)
            for k in totals:
                totals[k] += counts[k]
            if counts["deferred_ticks"]:
                break
            cursor = max(cursor, end)
            if cursor >= limit:
                break
        self.store.set_meta("tick_cursor", cursor.isoformat())
        return totals


# --------------------------------------------------------------------------- pre-registered criteria

def selective_a_criteria(store: ShadowStore, books: dict, prereg: dict, *, now: datetime) -> dict:
    """The Strategy A pre-registration's criteria, evaluated exactly as written on the primary book
    (10,000 USD, 7 USD/lot round-trip commission). No verdict before the final checkpoint."""
    ev = prereg["evaluation"]
    primary = ev["primary_book"]
    equity, commission = Decimal(primary["equity"]), Decimal(primary["commission"])

    def book_of(key):
        return next((b for b, i in books.items() if i["strategy_key"] == key and i["initial_equity"] == equity
                     and i["commission"] == commission), None)
    cand = book_of(CANDIDATE_KEY)
    trades = store.trades(cand) if cand else []
    r = [float(t["r_multiple"]) for t in trades]
    random_means = []
    for key in sorted({i["strategy_key"] for i in books.values() if i["strategy_key"].startswith("random_")}):
        rb = book_of(key)
        rr = [float(t["r_multiple"]) for t in store.trades(rb)] if rb else []
        if rr:
            random_means.append(statistics.fmean(rr))
    curve = [v for _, v in store.equity_curve(cand)] if cand else []
    unseen = datetime.fromisoformat(prereg["unseen_window_starts"])
    blocks = _blocks(trades, unseen, now)
    n, last3 = len(trades), blocks[-3:]
    mean_r = statistics.fmean(r) if r else None
    ci = bootstrap_ci(r)
    dd = float(_drawdown([equity] + curve) * 100)
    limits = ev["thresholds"]

    def status(ok, enough):
        return "insufficient_data" if not enough else ("pass" if ok else "fail")
    text = ev["success_criteria_all_required"]
    criteria = [
        {"criterion": text[0], "value": mean_r, "status": status(mean_r is not None and mean_r > 0, n >= 2)},
        {"criterion": text[1], "value": ci, "status": status(ci is not None and ci[0] > limits["ci_lower_gt"],
                                                             ci is not None)},
        {"criterion": text[2], "value": {"candidate": mean_r, "random_means": random_means},
         "status": status(mean_r is not None and len(random_means) == len(prereg["controls"][0]["seeds"])
                          and all(mean_r > m for m in random_means), n >= 2 and len(random_means) >= 1)},
        {"criterion": text[3], "value": last3,
         "status": status(sum(1 for b in last3 if b["net_pnl"] > 0) >= 2, len(last3) == 3)},
        {"criterion": text[4], "value": round(dd, 3),
         "status": "fail" if dd >= limits["max_drawdown_pct_lt"] else ("pass" if n >= 2 else "insufficient_data")},
    ]
    final = n >= limits["final_min_trades"] and now >= unseen + timedelta(days=limits["final_min_days"])
    hard_end = now >= unseen + timedelta(days=limits["hard_end_days"])
    safety_stop = dd >= limits["safety_stop_drawdown_pct"]
    if safety_stop:
        verdict = "retire (safety stop: drawdown)"
    elif final or hard_end:
        if all(c["status"] == "pass" for c in criteria):
            verdict = "pass - eligible for review (no automatic promotion)"
        elif mean_r is not None and mean_r > 0 and all(c["status"] == "pass" for i, c in enumerate(criteria) if i != 1):
            verdict = "inconclusive-positive (CI includes 0): one pre-declared extension allowed, no changes"
        else:
            verdict = "retire (criterion failed)"
    else:
        verdict = "in progress - no conclusion before the final checkpoint"
    return {"candidate": prereg["candidates"][0]["id"], "book": cand, "equity": str(equity),
            "commission_scenario": str(commission), "trades": n,
            "checkpoint_50_trades_reached": n >= 50, "final_checkpoint_reached": final,
            "final_checkpoint_rule": ev["checkpoints"][-1], "criteria": criteria, "verdict": verdict,
            "two_month_blocks": blocks, "interpretation_notes": ev["notes"]}


def cmd_report(args) -> dict:
    config, prereg = load_config(Path(args.config))
    store = ShadowStore(Path(args.state_dir or config["state_dir"]) / "shadow.sqlite")
    now = datetime.now(timezone.utc)
    report = build_report(store, prereg, now=now, poll_seconds=config["runner"]["poll_seconds"])
    report["preregistered_criteria"] = selective_a_criteria(store, store.books(), prereg, now=now)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_jsonable(report), indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")
    return {"output": str(out), "criteria": report["preregistered_criteria"]["verdict"]}


def _mt5_feed(config: dict) -> M5Feed:
    from research.phase2d_shadow import _mt5_feed as phase2d_feed
    return M5Feed(phase2d_feed(config).conn)


def cmd_run(args) -> dict:
    if not args.i_have_launch_approval:
        raise SystemExit("Strategy A shadow runner is prepared but NOT approved for launch. See "
                         "docs/xau-selective-a-shadow.md; pass --i-have-launch-approval only after the operator "
                         "has approved the launch.")
    config, prereg = load_config(Path(args.config))
    runner = SelectiveARunner(config, prereg, _mt5_feed(config), Path(args.state_dir or config["state_dir"]))
    return runner.run(once=args.once, max_minutes=args.max_minutes)


def cmd_status(args) -> dict:
    config, _ = load_config(Path(args.config))
    state = Path(args.state_dir or config["state_dir"])
    if not (state / "shadow.sqlite").exists():
        return {"state": "not started", "state_dir": str(state)}
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
    for name, func in (("run", cmd_run), ("report", cmd_report), ("status", cmd_status),
                       ("reset-breaker", cmd_reset_breaker)):
        p = sub.add_parser(name)
        p.add_argument("--config", default=DEFAULT_CONFIG)
        p.add_argument("--state-dir", default=None)
        p.set_defaults(func=func)
        if name == "run":
            p.add_argument("--i-have-launch-approval", action="store_true")
            p.add_argument("--once", action="store_true")
            p.add_argument("--max-minutes", type=float, default=None)
        if name == "report":
            p.add_argument("--output", default="research/output/xau_selective_a_shadow_report.json")
        if name == "reset-breaker":
            p.add_argument("--operator", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(to_jsonable(args.func(args)), indent=2, default=str))


if __name__ == "__main__":
    main()

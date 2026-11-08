"""Shadow forward-test reporting: per-book metrics, strategy comparison, rejected signals, risk-limit
checks, uptime/data interruptions and the pre-registered success criteria (unchanged).

Only data recorded by the shadow engine is used. With too little data every criterion reports
`insufficient_data`; no verdict is issued before the pre-registered final checkpoint.
"""
from __future__ import annotations

import random
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.shadow.store import ShadowStore

ZERO = Decimal("0")


def _f(value) -> float | None:
    return None if value is None else float(value)


def _drawdown(values: list[Decimal]) -> Decimal:
    peak, worst = None, ZERO
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def bootstrap_ci(values: list[float], *, resamples: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(resamples))
    return round(means[int(0.025 * resamples)], 4), round(means[min(resamples - 1, int(0.975 * resamples))], 4)


def reason_key(reason: str) -> str:
    for prefix, key in (("Minimum size", "minimum_lot"), ("Market session", "market_session"),
                        ("Event blackout", "event_or_market_guard"), ("System not live", "system_not_live"),
                        ("Daily loss", "daily_loss_halt"), ("Circuit breaker", "circuit_breaker_or_kill_file"),
                        ("Spread", "spread_limit"), ("No executable tick", "no_fill_tick"),
                        ("Signal is stale", "stale_signal"), ("Quote is stale", "stale_quote"),
                        ("Maximum concurrent", "position_open"), ("Duplicate", "duplicate"),
                        ("XAUUSD trading-session calendar", "calendar_unverified"),
                        ("Market moved", "price_moved"), ("Margin", "margin")):
        if reason.startswith(prefix):
            return key
    return reason.split(":")[0][:48]


def book_metrics(store: ShadowStore, book_id: str, info: dict) -> dict:
    trades = store.trades(book_id)
    decisions = store.decisions(book_id)
    curve = [v for _, v in store.equity_curve(book_id)]
    initial = info["initial_equity"]
    final = curve[-1] if curve else initial + info["realized"]
    pnl = [Decimal(t["pnl"]) for t in trades]
    r = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    wins, losses = [p for p in pnl if p > 0], [-p for p in pnl if p <= 0]
    rejected = Counter()
    for d in decisions:
        if not d["approved"]:
            rejected.update({reason_key(x) for x in d["reasons"]} or {"unspecified"})
    violations = []
    for t in trades:
        if Decimal(t["planned_risk"]) > Decimal(t["risk_budget"]):
            violations.append({"signal_id": t["signal_id"], "issue": "planned risk above budget"})
        if Decimal(t.get("loss_beyond_planned") or 0) > 0:
            violations.append({"signal_id": t["signal_id"], "issue": "realized loss exceeded planned risk "
                               "(gap/slippage beyond allowance)", "excess": t["loss_beyond_planned"]})
    for d in decisions:
        if d["approved"] and d["context"].get("equity") and d["planned_risk"]:
            if Decimal(d["planned_risk"]) > Decimal(d["context"]["equity"]) * Decimal("0.005"):
                violations.append({"signal_id": d["signal_id"], "issue": "planned risk above 0.5% of equity"})
    halts = sum(1 for d in decisions if any(x.startswith("Daily loss halt") for x in d["reasons"]))
    return {
        "book_id": book_id, "strategy_key": info["strategy_key"], "initial_equity": float(initial),
        "commission_per_lot_round_trip": float(info["commission"]), "commission_verified": False,
        "signals_evaluated": len(decisions), "approved": sum(1 for d in decisions if d["approved"]),
        "rejected": sum(1 for d in decisions if not d["approved"]), "rejection_reasons": dict(rejected),
        "trades": len(trades), "open_position": None,
        "net_pnl": float(sum(pnl, ZERO)), "net_return_pct": float((final / initial - 1) * 100),
        "max_drawdown_pct": float(_drawdown([initial] + curve) * 100),
        "win_rate_pct": round(100 * len(wins) / len(trades), 2) if trades else None,
        "profit_factor": round(float(sum(wins) / sum(losses)), 3) if losses and sum(losses) > 0 else None,
        "mean_r": round(statistics.fmean(r), 4) if r else None,
        "mean_r_ci95": bootstrap_ci(r),
        "mean_pnl": round(float(sum(pnl, ZERO) / len(pnl)), 2) if pnl else None,
        "gross_pnl": float(sum((Decimal(t["gross_pnl"]) for t in trades), ZERO)),
        "costs": {"spread_and_slippage": float(sum((Decimal(t["spread_slippage_cost"]) for t in trades), ZERO)),
                  "commission": float(sum((Decimal(t["commission"]) for t in trades), ZERO)),
                  "swap": float(sum((Decimal(t["swap"]) for t in trades), ZERO))},
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "daily_loss_halt_rejections": halts, "risk_limit_violations": violations,
    }


def _blocks(trades: list[dict], start: datetime, now: datetime) -> list[dict]:
    """Consecutive two-month blocks anchored at the unseen-data start; only complete blocks count."""
    out, block_start = [], start
    while True:
        month = block_start.month + 2
        end = block_start.replace(year=block_start.year + (month - 1) // 12, month=(month - 1) % 12 + 1)
        if end > now:
            break
        net = sum(float(t["pnl"]) for t in trades if block_start.isoformat() <= t["closed_at"] < end.isoformat())
        out.append({"start": block_start.isoformat(), "end": end.isoformat(), "net_pnl": round(net, 2)})
        block_start = end
    return out


def preregistered_criteria(store: ShadowStore, books: dict, prereg: dict, *, now: datetime,
                           equity: str = "10000", commission: str = "7") -> dict:
    """Evaluate the pre-registration's success criteria exactly as written, for the candidate book at the
    stated equity under the 7 USD/lot commission scenario. Returns statuses, never a verdict before the
    final checkpoint."""
    candidate = prereg["candidates"][0]["id"]
    def book_of(key):
        return next((b for b, i in books.items() if i["strategy_key"] == key
                     and i["initial_equity"] == Decimal(equity) and i["commission"] == Decimal(commission)), None)
    cand = book_of("orb_newyork")
    trades = store.trades(cand) if cand else []
    r = [float(t["r_multiple"]) for t in trades]
    random_means = []
    for key in sorted({i["strategy_key"] for i in books.values() if i["strategy_key"].startswith("random_")}):
        rb = book_of(key)
        rr = [float(t["r_multiple"]) for t in store.trades(rb)] if rb else []
        if rr:
            random_means.append(statistics.fmean(rr))
    curve = [v for _, v in store.equity_curve(cand)] if cand else []
    initial = Decimal(equity)
    unseen = datetime.fromisoformat(prereg["unseen_window_starts"])
    blocks = _blocks(trades, unseen, now)
    final_date = datetime(2027, 5, 9, tzinfo=timezone.utc)
    n = len(trades)
    mean_r = statistics.fmean(r) if r else None
    ci = bootstrap_ci(r)
    dd = float(_drawdown([initial] + curve) * 100)
    last3 = blocks[-3:]

    def status(ok, enough):
        return "insufficient_data" if not enough else ("pass" if ok else "fail")
    criteria = [
        {"criterion": prereg["evaluation"]["success_criteria_all_required"][0], "value": mean_r,
         "status": status(mean_r is not None and mean_r > 0, n >= 2)},
        {"criterion": prereg["evaluation"]["success_criteria_all_required"][1], "value": ci,
         "status": status(ci is not None and ci[0] > -0.05, ci is not None)},
        {"criterion": prereg["evaluation"]["success_criteria_all_required"][2],
         "value": {"candidate": mean_r, "random_median": statistics.median(random_means) if random_means else None,
                   "random_books_with_trades": len(random_means)},
         "status": status(mean_r is not None and random_means and mean_r > statistics.median(random_means),
                          n >= 2 and len(random_means) >= 1)},
        {"criterion": prereg["evaluation"]["success_criteria_all_required"][3], "value": last3,
         "status": status(sum(1 for b in last3 if b["net_pnl"] > 0) >= 2, len(last3) == 3)},
        {"criterion": prereg["evaluation"]["success_criteria_all_required"][4], "value": round(dd, 3),
         "status": "fail" if dd >= 6 else ("pass" if n >= 2 else "insufficient_data")},
    ]
    checkpoint_1 = n >= 50
    final = n >= 100 and now >= final_date
    if final:
        verdict = "pass" if all(c["status"] == "pass" for c in criteria) else "retire (criterion failed)"
    else:
        verdict = "in progress - no conclusion before the final checkpoint"
    return {"candidate": candidate, "book": cand, "equity": equity, "commission_scenario": commission,
            "trades": n, "checkpoint_50_trades_reached": checkpoint_1, "final_checkpoint_reached": final,
            "final_checkpoint_rule": prereg["evaluation"]["checkpoints"][1], "criteria": criteria, "verdict": verdict,
            "two_month_blocks": blocks,
            "interpretation_notes": ["Criteria evaluated on the 10,000 USD candidate book under the 7 USD/lot "
                                     "round-trip commission scenario (the stricter scenario named in criterion 1).",
                                     "Two-month blocks are anchored at the pre-registered unseen-data start; the "
                                     "most recent three complete blocks are used."]}


def uptime(store: ShadowStore, *, poll_seconds: float, now: datetime) -> dict:
    beats = [t for t, _ in store.heartbeats()]
    if not beats:
        return {"heartbeats": 0, "monitored_hours": 0.0, "uptime_pct": None, "downtime": []}
    allowed = timedelta(seconds=max(3 * poll_seconds, 120))
    gaps = [(a, b) for a, b in zip(beats, beats[1:]) if b - a > allowed]
    span = (beats[-1] - beats[0]).total_seconds()
    down = sum((b - a).total_seconds() for a, b in gaps)
    return {"heartbeats": len(beats), "first": beats[0].isoformat(), "last": beats[-1].isoformat(),
            "monitored_hours": round(span / 3600, 2),
            "uptime_pct": round(100 * (1 - down / span), 3) if span > 0 else None,
            "downtime": [{"from": a.isoformat(), "to": b.isoformat(), "minutes": round((b - a).total_seconds() / 60, 1)}
                         for a, b in gaps],
            "last_heartbeat_age_minutes": round((now - beats[-1]).total_seconds() / 60, 1)}


def build_report(store: ShadowStore, prereg: dict, *, now: datetime, poll_seconds: float) -> dict:
    books = store.books()
    per_book = {b: book_metrics(store, b, info) for b, info in sorted(books.items())}
    for book_id, body in store.positions().items():
        per_book[book_id]["open_position"] = {k: body[k] for k in ("signal_id", "side", "size", "entry", "stop",
                                                                    "take_profit", "opened_at")}
    events = store.events()
    interruptions = Counter(e["kind"] for e in events if e["kind"] in (
        "data_interruption", "bar_without_ticks", "mt5_error", "reconnect", "calendar_refresh_failed"))
    signals = store.signals()
    return {
        "generated_at": now.isoformat(), "simulation_only": True, "orders_placed": 0,
        "unseen_window_starts": prereg["unseen_window_starts"],
        "signals": dict(Counter(s["status"] for s in signals)),
        "live_signals_by_strategy": dict(Counter(s["strategy_key"] for s in signals if s["status"] != "warmup")),
        "books": per_book,
        "comparison": [{k: m[k] for k in ("book_id", "trades", "net_return_pct", "max_drawdown_pct", "win_rate_pct",
                                          "profit_factor", "mean_r", "rejected")} for m in per_book.values()],
        "preregistered_criteria": preregistered_criteria(store, books, prereg, now=now),
        "uptime": uptime(store, poll_seconds=poll_seconds, now=now),
        "data_interruptions": dict(interruptions),
        "breaker_events": [e for e in events if e["kind"] in ("breaker_trip", "kill_file", "reconciliation")],
        "mt5_account_checks": [e for e in events if e["kind"] == "mt5_account_check"][-5:],
        "notes": ["Simulated fills against live MetaQuotes-Demo ticks; no orders were sent.",
                  "Commission scenarios (0 and 7 USD per 1.00 lot round trip) are assumptions; the broker's "
                  "commission is unverified.",
                  "Bars closing before the unseen-data start are indicator warm-up only and never traded."],
    }


def to_markdown(report: dict) -> str:
    lines = [f"# Phase 2D shadow forward test - report {report['generated_at']}", "",
             "Simulation only (no orders). Commission scenarios are assumptions (unverified).", "",
             f"Unseen data from {report['unseen_window_starts']}. Signals by status: {report['signals']}", "",
             "| Book | Trades | Net % | Max DD % | Win % | PF | Mean R | Rejected | Costs (spread+slip / comm / swap) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in report["books"].values():
        c = m["costs"]
        lines.append(f"| {m['book_id']} | {m['trades']} | {m['net_return_pct']:.2f} | {m['max_drawdown_pct']:.2f} | "
                     f"{m['win_rate_pct']} | {m['profit_factor']} | {m['mean_r']} | {m['rejected']} | "
                     f"{c['spread_and_slippage']:.2f} / {c['commission']:.2f} / {c['swap']:.2f} |")
    p = report["preregistered_criteria"]
    lines += ["", f"## Pre-registered criteria ({p['book']}, {p['trades']} trades)", "", f"Verdict: **{p['verdict']}**",
              ""]
    lines += [f"- {c['status']}: {c['criterion']} (value: {c['value']})" for c in p["criteria"]]
    u = report["uptime"]
    lines += ["", "## Operations", "", f"- Uptime: {u.get('uptime_pct')}% over {u.get('monitored_hours')} h "
              f"({len(u.get('downtime', []))} gaps)", f"- Data interruptions: {report['data_interruptions']}",
              f"- Breaker/kill events: {len(report['breaker_events'])}"]
    rejected = Counter()
    for m in report["books"].values():
        rejected.update(m["rejection_reasons"])
    lines += [f"- Rejection reasons (all books): {dict(rejected)}", ""]
    return "\n".join(lines)


def candidate_criteria(store: ShadowStore, books: dict, *, candidate_key: str, control_keys: list[str],
                       criteria_text: list[str], unseen_start: datetime, final_date: datetime,
                       min_final_trades: int, equity: Decimal, cost_key: Decimal, now: datetime,
                       max_drawdown_pct: float = 6.0) -> dict:
    """Market-agnostic version of the pre-registered criteria (Phase 2E): (1) net mean R > 0, (2) bootstrap 95% CI
    lower bound > -0.05, (3) mean R above the median of the candidate's own random controls, (4) positive net result
    in >= 2 of the latest 3 complete two-month blocks, (5) max drawdown below the limit. No verdict before the final
    checkpoint (>= min_final_trades and >= final_date)."""
    def book_of(key):
        return next((b for b, i in books.items() if i["strategy_key"] == key and i["initial_equity"] == equity
                     and i["commission"] == cost_key), None)
    cand = book_of(candidate_key)
    trades = store.trades(cand) if cand else []
    r = [float(t["r_multiple"]) for t in trades]
    control_means = []
    for key in control_keys:
        cb = book_of(key)
        cr = [float(t["r_multiple"]) for t in store.trades(cb)] if cb else []
        if cr:
            control_means.append(statistics.fmean(cr))
    curve = [v for _, v in store.equity_curve(cand)] if cand else []
    blocks = _blocks(trades, unseen_start, now)
    last3, n = blocks[-3:], len(trades)
    mean_r, ci = (statistics.fmean(r) if r else None), bootstrap_ci(r)
    dd = float(_drawdown([equity] + curve) * 100)
    median_ctrl = statistics.median(control_means) if control_means else None

    def status(ok, enough):
        return "insufficient_data" if not enough else ("pass" if ok else "fail")
    criteria = [
        {"criterion": criteria_text[0], "value": mean_r, "status": status(mean_r is not None and mean_r > 0, n >= 2)},
        {"criterion": criteria_text[1], "value": ci, "status": status(ci is not None and ci[0] > -0.05, ci is not None)},
        {"criterion": criteria_text[2], "value": {"candidate": mean_r, "control_median": median_ctrl,
                                                  "controls_with_trades": len(control_means)},
         "status": status(mean_r is not None and median_ctrl is not None and mean_r > median_ctrl,
                          n >= 2 and median_ctrl is not None)},
        {"criterion": criteria_text[3], "value": last3,
         "status": status(sum(1 for b in last3 if b["net_pnl"] > 0) >= 2, len(last3) == 3)},
        {"criterion": criteria_text[4], "value": round(dd, 3),
         "status": "fail" if dd >= max_drawdown_pct else ("pass" if n >= 2 else "insufficient_data")},
    ]
    final = n >= min_final_trades and now >= final_date
    verdict = (("pass" if all(c["status"] == "pass" for c in criteria) else "retire (criterion failed)") if final
               else "in progress - no conclusion before the final checkpoint")
    return {"candidate": candidate_key, "book": cand, "trades": n, "final_checkpoint_reached": final,
            "final_checkpoint": {"min_trades": min_final_trades, "not_before": final_date.isoformat()},
            "criteria": criteria, "verdict": verdict, "two_month_blocks": blocks}

"""Unified READ-ONLY daily health report for the XAUUSD (MT5) and BTC/USDT (Binance) shadow runners.

Opens each runner's SQLite state with `mode=ro` and never writes to a state directory. Reports, per market and
for the chosen window (default: the last 24 h): runner uptime from heartbeats, disconnections, data gaps, recorded
signals (warm-up vs forward-test), simulated decisions and trades, account performance per hypothetical book,
safety alerts (breaker, KILL/STOP, reconciliation, risk exceptions, stale heartbeats, calendar failures) and
integrity checks (pre-registration hash: file vs config vs state). Both markets stay independent: one market's
problems never change the other's figures.

    python -m research.dual_health_report [--hours 24] [--output-dir research/output/health]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

GOLD_REPO = Path("C:/Users/Admin/Projects/kronos-trading-research")
CRYPTO_REPO = Path("C:/Users/Admin/Projects/kronos-binance-shadow")
SELECTIVE_A_REPO = Path("C:/Users/Admin/Projects/kronos-selective-a-shadow")  # dedicated checkout, created at launch
DEFAULT_MARKETS = {
    "xauusd": {"state": GOLD_REPO / "state/phase2d_shadow", "repo": GOLD_REPO,
               "config": "research/configs/phase2d_shadow_xauusd.json"},
    "btcusdt": {"state": CRYPTO_REPO / "state/phase2e_binance_shadow", "repo": CRYPTO_REPO,
                "config": "research/configs/phase2e_binance_shadow.json"},
    # Optional third runner (XAUUSD Selective Strategy A): reported as "not started" until its state exists.
    "xauusd_selective_a": {"state": SELECTIVE_A_REPO / "state/xau_selective_a_shadow", "repo": SELECTIVE_A_REPO,
                           "config": "research/configs/xau_selective_a_shadow.json", "optional": True},
}
DISCONNECT_EVENTS = ("data_interruption", "mt5_error", "start_failed", "reconnect", "quote_source", "clock_offset")
GAP_EVENTS = ("bar_without_ticks", "kline_gap", "kronos_unavailable")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _normalized_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def _heartbeat_view(detail: dict) -> dict:
    """Normalize XAUUSD (runner_status/mt5_connection/market_data) and BTC (status/data/stream) heartbeats."""
    if "runner_status" in detail:
        connected = detail.get("mt5_connection") == "connected"
        return {"format": "xauusd", "connected": connected, "fresh": detail.get("market_data") == "fresh",
                "market_open": detail.get("market_open"), "entries_blocked": detail.get("new_entries") == "blocked"}
    if "status" in detail:
        stream = detail.get("stream") or {}
        quotes = (detail.get("data") or {}).get("quotes", "")
        connected = bool(stream.get("connected")) or quotes in ("websocket", "rest_fallback")
        return {"format": "btcusdt", "connected": connected, "fresh": detail.get("status") == "ok",
                "market_open": True, "entries_blocked": detail.get("status") != "ok", "quotes": quotes,
                "reconnects": stream.get("reconnects")}
    return {"format": "legacy", "connected": None, "fresh": None, "market_open": None, "entries_blocked": None}


def market_health(name: str, state: Path, repo: Path, config_rel: str, *, now: datetime, hours: float,
                  optional: bool = False) -> dict:
    state, start = Path(state), now - timedelta(hours=hours)
    out: dict = {"market": name, "state_dir": str(state), "window": [_iso(start), _iso(now)], "alerts": []}
    db_path = state / "shadow.sqlite"
    if not db_path.exists() and optional:
        out["status"] = "not started"
        return out
    if not db_path.exists():
        out["alerts"].append("state database missing")
        return out
    db = _ro(db_path)
    try:
        q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
        # -- heartbeats / uptime ----------------------------------------------------------------------------
        beats = [(datetime.fromisoformat(a), json.loads(d)) for a, d in
                 q("SELECT at, detail FROM heartbeats WHERE at >= ? ORDER BY at", _iso(start))]
        all_last = q("SELECT MAX(at) FROM heartbeats")[0][0]
        last = datetime.fromisoformat(all_last) if all_last else None
        times = [b[0] for b in beats]
        edges = ([start] if times else []) + times + ([now] if times else [])
        gaps = [(a, b) for a, b in zip(edges, edges[1:]) if (b - a).total_seconds() > 180]
        down = sum((b - a).total_seconds() for a, b in gaps) if times else hours * 3600
        views = [_heartbeat_view(d) for _, d in beats]
        known = [v for v in views if v["connected"] is not None]
        open_known = [v for v in known if v["market_open"]]
        out["uptime"] = {
            "heartbeats": len(beats), "expected_approx": int(hours * 60),
            "runner_uptime_pct": round(100 * (1 - down / (hours * 3600)), 2),
            "gaps_over_3_min": [{"from": _iso(a), "to": _iso(b), "minutes": round((b - a).total_seconds() / 60, 1)}
                                for a, b in gaps][:20],
            "last_heartbeat": None if last is None else _iso(last),
            "last_heartbeat_age_min": None if last is None else round((now - last).total_seconds() / 60, 1),
            "heartbeats_disconnected": sum(1 for v in known if not v["connected"]),
            "fresh_data_pct_while_open": round(100 * sum(1 for v in open_known if v["fresh"]) / len(open_known), 2)
            if open_known else None,
            "heartbeats_with_entries_blocked": sum(1 for v in known if v["entries_blocked"]),
        }
        if last is None or now - last > timedelta(minutes=5):
            out["alerts"].append(f"no heartbeat for {out['uptime']['last_heartbeat_age_min']} min (runner down?)")
        # -- disconnections and data gaps -------------------------------------------------------------------
        events = [(a, k, json.loads(d)) for a, k, d in
                  q("SELECT at, kind, detail FROM events WHERE at >= ? ORDER BY id", _iso(start))]
        counts = Counter(k for _, k, _ in events)
        out["disconnections"] = {k: counts[k] for k in DISCONNECT_EVENTS if counts[k]}
        out["data_gaps"] = {k: counts[k] for k in GAP_EVENTS if counts[k]}
        out["data_gaps"]["kline_gaps"] = [d for _, k, d in events if k == "kline_gap"][:10]
        reconnects = [v.get("reconnects") for v in views if v.get("reconnects") is not None]
        if reconnects:
            out["disconnections"]["websocket_reconnects_in_window"] = max(reconnects) - min(reconnects)
        # -- signals, decisions, trades --------------------------------------------------------------------
        out["signals"] = {
            "in_window_by_status": dict(q("SELECT status, COUNT(*) FROM signals WHERE bar_close >= ? GROUP BY status",
                                          _iso(start))),
            "total_by_status": dict(q("SELECT status, COUNT(*) FROM signals GROUP BY status")),
        }
        decisions = q("SELECT approved, reasons FROM decisions WHERE evaluated_at >= ?", _iso(start))
        reasons = Counter()
        for approved, text in decisions:
            if not approved:
                reasons.update({r.split(":")[0][:60] for r in json.loads(text)})
        out["decisions"] = {"in_window": len(decisions), "approved": sum(1 for a, _ in decisions if a),
                            "rejected": sum(1 for a, _ in decisions if not a),
                            "rejection_reasons": dict(reasons.most_common(10))}
        trades_window = q("SELECT book_id, body FROM trades WHERE closed_at >= ?", _iso(start))
        out["trades"] = {"closed_in_window": len(trades_window),
                         "closed_total": q("SELECT COUNT(*) FROM trades")[0][0],
                         "open_positions": q("SELECT COUNT(*) FROM positions")[0][0]}
        # -- account performance per book ------------------------------------------------------------------
        books = {}
        for book_id, key, initial, commission, realized in q(
                "SELECT book_id, strategy_key, initial_equity, commission, realized FROM books ORDER BY book_id"):
            n = q("SELECT COUNT(*) FROM trades WHERE book_id=?", book_id)[0][0]
            mark = q("SELECT equity FROM equity WHERE book_id=? ORDER BY at DESC LIMIT 1", book_id)
            equity = Decimal(mark[0][0]) if mark else Decimal(initial) + Decimal(realized)
            books[book_id] = {"strategy": key, "cost_scenario": commission, "trades": n,
                              "realized": float(Decimal(realized)), "equity": float(equity),
                              "return_pct": round(float((equity / Decimal(initial) - 1) * 100), 4)}
        out["books"] = books
        # -- safety alerts ----------------------------------------------------------------------------------
        for _, body in trades_window:
            t = json.loads(body)
            if Decimal(str(t.get("loss_beyond_planned") or 0)) > 0:
                out["alerts"].append(f"risk exception: {t['signal_id']} lost {t['loss_beyond_planned']} beyond plan")
        for at, k, d in events:
            if k == "reconciliation" and not d.get("ok"):
                out["alerts"].append(f"reconciliation failed at {at}: {d.get('problems', [])[:1]}")
            if k in ("calendar_refresh_failed", "kill_file", "stop_file", "housekeeping_error", "clock_offset"):
                out["alerts"].append(f"{k} at {at}")
        breaker = state / "breaker.json"
        if breaker.exists():
            try:
                trips = json.loads(breaker.read_text(encoding="utf-8")).get("trips", [])
                if trips:
                    out["alerts"].append(f"circuit breaker tripped: {[t.get('reason') for t in trips][:3]}")
            except (OSError, ValueError):
                out["alerts"].append("breaker state unreadable (fail closed)")
        for flag in ("KILL", "STOP"):
            if (state / flag).exists():
                out["alerts"].append(f"{flag} file present")
        # -- integrity ------------------------------------------------------------------------------------
        meta = dict(q("SELECT key, value FROM meta"))
        integrity = {"state_preregistration_sha256": meta.get("preregistration_sha256")}
        try:
            config = json.loads((Path(repo) / config_rel).read_text(encoding="utf-8"))
            integrity["config_preregistration_sha256"] = config.get("preregistration_sha256")
            integrity["file_preregistration_sha256"] = _normalized_sha256(Path(repo) / config["preregistration"])
            prereg = json.loads((Path(repo) / config["preregistration"]).read_text(encoding="utf-8"))
            unseen = datetime.fromisoformat(prereg["unseen_window_starts"])
            integrity["unseen_window_starts"] = _iso(unseen)
            early = q("SELECT COUNT(*) FROM signals WHERE status != 'warmup' AND bar_close < ?", _iso(unseen))[0][0]
            integrity["non_warmup_signals_before_unseen_start"] = early
            if early:
                out["alerts"].append(f"{early} non-warm-up signals before the unseen-data start")
            live = q("SELECT COUNT(*), MIN(open_time) FROM bars WHERE source='live'")[0]
            integrity["live_bars"], integrity["first_live_bar"] = live[0], live[1]
        except (OSError, ValueError, KeyError) as exc:
            integrity["error"] = f"{type(exc).__name__}: {exc}"
        hashes = {integrity.get(k) for k in ("state_preregistration_sha256", "config_preregistration_sha256",
                                             "file_preregistration_sha256")}
        integrity["preregistration_consistent"] = len(hashes) == 1 and None not in hashes
        if not integrity["preregistration_consistent"]:
            out["alerts"].append("pre-registration hash mismatch between file, config and state")
        out["integrity"] = integrity
    finally:
        db.close()
    return out


def build(markets: dict, *, now: datetime, hours: float) -> dict:
    report = {"generated_at": _iso(now), "window_hours": hours, "read_only": True, "orders_possible": False,
              "markets": {name: market_health(name, m["state"], m["repo"], m["config"], now=now, hours=hours,
                                             optional=m.get("optional", False))
                          for name, m in markets.items()}}
    report["alerts_total"] = sum(len(m["alerts"]) for m in report["markets"].values())
    return report


def to_markdown(report: dict) -> str:
    lines = [f"# Kronos daily health - {report['generated_at']}", "",
             f"Read-only. Window: last {report['window_hours']} h. Alerts: {report['alerts_total']}.", ""]
    for name, m in report["markets"].items():
        u = m.get("uptime", {})
        if m.get("status") == "not started":
            lines += [f"## {name.upper()}", "", "- Not started (optional runner; no state directory yet)", ""]
            continue
        lines += [f"## {name.upper()}", "",
                  f"- Runner uptime {u.get('runner_uptime_pct')}% ({u.get('heartbeats')} heartbeats, last "
                  f"{u.get('last_heartbeat_age_min')} min ago); gaps > 3 min: {len(u.get('gaps_over_3_min', []))}",
                  f"- Disconnected heartbeats {u.get('heartbeats_disconnected')}; fresh data while open "
                  f"{u.get('fresh_data_pct_while_open')}%; disconnection events {m.get('disconnections')}",
                  f"- Data gaps {({k: v for k, v in m.get('data_gaps', {}).items() if k != 'kline_gaps'})}",
                  f"- Signals in window {m.get('signals', {}).get('in_window_by_status')}; decisions "
                  f"{m.get('decisions', {}).get('in_window')} (approved {m.get('decisions', {}).get('approved')}); "
                  f"trades closed {m.get('trades', {}).get('closed_in_window')} (total "
                  f"{m.get('trades', {}).get('closed_total')}, open {m.get('trades', {}).get('open_positions')})",
                  f"- Integrity: pre-registration consistent = {m.get('integrity', {}).get('preregistration_consistent')}, "
                  f"live bars {m.get('integrity', {}).get('live_bars')}",
                  "- Alerts: " + ("; ".join(m["alerts"]) if m["alerts"] else "none"), "",
                  "| Book | Trades | Realized | Equity | Return % |", "| --- | --- | --- | --- | --- |"]
        for book, b in m.get("books", {}).items():
            lines.append(f"| {book} | {b['trades']} | {b['realized']:.2f} | {b['equity']:.2f} | {b['return_pct']} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--output-dir", type=Path, default=GOLD_REPO / "research/output/health")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    report = build(DEFAULT_MARKETS, now=now, hours=args.hours)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"health_{now:%Y%m%dT%H%MZ}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))


if __name__ == "__main__":
    main()

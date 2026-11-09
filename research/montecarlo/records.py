"""Verified trade records and datasets.

Only *closed* trades with a recorded net outcome enter a dataset. Warm-up signals, candidate signals, rejected
decisions and open positions are never trades. Each dataset carries one market, one strategy, one data partition
and one simulated book (equity size x cost scenario), so the same signal is never counted twice.

Partitions (never mixed in one dataset):
  development        - data used to design or select the strategy
  historical_holdout - held-out history; NOTE: may already have been examined before a pre-registration
  forward            - prospective records after a pre-registered forward boundary
  synthetic_fixture  - software tests only; refused by the research CLI
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PARTITIONS = ("development", "historical_holdout", "forward", "synthetic_fixture")
MARKETS = ("xauusd", "btcusdt")
UTC = timezone.utc


class RecordError(ValueError):
    """A trade record or dataset failed validation (the dataset is refused, never repaired)."""


def _dt(v) -> datetime:
    t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise RecordError(f"timestamp without time zone: {v}")
    return t.astimezone(UTC)


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str           # unique within the dataset (signal id; one book per dataset)
    signal_id: str
    side: str               # long | short
    opened_at: str          # ISO UTC
    closed_at: str
    entry: float
    exit: float
    size: float             # lots (XAUUSD) or base units (BTC)
    net_pnl: float          # after every recorded cost
    gross_pnl: float | None
    costs: float | None     # recorded spread/slippage/commission/swap total
    planned_risk: float     # intended loss at the stop, in account currency
    r_net: float            # net_pnl / planned_risk (as recorded)
    exit_reason: str | None = None
    regime: str | None = None   # causal label at signal time, only when derived from verified data
    session: str | None = None  # derived from the open time (UTC hour bands), descriptive only


@dataclass
class TradeDataset:
    market: str
    strategy: str
    partition: str
    book: dict                       # {"initial_equity", "commission_per_lot", "label"}
    provenance: dict                 # source file, sha256, config/dataset hashes, period, notes
    cost_model: dict                 # how recorded costs were produced (assumptions stated as such)
    trades: list[TradeRecord] = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    regime_source: dict | None = None
    selection: dict = field(default_factory=dict)  # how many strategies/configurations the source compared

    def fingerprint(self) -> str:
        body = {"market": self.market, "strategy": self.strategy, "partition": self.partition, "book": self.book,
                "trades": [asdict(t) for t in self.trades]}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def to_json(self) -> dict:
        return {"market": self.market, "strategy": self.strategy, "partition": self.partition, "book": self.book,
                "provenance": self.provenance, "cost_model": self.cost_model, "validation": self.validation,
                "regime_source": self.regime_source, "selection": self.selection,
                "fingerprint": self.fingerprint(), "trades": [asdict(t) for t in self.trades]}

    @classmethod
    def from_json(cls, d: dict) -> "TradeDataset":
        ds = cls(market=d["market"], strategy=d["strategy"], partition=d["partition"], book=d["book"],
                 provenance=d["provenance"], cost_model=d["cost_model"],
                 trades=[TradeRecord(**t) for t in d["trades"]], validation=d.get("validation", {}),
                 regime_source=d.get("regime_source"), selection=d.get("selection", {}))
        if d.get("fingerprint") and d["fingerprint"] != ds.fingerprint():
            raise RecordError("dataset fingerprint mismatch (file edited after extraction)")
        return ds


def session_label(opened_at: str) -> str:
    """Descriptive UTC session band of the entry (not used for any decision)."""
    h = _dt(opened_at).hour
    return "asia" if h < 7 else "london" if h < 12 else "new_york" if h < 21 else "late"


def validate(ds: TradeDataset, *, period: tuple[str, str] | None = None, r_tolerance: float = 1e-6) -> dict:
    """Refuse anything that is not a clean set of closed, net-of-cost trades from one partition and one book."""
    if ds.market not in MARKETS:
        raise RecordError(f"unknown market {ds.market!r}")
    if ds.partition not in PARTITIONS:
        raise RecordError(f"unknown partition {ds.partition!r}")
    seen_ids, seen_signals = set(), set()
    problems = []
    for t in ds.trades:
        if t.trade_id in seen_ids or t.signal_id in seen_signals:
            raise RecordError(f"duplicate trade/signal {t.signal_id} (same signal counted twice)")
        seen_ids.add(t.trade_id)
        seen_signals.add(t.signal_id)
        if t.side not in ("long", "short"):
            raise RecordError(f"{t.trade_id}: unknown side {t.side!r}")
        o, c = _dt(t.opened_at), _dt(t.closed_at)
        if c < o:
            raise RecordError(f"{t.trade_id}: closed before it opened")
        if not t.planned_risk or t.planned_risk <= 0:
            raise RecordError(f"{t.trade_id}: missing initial risk")
        if abs(t.net_pnl / t.planned_risk - t.r_net) > max(r_tolerance, 1e-6 * abs(t.r_net)):
            raise RecordError(f"{t.trade_id}: R-multiple inconsistent with net P&L / planned risk")
        if t.gross_pnl is not None and t.costs is not None and abs(t.gross_pnl - t.costs - t.net_pnl) > 1e-6 * max(1, abs(t.gross_pnl)):
            raise RecordError(f"{t.trade_id}: gross - costs != net")
        if period is not None and not (_dt(period[0]) <= o and c <= _dt(period[1])):
            raise RecordError(f"{t.trade_id}: outside the declared {ds.partition} period {period}")
    order = [t.closed_at for t in ds.trades]
    if order != sorted(order):
        problems.append("trades were not in closing order; sorted")
        ds.trades.sort(key=lambda t: (t.closed_at, t.trade_id))
    out = {"status": "verified", "trades": len(ds.trades), "checks": [
        "closed trades only", "one book (no duplicate signals)", "net = gross - costs", "R = net / planned risk",
        "inside the declared partition period" if period else "partition period not declared"], "notes": problems}
    ds.validation = out
    return out


# --------------------------------------------------------------------------- loaders

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def from_phase2_report(path: Path, market: str, strategy: str, *, partition: str = "historical_holdout",
                       examined_note: str | None = None, initial_equity=None, commission=None,
                       config_source: str | None = None) -> TradeDataset:
    """Holdout trade ledger of a Phase 2 research report (one book: the report's initial equity and first
    commission scenario). Provenance includes the report's own config and dataset hashes."""
    rep = json.loads(Path(path).read_text(encoding="utf-8"))
    s = rep["strategies"][strategy]
    hold = s.get("holdout") or {}
    trades = []
    for t in s.get("holdout_trades", []):
        net, risk = float(Decimal(t["pnl"])), float(Decimal(t["planned_risk"]))
        trades.append(TradeRecord(
            trade_id=t["signal_id"], signal_id=t["signal_id"], side=t["side"], opened_at=t["opened_at"],
            closed_at=t["closed_at"], entry=float(t["entry"]), exit=float(t["exit"]), size=float(t["size"]),
            net_pnl=net, gross_pnl=float(t["gross_pnl"]) if t.get("gross_pnl") is not None else None,
            costs=float(t["costs"]) if t.get("costs") is not None else None, planned_risk=risk,
            r_net=float(Decimal(t["r_multiple"])), exit_reason=t.get("exit_reason"),
            session=session_label(t["opened_at"])))
    dataset_sha = rep.get("dataset_sha256") or (rep.get("dataset") or {}).get("sha256")
    equity = rep.get("initial_equity") or initial_equity
    commission = (rep.get("commission_scenarios") or [None])[0] if commission is None else commission
    if equity is None:
        raise RecordError("initial equity of the report book is unknown")
    ds = TradeDataset(
        market=market, strategy=strategy, partition=partition,
        book={"initial_equity": float(equity) if equity else None, "commission_per_lot": commission,
              "label": f"report book (equity {equity}, commission {commission})"},
        provenance={"source": "phase2 research report (holdout_trades)", "file": Path(path).name,
                    "file_sha256": _sha256(Path(path)), "config_sha256": rep.get("config_sha256"),
                    "dataset_sha256": dataset_sha, "git_commit": rep.get("git_commit"),
                    "holdout_period": rep.get("holdout_period"), "strategy_holdout_span": [hold.get("start"), hold.get("end")],
                    "selected_params": s.get("selected_params"), "classification": s.get("classification"),
                    "report_holdout_summary": {k: hold.get(k) for k in ("trades", "net_return_pct", "max_drawdown_pct",
                                                                         "profit_factor", "win_rate_pct")},
                    "examined": examined_note, "book_parameters_from": config_source or "report"},
        cost_model={"recorded": "spread from the dataset's recorded bar spreads, slippage allowance and commission "
                                "scenario as configured in the source report; swap where modelled",
                    "status": "modelled costs (not broker fills)", "commission_scenario": commission},
        trades=trades,
        selection={"strategies_in_report": len(rep["strategies"]), "configurations_tried": s.get("configurations"),
                   "selection_method": "walk-forward selection on training windows; holdout evaluated once"})
    if len(trades) != hold.get("trades"):
        raise RecordError(f"{strategy}: {len(trades)} ledger trades but the report summary says {hold.get('trades')}")
    validate(ds, period=(hold["start"], hold["end"]) if hold.get("start") else None)
    return ds


def from_shadow_store(db_path: Path, market: str, strategy_key: str, book_id: str, *, forward_start: str) -> TradeDataset:
    """Closed forward-test trades of ONE book from a runner store (read-only). Trades whose signal predates the
    pre-registered forward boundary are refused (they would be warm-up)."""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        book = con.execute("SELECT strategy_key, initial_equity, commission FROM books WHERE book_id=?", (book_id,)).fetchone()
        if book is None or book[0] != strategy_key:
            raise RecordError(f"book {book_id} not found for {strategy_key}")
        rows = con.execute("SELECT signal_id, body FROM trades WHERE book_id=? ORDER BY closed_at", (book_id,)).fetchall()
        statuses = dict(con.execute("SELECT signal_id, status FROM signals WHERE strategy_key=?", (strategy_key,)).fetchall())
    finally:
        con.close()
    trades = []
    for sid, body in rows:
        t = json.loads(body)
        if statuses.get(sid) == "warmup" or _dt(t["opened_at"]) < _dt(forward_start):
            raise RecordError(f"{sid}: warm-up / pre-boundary signal cannot be a forward trade")
        costs = sum(float(t.get(k) or 0) for k in ("spread_slippage_cost", "commission", "swap"))
        trades.append(TradeRecord(
            trade_id=sid, signal_id=sid, side=t["side"], opened_at=t["opened_at"], closed_at=t["closed_at"],
            entry=float(t["entry"]), exit=float(t["exit"]), size=float(t["size"]), net_pnl=float(t["pnl"]),
            gross_pnl=float(t["gross_pnl"]) if t.get("gross_pnl") is not None else None, costs=costs,
            planned_risk=float(t["planned_risk"]), r_net=float(Decimal(str(t["pnl"])) / Decimal(str(t["planned_risk"]))),
            exit_reason=t.get("exit_reason"), session=session_label(t["opened_at"])))
    ds = TradeDataset(
        market=market, strategy=strategy_key, partition="forward",
        book={"initial_equity": float(book[1]), "commission_per_lot": book[2], "label": book_id},
        provenance={"source": "shadow runner store (read-only)", "file": Path(db_path).name, "book": book_id,
                    "forward_start": forward_start},
        cost_model={"recorded": "live spread at the fill tick + slippage allowance + commission scenario + swap",
                    "status": "simulated fills on live quotes (no order placed)"},
        trades=trades, selection={"strategies_in_report": 1, "configurations_tried": 1,
                                  "selection_method": "pre-registered, frozen before the forward boundary"})
    validate(ds, period=(forward_start, datetime.now(UTC).isoformat()))
    return ds


def synthetic_fixture(r_values: list[float], *, start: str = "2030-01-07T10:00:00+00:00", hours_apart: float = 30,
                      risk: float = 50.0, regimes: list[str] | None = None, market: str = "xauusd") -> TradeDataset:
    """Synthetic trades for SOFTWARE TESTS ONLY (partition 'synthetic_fixture'; the research CLI refuses it)."""
    from datetime import timedelta
    t0 = _dt(start)
    trades = []
    for i, r in enumerate(r_values):
        o = t0 + timedelta(hours=hours_apart * i)
        net = round(r * risk, 10)
        trades.append(TradeRecord(trade_id=f"fx{i}", signal_id=f"fx{i}", side="long", opened_at=o.isoformat(),
                                  closed_at=(o + timedelta(hours=2)).isoformat(), entry=100.0, exit=100.0 + r,
                                  size=1.0, net_pnl=net, gross_pnl=net + 1.0, costs=1.0, planned_risk=risk,
                                  r_net=net / risk, regime=None if regimes is None else regimes[i],
                                  session=session_label(o.isoformat())))
    ds = TradeDataset(market=market, strategy="fixture", partition="synthetic_fixture",
                      book={"initial_equity": 10000.0, "commission_per_lot": "0", "label": "fixture"},
                      provenance={"source": "synthetic fixture (tests only)"}, cost_model={"recorded": "fixture"},
                      trades=trades, regime_source={"source": "fixture"} if regimes else None)
    validate(ds)
    return ds

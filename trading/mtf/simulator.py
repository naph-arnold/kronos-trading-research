"""Event simulation of multi-timeframe candidates on the execution timeframe (research only; no orders).

For each candidate, in time order:
1. Exits of open simulated positions that completed earlier are realized.
2. Account state at the next execution candle's open: equity marked to the executable side, day-start equity,
   daily-loss latch (1.5%, using the worst intraday mark), and optional research halts (weekly loss,
   consecutive losses, per-session entry cap, hybrid scalp-share cap). These can only ADD rejections.
3. Planned reward-to-risk after estimated costs (spread, slippage both ways, fees/commission) must be >=
   the candidate's `min_net_rr`.
4. `trading.risk.evaluate` (unchanged) decides and sizes: 0.5% intended risk on CURRENT equity, round-down
   sizing, minimum-lot rejection, 1% aggregate open risk, spread cap, session calendar, event blackout,
   long-only spot, venue filters.
5. Approved entries fill at the open on the executable side plus slippage. Exits scan forward: stop first,
   then target, then the time limit. A candle opening beyond the stop fills at that open (gap).

Price convention per market: MT5 candles are BID prices (ask = bid + spread); Binance candles are traded
prices treated as mid (bid/ask = mid -/+ spread/2).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable

import numpy as np
import pandas as pd

from trading.breaker import CircuitBreaker
from trading.common import MARKET_SYMBOLS, Market, Side
from trading.config import load_demo_environment
from trading.instruments import BinanceSpotSpec
from trading.marketdata import Quote
from trading.mtf.frames import MINUTES, Frame
from trading.risk import RiskLimits, evaluate
from trading.signals import TradeSignal
from trading.state import AccountSnapshot, OpenPosition

NS_MIN = 60_000_000_000


@dataclass
class MarketSpec:
    market: Market
    instrument: object  # Mt5InstrumentSpec | BinanceSpotSpec (verified spec; costs = scenario assumption)
    limits: RiskLimits
    spread_fn: Callable[[np.ndarray], np.ndarray]  # open_ns -> spread (price units)
    bar_convention: str  # "bid" (MT5) or "mid" (Binance)
    day_key: Callable[[int], object]  # ns -> risk day
    session_fn: Callable[[int], str]
    calendar: object = None
    event_guard: Callable | None = None
    swap: object = None  # trading.shadow.costs.SwapSpec or None
    tick: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class AccountRules:
    initial_equity: float = 10_000.0
    max_open_positions: int = 1
    weekly_loss_halt: float | None = None
    consecutive_loss_halt: int | None = None
    session_entry_cap: int | None = None
    max_scalp_share: float | None = None  # hybrid only: share of B entries among all entries
    allow_opposite: bool = False  # hybrid conflict rule: no new position opposite to an open one


@dataclass
class _Pos:
    tag: str
    side: int
    size: float
    entry: float
    stop: float
    target: float
    open_i: int
    exit_i: int
    exit_price: float
    exit_reason: str
    planned_risk: float
    risk_to_stop: float
    meta: dict = field(default_factory=dict)


def _dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc)


def _d(x: float, tick: Decimal, *, up: bool) -> Decimal:
    q = Decimal(repr(float(x))) / tick
    return q.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


def simulate(cands: pd.DataFrame, base: Frame, spec: MarketSpec, rules: AccountRules, *, start_ns: int,
             end_ns: int, record_rejections: bool = True) -> dict:
    inst, limits = spec.instrument, spec.limits
    if limits.max_open_positions != rules.max_open_positions:
        from dataclasses import replace
        limits = replace(limits, max_open_positions=rules.max_open_positions)
    env = load_demo_environment({"EXECUTION_MODE": "demo"})
    breaker = CircuitBreaker(spec.market)
    symbol = MARKET_SYMBOLS[spec.market]
    o, h, low, c = (base.df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    t_open = base.open_time
    n = len(o)
    sp = spec.spread_fn(t_open)
    if spec.bar_convention == "bid":
        bid_o, bid_h, bid_l, bid_c = o, h, low, c
        ask_o, ask_h, ask_l, ask_c = o + sp, h + sp, low + sp, c + sp
    else:
        hs = sp / 2
        bid_o, bid_h, bid_l, bid_c = o - hs, h - hs, low - hs, c - hs
        ask_o, ask_h, ask_l, ask_c = o + hs, h + hs, low + hs, c + hs
    slip = float(limits.slippage_allowance)
    vpu = float(inst.value_per_price_unit(Decimal("1")))
    base_min = MINUTES[base.interval]

    def cost(size: float, a: float, b: float) -> float:
        return float(inst.round_trip_cost(Decimal(repr(size)), Decimal(repr(a)), Decimal(repr(b))))

    cash = rules.initial_equity
    open_pos: list[_Pos] = []
    trades, rejections = [], {}
    day, day_start, day_latched, day_min = None, cash, False, cash
    week, week_start = None, cash
    consec_losses, last_loss_day = 0, None
    session_counts: dict = {}
    entries_by_tag = {"A": 0, "B": 0}
    claimed: set = set()

    def mark(i: int, pos: _Pos, worst: bool = False, from_i: int | None = None) -> float:
        # `worst` uses only COMPLETED candles before i (their lows/highs are known at i's open) plus i's open.
        if pos.side == 1:
            px = min(float(np.min(bid_l[from_i:i])), bid_o[i]) if worst and from_i < i else bid_o[i]
        else:
            px = max(float(np.max(ask_h[from_i:i])), ask_o[i]) if worst and from_i < i else ask_o[i]
        return vpu * pos.size * (px - pos.entry) * pos.side - cost(pos.size, pos.entry, px)

    def realize(p: _Pos):
        nonlocal cash, consec_losses, last_loss_day
        pnl = vpu * p.size * (p.exit_price - p.entry) * p.side - cost(p.size, p.entry, p.exit_price)
        swap_cost, nights = 0.0, 0
        if spec.swap is not None:
            s, nights = spec.swap.charge(Side.LONG if p.side == 1 else Side.SHORT, Decimal(repr(p.size)),
                                         _dt(int(t_open[p.open_i])), _dt(int(t_open[min(p.exit_i, n - 1)])))
            swap_cost = float(s)
        pnl += swap_cost
        cash += pnl
        # gross = mid-to-mid price move; costs = spread + slippage + commission/fees - swap (all explicit)
        gross = vpu * p.size * (p.meta["exit_mid"] - p.meta["entry_mid"]) * p.side
        r = pnl / p.planned_risk if p.planned_risk else None
        trades.append({"tag": p.tag, "side": p.side, "size": p.size, "entry": p.entry, "exit": p.exit_price,
                       "entry_mid": p.meta["entry_mid"], "exit_mid": p.meta["exit_mid"],
                       "opened_ns": int(t_open[p.open_i]), "closed_ns": int(t_open[min(p.exit_i, n - 1)]),
                       "exit_reason": p.exit_reason, "pnl": pnl, "gross": gross, "costs": gross - pnl,
                       "swap": swap_cost, "r": r, "r_gross": gross / p.planned_risk if p.planned_risk else None,
                       "planned_risk": p.planned_risk, "stop_dist": abs(p.entry - p.stop), "equity_after": cash,
                       **{k: v for k, v in p.meta.items() if k not in ("entry_mid", "exit_mid")}})
        if pnl <= 0:
            consec_losses += 1
            last_loss_day = spec.day_key(int(t_open[min(p.exit_i, n - 1)]))
        else:
            consec_losses = 0

    def reject(reason: str):
        if record_rejections:
            key = reason.split(":")[0][:60]
            rejections[key] = rejections.get(key, 0) + 1

    sel = cands[(cands.time_ns >= start_ns) & (cands.time_ns < end_ns)]
    order = sel.assign(_p=sel.tag.map({"A": 0, "TREND": 0, "RANDOM": 0, "B": 1}).fillna(2)).sort_values(
        ["time_ns", "_p"], kind="stable")
    for row in order.itertuples(index=False):
        t = int(row.time_ns)
        i = int(np.searchsorted(t_open, t, side="left"))
        if i >= n:
            break
        if t_open[i] - t > 5 * NS_MIN:
            reject("No execution candle within 5 minutes of the signal")
            continue
        for p in sorted([p for p in open_pos if p.exit_i < i], key=lambda q: q.exit_i):
            open_pos.remove(p)
            realize(p)
        equity = cash + sum(mark(i, p) for p in open_pos)
        dk = spec.day_key(t)
        if dk != day:
            day, day_start, day_latched, day_min = dk, equity, False, equity
            day_start_i = i
        worst = cash + sum(mark(i, p, worst=True, from_i=max(p.open_i, day_start_i)) for p in open_pos)
        day_min = min(day_min, worst, equity)
        if (day_start - day_min) / day_start >= float(limits.daily_loss_halt_fraction):
            day_latched = True
        wk = pd.Timestamp(t, tz="UTC").isocalendar()[:2]
        if wk != week:
            week, week_start = wk, equity
        session = spec.session_fn(t)
        extra = []
        if rules.weekly_loss_halt and (week_start - equity) / week_start >= rules.weekly_loss_halt:
            extra.append("Weekly loss halt")
        if rules.consecutive_loss_halt and consec_losses >= rules.consecutive_loss_halt and last_loss_day == dk:
            extra.append("Consecutive-loss halt")
        if rules.session_entry_cap and session_counts.get((dk, session), 0) >= rules.session_entry_cap:
            extra.append("Session entry cap")
        if any(p.side != row.side for p in open_pos) and not rules.allow_opposite:
            extra.append("Conflict - opposite exposure open")
        if any(p.tag == row.tag for p in open_pos):
            extra.append("Conflict - same strategy already in a position")
        if rules.max_scalp_share is not None and row.tag == "B":
            total = entries_by_tag["A"] + entries_by_tag["B"]
            if (entries_by_tag["B"] + 1) / (total + 1) > rules.max_scalp_share and entries_by_tag["A"] > 0:
                extra.append("Scalp share cap")
        long = row.side == 1
        tick = spec.tick
        entry_ref = _d(row.entry_ref, tick, up=not long)
        stop = _d(row.stop, tick, up=not long)
        risk_px = abs(float(entry_ref) - float(stop))
        target = _d(float(entry_ref) + row.side * row.rr * risk_px, tick, up=not long)
        fill = (ask_o[i] + slip) if long else (bid_o[i] - slip)
        stop_fill = float(stop) - slip if long else float(stop) + slip
        reward = (float(target) - fill) * row.side - cost(1.0, fill, float(target)) / vpu
        loss = (fill - stop_fill) * row.side + cost(1.0, fill, stop_fill) / vpu
        if loss <= 0 or reward / loss < row.min_net_rr:
            extra.append("Net reward-to-risk after costs below minimum")
        if extra:
            for e in extra:
                reject(e)
            continue
        now = _dt(int(t_open[i]))
        sid = f"{row.tag}-{t // NS_MIN}-{'L' if long else 'S'}"
        try:
            signal = TradeSignal.create(signal_id=sid, market=spec.market.value, symbol=symbol,
                                        side="long" if long else "short", entry_price=entry_ref, stop_price=stop,
                                        take_profit=target, strategy_id=f"mtf-{row.tag.lower()}", strategy_version="1",
                                        generated_at=_dt(t), bar_close_time=_dt(t))
        except Exception as exc:  # noqa: BLE001 - malformed candidate -> rejection
            reject(f"Invalid signal: {type(exc).__name__}")
            continue
        quote = Quote(symbol=symbol, bid=_d(bid_o[i], Decimal("1E-8"), up=False), ask=_d(ask_o[i], Decimal("1E-8"), up=True),
                      time=now)
        used = sum(p.size * (p.entry if isinstance(inst, BinanceSpotSpec) else float(inst.margin_per_lot)) for p in open_pos)
        positions = tuple(OpenPosition(f"p{k}", symbol, Side.LONG if p.side == 1 else Side.SHORT,
                                       Decimal(repr(p.size)), Decimal(repr(p.entry)), Decimal(repr(p.stop)),
                                       Decimal(repr(round(p.risk_to_stop, 8)))) for k, p in enumerate(open_pos))
        eq = Decimal(repr(round(max(equity, 0.01), 8)))
        account = AccountSnapshot(market=spec.market, account_id="mtf-research", is_demo=True,
                                  currency=inst.account_currency, equity=eq, balance=Decimal(repr(round(cash, 8))),
                                  available_funds=max(eq - Decimal(repr(round(used, 8))), Decimal("0")),
                                  day_start_equity=Decimal(repr(round(max(day_start, 0.01), 8))), snapshot_at=now,
                                  reconciled=True, open_positions=positions, daily_loss_halted=day_latched)
        decision = evaluate(signal, account=account, instrument=inst, quote=quote, limits=limits, environment=env,
                            breaker=breaker, now=now, duplicate_check=lambda s: s.signal_id in claimed,
                            session_calendar=spec.calendar, event_guard=spec.event_guard)
        if not decision.approved:
            for r in decision.reasons:
                reject(r)
            continue
        claimed.add(sid)
        size = float(decision.size)
        hold = max(1, int(row.max_hold_minutes // base_min))
        last = min(n - 1, i + hold - 1)
        tp = float(target)
        if long:
            stop_hit = bid_l[i:last + 1] <= float(stop)
            tp_hit = bid_h[i:last + 1] >= tp
        else:
            stop_hit = ask_h[i:last + 1] >= float(stop)
            tp_hit = ask_l[i:last + 1] <= tp
        hit = stop_hit | tp_hit
        if hit.any():
            j = i + int(np.argmax(hit))
            if stop_hit[j - i]:
                gapped = j > i and ((bid_o[j] <= float(stop)) if long else (ask_o[j] >= float(stop)))
                trigger = (bid_o[j] if long else ask_o[j]) if gapped else float(stop)
                exit_price, reason = trigger - slip if long else trigger + slip, "stop_gap" if gapped else "stop"
            else:
                exit_price, reason = tp, "take_profit"
        else:
            j = last
            exit_price = bid_c[j] - slip if long else ask_c[j] + slip
            reason = "time_exit" if last == i + hold - 1 else "end_of_data"
        half_exit = sp[j] / 2
        if reason in ("stop", "stop_gap"):
            raw = exit_price + slip if long else exit_price - slip
        elif reason == "take_profit":
            raw = exit_price
        else:
            raw = bid_c[j] if long else ask_c[j]
        exit_mid = raw + half_exit if long else raw - half_exit
        entry_mid = (bid_o[i] + ask_o[i]) / 2
        risk_to_stop = vpu * size * (fill - stop_fill) * row.side + cost(size, fill, stop_fill)
        open_pos.append(_Pos(row.tag, int(row.side), size, fill, float(stop), tp, i, j,
                             exit_price, reason, float(decision.planned_risk), risk_to_stop,
                             {"session": session, "signal_ns": t, "spread": float(sp[i]), "entry_mid": entry_mid,
                              "exit_mid": exit_mid}))
        entries_by_tag[row.tag] = entries_by_tag.get(row.tag, 0) + 1
        session_counts[(dk, session)] = session_counts.get((dk, session), 0) + 1
    for p in sorted(open_pos, key=lambda q: q.exit_i):
        realize(p)
    return {"trades": trades, "rejections": rejections, "final_equity": cash, "initial_equity": rules.initial_equity,
            "candidates": int(len(sel))}


def summarize(result: dict, *, resamples: int = 1000, seed: int = 7) -> dict:
    trades = result["trades"]
    init = result["initial_equity"]
    r = [t["r"] for t in trades if t["r"] is not None]
    pnl = [t["pnl"] for t in trades]
    curve = [init] + [t["equity_after"] for t in sorted(trades, key=lambda t: t["closed_ns"])]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    ci = None
    if len(r) >= 2:
        rng = np.random.default_rng(seed)
        means = np.sort(rng.choice(np.array(r), size=(resamples, len(r)), replace=True).mean(axis=1))
        ci = (round(float(means[int(0.025 * resamples)]), 4), round(float(means[int(0.975 * resamples) - 1]), 4))
    wins, losses = [x for x in pnl if x > 0], [-x for x in pnl if x <= 0]
    daily = pd.Series(pnl, index=pd.to_datetime([t["closed_ns"] for t in trades])).groupby(level=0).sum() if trades else None
    sharpe = None
    if daily is not None and len(daily) > 5:
        d = daily.resample("1D").sum() / init
        sharpe = round(float(d.mean() / d.std() * np.sqrt(365)), 3) if d.std() > 0 else None
    return {"trades": len(trades), "net_return_pct": round((result["final_equity"] / init - 1) * 100, 3),
            "max_drawdown_pct": round(mdd * 100, 3),
            "win_rate_pct": round(100 * len(wins) / len(pnl), 2) if pnl else None,
            "profit_factor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
            "mean_r": round(statistics.fmean(r), 4) if r else None, "mean_r_ci95": ci,
            "mean_r_gross": round(statistics.fmean([t["r_gross"] for t in trades]), 4) if trades else None,
            "cost_r": round(statistics.fmean([t["r_gross"] - t["r"] for t in trades]), 4) if trades else None,
            "sharpe_daily_annualized": sharpe, "candidates": result["candidates"],
            "rejections": dict(sorted(result["rejections"].items(), key=lambda x: -x[1])[:8])}

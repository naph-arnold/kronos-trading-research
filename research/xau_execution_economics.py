"""XAUUSD MT5 execution economics (descriptive, development data only; selects no strategy).

1. Spread by UTC hour from broker-tick M1 bars (minimum spread per minute: optimistic).
2. Round-trip market-order cost per lot and per R for stop distances, with commission scenarios and slippage.
3. Minimum-lot feasibility: the largest stop a 0.01 lot can carry within 0.5% risk at a given equity.
4. Market vs limit entry at SEEDED RANDOM TIMES (no signal): a buy/sell limit placed `offset` x ATR(M1) away with a
   N-minute expiry. Reports fill rate, the price drift after the fill, and the same drift for a market entry at the
   same moment. A limit saves the spread, but if fills cluster where price keeps moving against the order (adverse
   selection), the saving is illusory. Fills are approximated from bid OHLC + the minute's spread (no queue model).

Broker contract facts come from the recorded MT5 specification (MetaQuotes-Demo) and are NOT assumed to describe the
broker that would eventually be used for real money.
    python -m research.xau_execution_economics
"""
from __future__ import annotations

import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

DATA = Path("C:/Users/Admin/Projects/kronos-mtf-research/data/raw/mt5_xauusd_1m_ticks_2025_2026.csv")
CONTRACT, POINT, MIN_LOT = 100.0, 0.01, 0.01
SLIP = 0.10
SWAP_USD_PER_LOT_NIGHT = {"long": -12.6, "short": -4.6}  # recorded symbol_info, triple on Wednesday


def lower_priority():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)


def load():
    rows = []
    with DATA.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((r["open_time_utc"], int(r["open_time_utc"][11:13]), float(r["high"]), float(r["low"]),
                         float(r["close"]), int(r["spread_points"]) * POINT))
    return rows


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))] if xs else None


def main() -> int:
    lower_priority()
    rows = load()
    by_hour = defaultdict(list)
    for _, h, *_rest, sp in rows:
        by_hour[h].append(sp)
    spread_by_hour = {h: {"median": round(q(v, 0.5), 3), "p90": round(q(v, 0.9), 3)} for h, v in sorted(by_hour.items())}
    # ATR(14) per minute
    atr, prev, atrs = None, None, []
    for _, _, hi, lo, c, _ in rows:
        tr = hi - lo if prev is None else max(hi - lo, abs(hi - prev), abs(lo - prev))
        atr = tr if atr is None else atr + (tr - atr) / 14
        prev = c
        atrs.append(atr)
    med_spread = q([r[5] for r in rows], 0.5)
    costs = []
    for comm in (0.0, 3.5, 7.0):
        per_lot = med_spread * CONTRACT + 2 * SLIP * CONTRACT + comm
        for stop in (1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
            costs.append({"commission_per_lot_rt": comm, "stop_usd": stop, "cost_usd_per_lot": round(per_lot, 2),
                          "cost_r": round(per_lot / (stop * CONTRACT), 4)})
    min_lot = {str(eq): {"risk_budget": eq * 0.005, "max_stop_usd_at_min_lot": round(eq * 0.005 / (MIN_LOT * CONTRACT), 2)}
               for eq in (3000, 10000, 25000)}
    # market vs limit at seeded random times
    rng = random.Random(20261109)
    out = {}
    for offset in (0.25, 0.5, 1.0):
        for expiry in (5, 15):
            fills, drift_lim, drift_mkt, n = 0, [], [], 0
            for _ in range(20000):
                i = rng.randrange(30, len(rows) - expiry - 31)
                side = rng.choice((1, -1))
                a = atrs[i]
                close, sp = rows[i][4], rows[i][5]
                market_entry = close + sp if side == 1 else close  # buy at ask, sell at bid
                limit = market_entry - side * offset * a
                filled_at = None
                for j in range(i + 1, i + 1 + expiry):
                    hi, lo, spj = rows[j][2], rows[j][3], rows[j][5]
                    ask_low, bid_high = lo + spj, hi
                    if (side == 1 and ask_low <= limit) or (side == -1 and bid_high >= limit):
                        filled_at = j
                        break
                n += 1
                horizon = 15
                if filled_at is not None and filled_at + horizon < len(rows):
                    fills += 1
                    exit_mid = rows[filled_at + horizon][4]
                    drift_lim.append(side * (exit_mid - limit) / a)
                if i + horizon < len(rows):
                    drift_mkt.append(side * (rows[i + horizon][4] - market_entry) / a)
            out[f"offset_{offset}atr_expiry_{expiry}m"] = {
                "fill_rate": round(fills / n, 4),
                "mean_15m_drift_after_fill_atr": round(sum(drift_lim) / len(drift_lim), 4) if drift_lim else None,
                "mean_15m_drift_market_entry_atr": round(sum(drift_mkt) / len(drift_mkt), 4)}
    report = {"data": DATA.name, "minutes": len(rows), "spread_median": med_spread, "spread_by_utc_hour": spread_by_hour,
              "atr_m1_median": q(atrs[14:], 0.5), "market_cost_per_lot": costs, "minimum_lot": min_lot,
              "swap_usd_per_lot_night": SWAP_USD_PER_LOT_NIGHT, "limit_vs_market_random_times": out,
              "notes": ["minimum spread per minute understates paid spread",
                        "fills approximated from bid OHLC + minute spread; no queue position, no partial fills",
                        "random entry times: measures execution mechanics only, not any strategy edge"]}
    Path("research/output").mkdir(parents=True, exist_ok=True)
    Path("research/output/xau_execution_economics.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("minutes", "spread_median", "atr_m1_median", "minimum_lot")}, indent=0))
    print("spread by UTC hour (median/p90):", {h: (v["median"], v["p90"]) for h, v in spread_by_hour.items()})
    for c in costs:
        if c["stop_usd"] in (2.0, 5.0, 12.0):
            print(f"  comm {c['commission_per_lot_rt']:>4} stop {c['stop_usd']:>5}: {c['cost_usd_per_lot']} USD/lot = {c['cost_r']} R")
    for k, v in out.items():
        print(" ", k, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

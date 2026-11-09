"""XAUUSD scalping cost floor (descriptive; selects no strategy and tunes no parameter).

For a round trip, cost per ounce = spread + 2 x slippage + commission per lot / contract size. With a stop distance
s (USD per ounce), cost in R = cost / s. A trade then wins (k - c) R or loses (1 + c) R, so the break-even win rate
is (1 + c) / (1 + k). This shows how large a stop (and gross edge) a scalper needs before any signal can pay.

Inputs (verified, read-only datasets with manifests): MT5 bid M1 bars built from broker ticks, with the MINIMUM
spread per minute (`spread_points`, point 0.01) for 2019-2020 and 2025-05 -> 2026-11. The minimum spread
understates the spread actually paid, so every figure here is optimistic. Commission is not exposed by MT5 and is
shown as 0 and 7 USD per lot round trip (scenarios); slippage uses the runners' configured allowance (0.10).

    python -m research.xau_scalping_cost_floor --out research/output/scalping_cost_floor.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

DATASETS = {
    "2019-2020": "C:/Users/Admin/Projects/kronos-mtf-research/data/raw/mt5_xauusd_1m_ticks_2019_2020.csv",
    "2025-2026": "C:/Users/Admin/Projects/kronos-mtf-research/data/raw/mt5_xauusd_1m_ticks_2025_2026.csv",
}
POINT = 0.01
CONTRACT = 100.0
SLIPPAGE_PER_SIDE = 0.10
COMMISSIONS = (0.0, 7.0)
STOP_ATR_MULTIPLES = (1, 2, 3, 5, 10, 20)
REWARD_TO_RISK = (1.0, 1.5, 2.0, 3.0)


def session(hour: int) -> str:
    return "asia" if hour < 7 else "london" if hour < 12 else "new_york" if hour < 21 else "late"


def _q(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def scan(path: Path) -> dict:
    manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest.get("sha256") and manifest["sha256"] != digest:
        raise ValueError(f"{path.name}: SHA-256 differs from its manifest")
    spreads, atrs = defaultdict(list), defaultdict(list)
    prev_close, atr, n = None, None, 0
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            h, low, c = float(r["high"]), float(r["low"]), float(r["close"])
            tr = h - low if prev_close is None else max(h - low, abs(h - prev_close), abs(low - prev_close))
            atr = tr if atr is None else atr + (tr - atr) / 14  # Wilder ATR(14)
            prev_close = c
            n += 1
            sess = session(int(r["open_time_utc"][11:13]))
            spreads[sess].append(int(r["spread_points"]) * POINT)
            if n > 14:
                atrs[sess].append(atr)
    allsp = [x for v in spreads.values() for x in v]
    allatr = [x for v in atrs.values() for x in v]
    by = {s: {"minutes": len(spreads[s]), "spread_median": _q(spreads[s], 0.5), "spread_p90": _q(spreads[s], 0.9),
              "atr_m1_median": _q(atrs[s], 0.5)} for s in sorted(spreads)}
    return {"file": path.name, "sha256": digest, "minutes": n, "spread_median": _q(allsp, 0.5), "spread_p90": _q(allsp, 0.9),
            "atr_m1_median": _q(allatr, 0.5), "atr_m1_p90": _q(allatr, 0.9), "by_session": by}


def cost_table(spread: float, atr: float) -> list[dict]:
    rows = []
    for comm in COMMISSIONS:
        cost = spread + 2 * SLIPPAGE_PER_SIDE + comm / CONTRACT
        for m in STOP_ATR_MULTIPLES:
            s = m * atr
            c = cost / s
            rows.append({"commission_per_lot": comm, "stop_atr_m1": m, "stop_usd": round(s, 3), "cost_usd_per_oz": round(cost, 3),
                         "cost_r": round(c, 4),
                         "breakeven_win_rate_pct": {str(k): round(100 * (1 + c) / (1 + k), 2) for k in REWARD_TO_RISK}})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="research/output/scalping_cost_floor.json")
    args = ap.parse_args(argv)
    out = {"purpose": "descriptive cost floor; no strategy, no parameter selection", "slippage_per_side": SLIPPAGE_PER_SIDE,
           "commission_scenarios": COMMISSIONS, "spread_note": "minimum spread per minute: optimistic", "periods": {}}
    for name, p in DATASETS.items():
        stats = scan(Path(p))
        stats["cost_table_median_spread"] = cost_table(stats["spread_median"], stats["atr_m1_median"])
        out["periods"][name] = stats
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    for name, s in out["periods"].items():
        print(f"{name}: {s['minutes']} M1 bars; min-spread median {s['spread_median']:.2f} p90 {s['spread_p90']:.2f}; "
              f"ATR(M1) median {s['atr_m1_median']:.3f} p90 {s['atr_m1_p90']:.3f}")
        for sess, b in s["by_session"].items():
            print(f"   {sess:9s} spread med {b['spread_median']:.2f} p90 {b['spread_p90']:.2f}  ATR med {b['atr_m1_median']:.3f}")
        for r in s["cost_table_median_spread"]:
            print(f"   comm {r['commission_per_lot']:>3}  stop {r['stop_atr_m1']:>2}xATR = {r['stop_usd']:6.2f}  cost {r['cost_usd_per_oz']:.2f}"
                  f" = {r['cost_r']:.3f} R  breakeven win% {r['breakeven_win_rate_pct']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

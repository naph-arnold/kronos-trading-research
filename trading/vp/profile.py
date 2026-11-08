"""Deterministic Volume Profile engine (research only; no orders).

Data model
----------
`BinStore` holds activity weights summed per (15-minute bucket, base price bin): bucket = UTC start time of a
15-minute interval, base bin index b covers prices [b*base_bin, (b+1)*base_bin). The weight's meaning is carried
by `weight_kind` (see research/vp_data.WEIGHT_KINDS) and is never silently mixed:
* `traded_base_qty` - measured exchange-traded quantity (Binance aggregate trades);
* `quote_ticks`     - PROXY: broker quote-update counts at the bid (MT5 CFD has no traded volume);
* `tpo_minutes`     - time-at-price from 1-minute candle ranges (contains no volume information).

A profile over [start, end) uses only buckets that START at or after `start` and END at or before `end`, so a
completed-period profile is frozen at `end` and a developing profile at time t uses completed buckets only.

Profile bins are integer multiples k of the base bin: profile bin j covers base bins [j*k, (j+1)*k).
`bin_multiple(ref_price, bin_bps, base_bin)` = max(1, round(ref_price * bin_bps / 1e4 / base_bin)), with the
reference price taken from the profile's own completed data (mid of its traded range), so bins scale with price.

Point of control (POC): the profile bin with the largest weight. Ties: the tied bin closest to the centre of the
profile's price range (index (n-1)/2); if still tied, the LOWER price. POC price = centre of that bin.

Value area (VA): start from the POC bin and expand one step at a time. Each step compares the summed weight of the
next `rows` bins above with the next `rows` bins below (a side with no bins left is unavailable); the larger side is
added; on an exact tie both sides are added. Expansion stops as soon as the included weight is >= va_pct * total.
VAL = lower edge of the lowest included bin, VAH = upper edge of the highest included bin. Empty bins inside the
traded range count as zero weight.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

NS_MS = 1_000_000
NS_MIN = 60_000_000_000


@dataclass(frozen=True, eq=False)
class Profile:
    start_ns: int
    end_ns: int
    weight_kind: str
    bin_size: float
    lo_price: float          # lower edge of the first profile bin
    weights: np.ndarray      # per profile bin, ascending price
    va_pct: float
    poc_idx: int
    va_lo_idx: int
    va_hi_idx: int
    buckets: int             # 15-minute buckets that contributed

    @property
    def total(self) -> float:
        return float(self.weights.sum())

    @property
    def poc(self) -> float:
        return self.lo_price + (self.poc_idx + 0.5) * self.bin_size

    @property
    def val(self) -> float:
        return self.lo_price + self.va_lo_idx * self.bin_size

    @property
    def vah(self) -> float:
        return self.lo_price + (self.va_hi_idx + 1) * self.bin_size

    @property
    def low(self) -> float:
        return self.lo_price

    @property
    def high(self) -> float:
        return self.lo_price + len(self.weights) * self.bin_size

    def va_share(self) -> float:
        return float(self.weights[self.va_lo_idx:self.va_hi_idx + 1].sum() / self.total)


def point_of_control(w: np.ndarray) -> int:
    w = np.asarray(w, dtype=float)
    if w.size == 0 or not np.isfinite(w).all() or (w < 0).any() or w.sum() <= 0:
        raise ValueError("profile weights must be finite, non-negative and not all zero")
    cands = np.flatnonzero(w == w.max())
    dist = np.abs(cands - (w.size - 1) / 2.0)
    return int(cands[np.flatnonzero(dist == dist.min())[0]])  # cands ascending -> lowest price on a tie


def value_area(w: np.ndarray, va_pct: float, rows: int = 1) -> tuple[int, int, int]:
    """(poc_idx, va_lo_idx, va_hi_idx) under the rules in the module docstring."""
    if not 0 < va_pct <= 1:
        raise ValueError("va_pct must be in (0, 1]")
    if rows < 1:
        raise ValueError("rows must be >= 1")
    w = np.asarray(w, dtype=float)
    poc = point_of_control(w)
    total, n = float(w.sum()), w.size
    lo = hi = poc
    acc = float(w[poc])
    target = va_pct * total
    while acc < target * (1 - 1e-12) and (lo > 0 or hi < n - 1):
        up = float(w[hi + 1:hi + 1 + rows].sum()) if hi < n - 1 else -1.0
        up_n = min(rows, n - 1 - hi)
        dn = float(w[max(0, lo - rows):lo].sum()) if lo > 0 else -1.0
        dn_n = min(rows, lo)
        if up > dn:
            hi, acc = hi + up_n, acc + up
        elif dn > up:
            lo, acc = lo - dn_n, acc + dn
        else:
            hi, lo, acc = hi + up_n, lo - dn_n, acc + up + dn
    return poc, lo, hi


def bin_multiple(ref_price: float, bin_bps: float, base_bin: float) -> int:
    return max(1, int(round(ref_price * bin_bps / 1e4 / base_bin)))


def build_profile(bins: np.ndarray, w: np.ndarray, *, base_bin: float, bin_bps: float, va_pct: float,
                  weight_kind: str, start_ns: int, end_ns: int, buckets: int, rows: int = 1) -> Profile:
    bins = np.asarray(bins, dtype=np.int64)
    lo_b, hi_b = int(bins.min()), int(bins.max())
    ref = (lo_b + hi_b + 1) / 2.0 * base_bin
    k = bin_multiple(ref, bin_bps, base_bin)
    coarse = np.floor_divide(bins, k)
    c0 = int(coarse.min())
    weights = np.bincount(coarse - c0, weights=np.asarray(w, dtype=float))
    poc, lo, hi = value_area(weights, va_pct, rows)
    return Profile(start_ns, end_ns, weight_kind, k * base_bin, c0 * k * base_bin, weights, va_pct, poc, lo, hi, buckets)


class BinStore:
    """Activity weights per (15-minute bucket, base bin), sorted by bucket."""

    def __init__(self, bucket_ms: np.ndarray, bins: np.ndarray, w: np.ndarray, *, base_bin: float, weight_kind: str,
                 bucket_minutes: int = 15, manifest: dict | None = None):
        bucket_ms = np.asarray(bucket_ms, dtype=np.int64)
        if bucket_ms.size and (np.diff(bucket_ms) < 0).any():
            raise ValueError("buckets must be sorted")
        if (np.asarray(w) < 0).any():
            raise ValueError("negative weight")
        self.bucket_ns = bucket_ms * NS_MS
        self.bins, self.w = np.asarray(bins, dtype=np.int64), np.asarray(w, dtype=float)
        self.base_bin, self.weight_kind = float(base_bin), weight_kind
        self.bucket_ns_len = bucket_minutes * NS_MIN
        self.manifest = manifest or {}

    @classmethod
    def load(cls, prefix: str | Path) -> "BinStore":
        import hashlib
        prefix = Path(prefix)
        man = json.loads(prefix.with_suffix(".json").read_text())
        npz = prefix.with_suffix(".npz")
        if hashlib.sha256(npz.read_bytes()).hexdigest() != man["sha256"]:
            raise ValueError(f"{npz}: checksum mismatch")
        d = np.load(npz)
        return cls(d["bucket_ms"], d["bin"], d["w"], base_bin=man["base_bin"], weight_kind=man["weight_kind"],
                   bucket_minutes=man["bucket_minutes"], manifest=man)

    def span(self, start_ns: int, end_ns: int) -> tuple[int, int]:
        """Row range of buckets fully inside [start, end)."""
        a = int(np.searchsorted(self.bucket_ns, start_ns, side="left"))
        b = int(np.searchsorted(self.bucket_ns, end_ns - self.bucket_ns_len, side="right"))
        return a, max(a, b)

    def profile(self, start_ns: int, end_ns: int, *, bin_bps: float, va_pct: float, rows: int = 1,
                min_buckets: int = 1) -> Profile | None:
        a, b = self.span(start_ns, end_ns)
        if b <= a:
            return None
        nb = int(np.unique(self.bucket_ns[a:b]).size)
        if nb < min_buckets or self.w[a:b].sum() <= 0:
            return None
        return build_profile(self.bins[a:b], self.w[a:b], base_bin=self.base_bin, bin_bps=bin_bps, va_pct=va_pct,
                             weight_kind=self.weight_kind, start_ns=start_ns, end_ns=end_ns, buckets=nb, rows=rows)

    def developing(self, start_ns: int, t_ns: int, **kw) -> Profile | None:
        """Developing profile of a period that started at `start_ns`, as known at time t: completed buckets only."""
        cut = start_ns + (t_ns - start_ns) // self.bucket_ns_len * self.bucket_ns_len
        return self.profile(start_ns, cut, **kw) if cut > start_ns else None


def tpo_store(bars: pd.DataFrame, base_bin: float, bucket_minutes: int = 15) -> BinStore:
    """Time-at-price control: each 1-minute candle adds 1 to every base bin between its low and high."""
    t = bars.index.values.astype("datetime64[ns]").astype(np.int64) // NS_MS
    lo = np.floor(bars["low"].to_numpy() / base_bin + 1e-9).astype(np.int64)
    hi = np.floor(bars["high"].to_numpy() / base_bin + 1e-9).astype(np.int64)
    bucket = t // (bucket_minutes * 60_000) * (bucket_minutes * 60_000)
    parts = []
    step = 200_000
    for s in range(0, len(t), step):
        n = hi[s:s + step] - lo[s:s + step] + 1
        rep_b = np.repeat(bucket[s:s + step], n)
        start = np.repeat(lo[s:s + step], n)
        off = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        g = pd.DataFrame({"b": rep_b, "k": start + off}).groupby(["b", "k"], sort=True).size()
        parts.append(g)
    g = pd.concat(parts).groupby(level=[0, 1], sort=True).sum()
    return BinStore(g.index.get_level_values(0).to_numpy(), g.index.get_level_values(1).to_numpy(),
                    g.to_numpy(dtype=float), base_bin=base_bin, weight_kind="tpo_minutes", bucket_minutes=bucket_minutes)


# --------------------------------------------------------------------------------------------- periods

def periods(kind: str, start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[int, int, str]]:
    """Profile periods [start_ns, end_ns, label] covering [start, end).

    utc_day       00:00-24:00 UTC
    utc_session8  00-08 (asia), 08-16 (europe), 16-24 (us) UTC
    xau_day       17:00 New York -> 17:00 New York (gold trading day, labelled by its closing date)
    xau_session   17:00-03:00 (asia), 03:00-08:00 (london), 08:00-17:00 (new_york) New York time
    """
    out = []
    days = pd.date_range(start.normalize() - pd.Timedelta(days=1), end.normalize() + pd.Timedelta(days=1), freq="D", tz="UTC")
    if kind == "utc_day":
        out = [(d.value, (d + pd.Timedelta(days=1)).value, f"{d:%Y-%m-%d}") for d in days]
    elif kind == "utc_session8":
        for d in days:
            for h, lab in ((0, "asia"), (8, "europe"), (16, "us")):
                s = d + pd.Timedelta(hours=h)
                out.append((s.value, (s + pd.Timedelta(hours=8)).value, f"{d:%Y-%m-%d}-{lab}"))
    elif kind in ("xau_day", "xau_session"):
        def ny(day, hour):  # wall-clock New York time -> UTC (handles DST by localizing the naive time)
            return pd.Timestamp(day).replace(hour=hour).tz_localize("America/New_York").tz_convert("UTC")
        for d in days:
            today, yday = d.date(), (d - pd.Timedelta(days=1)).date()
            if kind == "xau_day":
                out.append((ny(yday, 17).value, ny(today, 17).value, f"{d:%Y-%m-%d}"))
            else:
                marks = [ny(yday, 17), ny(today, 3), ny(today, 8), ny(today, 17)]
                for (a, b), lab in zip(zip(marks, marks[1:]), ("asia", "london", "new_york")):
                    out.append((a.value, b.value, f"{d:%Y-%m-%d}-{lab}"))
    else:
        raise ValueError(f"unknown period kind {kind}")
    return [p for p in out if p[1] > start.value and p[0] < end.value]


def completed_profiles(store: BinStore, kind: str, start: pd.Timestamp, end: pd.Timestamp, *, bin_bps: float,
                       va_pct: float, min_coverage: float, rows: int = 1) -> list[Profile]:
    """Every period's completed profile (frozen at period end); periods below `min_coverage` of their 15-minute
    buckets are dropped (holidays, closures, data gaps)."""
    out = []
    for s, e, _ in periods(kind, start, end):
        need = int(np.ceil(min_coverage * (e - s) / store.bucket_ns_len))
        p = store.profile(s, e, bin_bps=bin_bps, va_pct=va_pct, rows=rows, min_buckets=max(1, need))
        if p is not None:
            out.append(p)
    return out


def profile_table(profiles: list[Profile]) -> dict:
    """Compact, picklable level table (one row per completed profile, ascending end time)."""
    f = {"end_ns": lambda p: p.end_ns, "vah": lambda p: p.vah, "val": lambda p: p.val, "poc": lambda p: p.poc,
         "low": lambda p: p.low, "high": lambda p: p.high, "total": lambda p: p.total}
    return {k: np.array([fn(p) for p in profiles], dtype=np.int64 if k == "end_ns" else float) for k, fn in f.items()}


def levels_from_table(table: dict, t_ns: np.ndarray, max_age_ns: int) -> dict:
    """For each decision time, the most recent profile whose period ENDED at or before t (and not older than
    max_age). Returns arrays vah/val/poc/low/high/total/pid (pid = -1 and NaN levels when none is usable)."""
    ends = table["end_ns"]
    t_ns = np.asarray(t_ns, dtype=np.int64)
    idx = np.searchsorted(ends, t_ns, side="right") - 1
    ok = idx >= 0
    ok[ok] &= (t_ns[ok] - ends[idx[ok]]) <= max_age_ns
    pid = np.where(ok, idx, -1)
    out = {"pid": pid}
    for k in ("vah", "val", "poc", "low", "high", "total"):
        col = np.full(len(pid), np.nan)
        col[ok] = table[k][pid[ok]]
        out[k] = col
    return out


def levels_at(profiles: list[Profile], t_ns: np.ndarray, max_age_ns: int) -> dict:
    return levels_from_table(profile_table(profiles), t_ns, max_age_ns)

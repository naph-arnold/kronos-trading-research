# Volume Profile / value-area research: feasibility report

Research only. No orders of any kind; no credentials; no change to either running shadow service. Written and
committed before any Volume Profile strategy result was computed. The frozen evaluation plan is in
`research/configs/vp_v1_protocol.json`.

## 1. Volume data quality

### XAUUSD (MetaQuotes-Demo, MT5): no traded volume, quote ticks only
Read-only probe on 2026-11-08 (`symbol_info`, `copy_rates_from_pos`, `copy_ticks_range`):

| Item | Result |
| --- | --- |
| Symbol | `Metals\XAUUSD`, exchange field `XCCY`, CFD calc mode 4 (no exchange) |
| `volume`, `volume_real`, `session_volume`, `session_deals` | all 0 |
| `real_volume` in M1 and H1 bars (last 5,000 each) | 0 of 5,000 non-zero |
| `COPY_TICKS_TRADE` (trade ticks) for 2020-06-02, 2025-06-03, 2026-11-05 | 0 ticks |
| `COPY_TICKS_ALL` = `COPY_TICKS_INFO` | quote ticks only; `last`, `volume`, `volume_real` all 0 |
| Quote-tick rate, 14:00–15:00 UTC | 12,404 (2020) / 4,882 (2025) / 25,552 (2026) per hour |

**Conclusion: the broker supplies no traded volume for gold.** "Tick volume" is the number of quote updates, a
feed-dependent activity count whose level changes several-fold over time.

**Defensible XAUUSD profile:** a quote-activity profile built from each actual quote at its exact bid price
(weight = number of quote updates at that price). It is labelled `quote_ticks` (PROXY) everywhere and is never
described as volume. Only its *shape* within one period is used; that shape is unaffected by the feed's
long-run intensity drift.

**Engine check on 2019–2020.** The quote-tick profile is almost the same as a pure time-at-price (TPO) profile:
- identical POC on 55% of trading days (67% of sessions);
- VAH/VAL within a median of about 2 basis points.

So the gold "volume" profile carries little information beyond time at price. A TPO control is therefore part
of every test.

### Gold futures volume
Genuine traded gold volume exists only in exchange futures (COMEX GC).
- **No free verified source:** no free, verifiable intraday GC volume-at-price history is available to this
  project. CME DataMine and commercial vendors are paid and licensed.
- **Not the same price:** futures are not the same price as the XAUUSD CFD. GC trades at spot plus carry
  (a basis of roughly $10–40 that drifts), rolls every two months, and has its own session and settlement
  times. Futures levels would have to be mapped onto CFD prices with the contemporaneous basis, adding an error
  source of the same order as a value-area boundary's width (about 20–60 bps).
- **Recommendation:** licensed GC data would make the gold *volume* hypothesis testable. It is not needed to
  test whether volume-at-price has an edge at all, which BTC/USDT can answer with genuine exchange volume.
  Acquire it only if the BTC test shows an edge.

### BTC/USDT (Binance spot): measured traded volume
- **Source:** Binance's official public `data.binance.vision` monthly aggregate-trade archives (`aggTrades`),
  each verified against Binance's published SHA-256 `.CHECKSUM`. No credentials and no order endpoints.
- **Fields:** each row carries the exact trade price, base quantity, timestamp and the underlying trade-id range.
  Timestamps are milliseconds until 2024 and microseconds from 2025; the code normalizes both.
- **Completeness checks:**
  - contiguous aggregate ids;
  - contiguous trade-id ranges (`first_id` = previous `last_id` + 1);
  - non-decreasing timestamps;
  - positive prices and quantities;
  - per-minute quantity sum, high, low, open and close compared with the official 1-minute klines.
- **Availability:**
  - 2017-08-17 → present;
  - monthly archives of 1–115 MB (2017–2018) and 300–800 MB (2023–2026), about 6 MB/s download;
  - 2017-08 → 2018-12 has never been used by this project (genuinely unexamined).

## 2. Profile methodology (`trading/vp/profile.py`)
- **Storage:** weights are summed per (15-minute bucket, base price bin). A period profile is an exact sum of the
  buckets that lie fully inside the period.
  - **Completed profiles:** frozen at period close.
  - **Developing profiles:** use completed buckets only, which is tested for causality.
- **Weight kinds** are never mixed: `traded_base_qty` (BTC, measured), `quote_ticks` (XAU, proxy) and
  `tpo_minutes` (control).
- **Bins** are base-bin multiples sized in basis points of the period's own price (primary 2 bp; sensitivity 1
  and 5 bp). Base bins: BTC 2024–26 $5; XAU $0.10.
- **POC:** maximum-weight bin. Ties go to the bin closest to the centre of the range, then to the lower price.
- **Value area:**
  - expansion from the POC, one row at a time toward the heavier neighbour;
  - on an exact tie, both sides are added;
  - expansion stops at ≥ va_pct of the total;
  - VAL and VAH are bin edges; POC is the bin centre.
- **va_pct:** 70% primary, with 60% and 80% in the grid.
- **Periods:**
  - BTC: UTC day, and three 8-hour UTC sessions.
  - Gold: trading day (17:00 → 17:00 New York), and the Asia / London / New York sessions (New York clock,
    DST-aware).
  - Periods below a coverage threshold are dropped: BTC 90% and gold 60% of 15-minute buckets.
- **Levels:** a signal candle uses the most recent profile that **ended at or before the candle opened**
  (maximum age 4 days).
- **Approximations:** no candle-volume distribution approximation is used.

## 3. Deterministic entry and exit definitions (`trading/vp/strategies.py`)
Signals are on completed 5m or 15m candles, with execution on 1-minute candles at the next open.

### A. Rejection
- **Setup:** price traded beyond VAH (VAL) within the last 12 candles, then a completed candle closes back
  inside the value area. The return happens on this candle.
- **Stop:** the excursion extreme ± 0.10 ATR.
- **Target:** the POC, or the opposite VA boundary.
- **Limit:** one signal per side per level set.

### B. Breakout and retest
1. **Break:** a completed candle closes beyond VAH (VAL), coming from inside.
2. **Retest:** within 12 candles, a candle comes back to the boundary (within 0.25 ATR) and still closes
   outside.
3. **Confirmation:** the next candle also closes outside.
- **Invalidation:** any close back inside before confirmation.
- **Stop:** the retest/confirmation extreme ± 0.10 ATR.
- **Target:** 1.5R or 2.5R.

### Both strategies
- **Optional 4H confirmation:** a completed-4H EMA(50) slope direction must agree with the trade.
- **Net reward-to-risk:** must be ≥ 1.0 after spread, slippage and fees.
- **Hold limit:** 240 minutes.
- **Risk gate:** every entry is approved and sized by the unchanged `trading.risk.evaluate`.

## 4. Realistic execution constraints
- **Account limits:**
  - 0.5% intended risk on current equity, with round-down sizing and minimum-lot / minimum-notional rejection;
  - 1.5% daily loss halt;
  - 1% aggregate open risk.
- **Gold-specific controls:** spread cap, CME gold session calendar, FOMC blackout from official
  federalreserve.gov dates (2019–2020, 2025, 2026+), and swap.
- **BTC is long-only spot**, so short rejections and VAL breakdowns are not tradable there.
- **BTC costs:** 0.10% fee per side, slippage 2 USDT; stress: 1 bp spread + 10 USDT; low fee: 0.075%.
- **Gold costs:** the dataset's own historical spread by New York hour (p50); stress: commission 7/lot +
  slippage 0.30, and p90 spread.
- **Small account:** 3,000-account eligibility is reported.

## 5. Validation approach
1. **Data validation:** archive checksums, id continuity, kline cross-check (BTC); broker M1/H1 cross-check and
   calendar gaps (XAU).
2. **Profile-engine tests** independent of strategies: tie rules, bucket completeness, developing-profile
   causality, DST periods, reproducibility. Then strategy-rule tests: exact signals, invalidation and prefix
   invariance.
3. **Development:** chronological walk-forward (6-month train, 2-month test). Parameters are selected only on
   training windows; out-of-sample windows are concatenated.
   - **Grid:** 24 settings for each of 4 variants (A, A+4H, B, B+4H), so 96 trials per market.
   - **Development periods:** BTC 2024-06 → 2026-05 and XAU 2019–2020. These were previously examined by other
     strategies and are labelled as such.
4. **Benchmarks:**
   - 20 matched random-entry seeds;
   - **TPO-level control** and **prior-period high/low control** with identical rules. These answer whether
     *volume* adds anything beyond time-at-price or the plain range.
5. **Multiple testing:** every grid result is reported, and the deflated Sharpe ratio is computed for the best
   configuration against the 96 trials.
6. **Frozen confirmation:**
   - **XAU:** dev selections are evaluated once on 2025-07 → 2026-11. That period was previously examined by
     other strategies and is **not** unseen data.
   - **BTC:** frozen parameters are pre-registered, then the never-examined 2017-08 → 2018-12 history is
     downloaded and evaluated once.
7. **Shadow test:** a prospective shadow test is proposed only for a variant that passes every rule.

## 6. Expected limitations
- **No genuine gold volume:** the XAU profile is a quote-activity proxy and the "volume" hypothesis cannot be
  tested on it. The XAU part tests value-area levels built from quote activity.
- **No unseen gold data:** every available gold period has been examined before, so gold can produce only
  exploratory evidence plus a prospective pre-registration.
- **Long-only BTC:** spot BTC/USDT halves the setups.
- **Different BTC regime in 2017–2018:** higher relative tick size, a different fee era and a bubble/crash
  market. A failure there is informative; a success would still need prospective confirmation.
- **Statistical power:** dozens to a few hundred trades per variant; confidence intervals will be wide.
- **Levels only:** the analysis uses value-area levels only, not order flow (delta, imbalance).

# Volume Profile value-area research (vp-v1): results

Research only. No orders of any kind; no credentials. The feasibility report and the frozen protocol
(`research/configs/vp_v1_protocol.json`, sha256 `61f7c632…35ef`) were committed before any strategy result (commit
`4c95de4`). Both shadow runners (XAUUSD NY ORB, BTC/USDT Donchian) were never stopped or modified. A read-only
health gate ran before every downloaded month and before each stage, and both runners stayed at 100% heartbeat
uptime with 0 alerts and consistent pre-registrations throughout.

**Verdict:**
- No Volume Profile variant shows a positive, repeatable edge after costs, on either market.
- No variant passed the development rules.
- **Volume-at-price levels never beat simple time-at-price or prior-range levels.**
- **No shadow forward test is proposed.**

## 1. Data

### XAUUSD (MetaQuotes-Demo, read-only): quote-tick PROXY
No traded volume exists for the CFD. The proxy weight is the number of quote updates at each bid price (see
`docs/volume-profile-feasibility.md`).

| Dataset | Ticks | Validation |
| --- | --- | --- |
| 2019-01 → 2020-12 | 55,547,701 | Rebuilt M1 bars byte-identical to the validated PR #19 dataset (sha256 `76d45bc3…`) |
| 2025-05-27 → 2026-11-06 | 157,018,979 | 504,847 M1 bars, see below |

2025–2026 M1 bars:
- **vs broker M1:** 100% identical on all 99,080 overlapping minutes (spread 99.86%).
- **vs broker H1:** 99.98% identical.
- **Missing minutes:** 2.27% of open minutes. 261 bars fall at times the CME calendar marks closed: broker holiday
  hours such as July 3–4 and Dec 24.

### BTC/USDT (Binance official aggTrades): measured traded volume
**Development data, 2024-06 → 2026-05:**
- **Size:** 24 monthly archives with 945,257,084 aggregate trades covering 2,716,290,245 trades. Every archive
  matched its published SHA-256.
- **Integrity:** 0 aggregate-id gaps, 0 trade-id gaps, 0 timestamp reversals, 0 invalid rows, and 0 no-trade gaps
  longer than 5 minutes (no exchange outage visible).
- **Duplicate rows:**
  - **What:** Binance's own `BTCUSDT-aggTrades-2026-02.zip` repeats ids 3856672511–3856674510
    (2026-02-11 00:00–00:04 UTC): 3,000 extra rows, identical in every field.
  - **Handling:** exact re-sent rows are dropped (first copy kept) and counted; nothing is filled.
  - **Evidence:** the subsequent cross-check matched the official klines exactly.
- **Kline cross-check:** per-minute trade sums vs official 1m klines over all 1,051,200 minutes. Volume matched
  100% (max relative difference 2.5e-16; total 17,112,630.38 BTC on both sides), and open/high/low/close matched
  100%.
- **Profile coverage:** 723 of 754 day profiles and 2,169 of 2,262 session profiles met the 90% coverage rule.

### Download incidents and resources
- **First attempt killed:** the host stopped it for low system memory after 12 of 24 months, because the first
  downloader held every month in RAM. Nothing from it was saved: the shard directory was confirmed absent before
  the restart, so **no month was claimed as recovered**.
- **Rewritten downloader:**
  - one archive at a time, streamed to disk in 4 MB blocks, with SHA-256 checked while streaming;
  - one checksummed shard per month, plus a persistent `progress.json`;
  - resume skips verified shards, and stale `.partial` files are deleted at start;
  - free-disk check (≥ 10 GB) and runner health gate before every month;
  - a memory guard that pauses below 1.0 GB available, resumes above 1.5 GB, and stops safely after 30 minutes;
  - Windows background (CPU/I-O/memory) priority.
- **Priority bug:** the first background-priority call failed because of a 64-bit handle-type bug, since fixed.
  That run was lowered to BelowNormal from outside, affecting only the downloader process.
- **Measured usage:**
  - downloader peak working set 0.99 GB;
  - minimum system free memory during downloads 1.87 GB, with 0 memory pauses;
  - research walk-forward: 3 workers, ≤ 0.7 GB, minimum free 2.2 GB (sampled during its first minutes only; the
    monitor stopped early);
  - processed volume-at-price data: 88 MB on disk; raw archives are deleted after verification and aggregation;
  - free disk afterwards: 180 GB.

## 2. Profile engine checks (before any strategy)
POC, VAH and VAL are deterministic, with explicit tie rules. Profiles use only completed 15-minute buckets, and
levels are frozen at period end. Tests: `tests/test_vp_profile.py`.

| Check (2 bp bins, 70% VA, day profiles) | XAU 2019–20 | XAU 2025–26 | BTC 2024–26 |
| --- | --- | --- | --- |
| Median VA width (bp) | 56 | 87 | 158 |
| POC identical to time-at-price POC | 55% | 51% | 18% |
| Median VAH distance from TPO VAH (bp) | 2.1 | 2.1 | 7.5 |
| Median VAH shift, 2 bp vs 5 bp bins (bp) | 1.8 | 2.0 | 2.3 |

- **Gold:** the quote-tick "volume" profile is nearly a time-at-price profile.
- **BTC:** genuine traded volume produces materially different levels, so BTC is the real test of the volume
  hypothesis.
- **Bin size:** levels are stable to bin size, moving about 1–2 bp between 1, 2 and 5 bp bins.

## 3. Results
The design is the same for both markets:
- **Variants:** A = rejection, B = breakout-retest, each with and without 4H confirmation.
- **Grid:** 24 settings per variant, so 96 trials per market.
- **Selection:** walk-forward with 6-month training and 2-month tests; parameters are selected on training data
  only.
- **Account and risk:** 10,000 account, every entry through the unchanged `trading.risk.evaluate` (0.5% risk,
  1.5% daily halt, 1% aggregate risk).

### XAUUSD development, 2019-02 → 2020-12 (8 out-of-sample windows; prices previously examined in PR #19)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Beats random | TPO-level R | Range-level R | Stress net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 138 | −16.9 | −0.27 (−0.45, −0.08) | −0.05 | 2/20 | −0.33 | −0.15 | −16.4 |
| A + 4H | 143 | −23.4 | −0.38 (−0.55, −0.18) | −0.15 | 1/20 | −0.26 | −0.25 | −24.9 |
| B | 221 | −34.0 | −0.39 (−0.51, −0.26) | −0.20 | 0/20 | −0.22 | −0.20 | −24.0 |
| B + 4H | 197 | −16.7 | −0.19 (−0.33, −0.03) | −0.03 | 6/20 | −0.20 | −0.23 | −15.2 |

- **Grid:** 0/96 settings were profitable over the full span.
- **Deflated Sharpe ratio of the best settings:** 0.00–0.003.
- **Breakdowns:** every year and session is negative, except tiny-sample cells.

### XAUUSD frozen validation, 2025-07 → 2026-11 (single run; previously examined period, not unseen)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Beats random | Stress net % |
| --- | --- | --- | --- | --- | --- | --- |
| A | 170 | −5.8 | −0.08 (−0.27, +0.12) | −0.01 | 8/20 | −8.5 |
| A + 4H | 80 | −4.0 | −0.10 (−0.36, +0.20) | −0.03 | 5/20 | −6.7 |
| B | 232 | −2.6 | −0.03 (−0.17, +0.13) | +0.02 | 9/20 | −6.8 |
| B + 4H | 282 | −4.6 | −0.03 (−0.16, +0.10) | +0.01 | 13/20 | −9.8 |

No variant passed. Gold Volume Profile research is closed: not to be restarted or optimized.

### BTC/USDT development, 2024-12 → 2026-04 (8 out-of-sample windows; long-only spot)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Beats random | TPO-level R | Range-level R | Stress net % | Low-fee net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 83 | −11.5 | −0.29 (−0.49, −0.10) | −0.01 | 0.28 | 14/20 | −0.21 | −0.35 | −10.3 | −9.8 |
| A + 4H | 36 | −6.0 | −0.34 (−0.57, −0.08) | −0.06 | 0.28 | 8/20 | −0.33 | −0.38 | −7.2 | −4.0 |
| B | 43 | −3.6 | −0.17 (−0.44, +0.13) | +0.09 | 0.26 | 17/20 | −0.41 | −0.11 | −3.9 | −1.6 |
| B + 4H | 23 | −2.9 | −0.25 (−0.60, +0.10) | +0.03 | 0.28 | 14/20 | −0.44 | −0.24 | −4.1 | −2.8 |

**Costs dominate.** Gross results are roughly zero (−0.06 to +0.09 R), while costs take about 0.26–0.28 R per
trade, mostly the 0.10% taker fee on each side.

**Volume vs controls** (difference in mean R, bootstrap 95% CI): no CI excludes zero.

| Variant | Volume − TPO | Volume − range |
| --- | --- | --- |
| A | −0.08 (−0.36, +0.20) | +0.05 (−0.19, +0.30) |
| A + 4H | −0.01 (−0.42, +0.37) | +0.04 (−0.33, +0.41) |
| B | +0.24 (−0.12, +0.61) | −0.05 (−0.47, +0.36) |
| B + 4H | +0.18 (−0.30, +0.66) | −0.02 (−0.50, +0.48) |

**Multiple testing and parameter stability:**
- **Full-span grid:** 14/96 settings ended positive (A 0, A+4H 2, B 7, B+4H 5).
- **Deflated Sharpe ratio of each variant's best setting:** 0.00, 0.17, 0.02, 0.07, against 0.95 required. This
  is what chance would produce among 96 trials.
- **Unstable selections:** the window-by-window choice switched between day and session profiles and between
  va_pct 0.6, 0.7 and 0.8 in every variant.
- **Most-selected settings, full span:** mean R between −0.25 and +0.04.

**Out-of-sample windows and drawdown:**
- **Positive windows:** 1, 2, 2 and 3 of 8.
- **Underwater periods:** 423–477 days.
- **Max drawdown:** 4–13%.

**Regimes and conditions** (small cells; reported, not selected on):
- **Regime:** A loses in every regime. B is slightly positive only in down-low-volatility (9 trades, +0.14 R).
  B + 4H trades only in up regimes and loses there.
- **Session:** B is positive in New York (21 trades, +0.05 R) and negative elsewhere; B + 4H is positive in
  London only (4 trades).
- **VA width, distance from POC, profile volume terciles:** no monotonic pattern, and every tercile is negative
  except one 15-trade cell.
- **3,000 account:** same trades, 0 minimum-size rejections.

**Decision (pre-declared rules):**
- **Development candidates:** none (all five checks fail for every variant).
- **Volume adds information:** not shown for any variant.

## 4. BTC/USDT independent earlier-regime robustness test, 2017-09 → 2018-12
Pre-registered before download in `research/forward_test/vp_btc_2017_2018_preregistration.json` (commit
`3ee4a21`):
- **Label:** independent earlier-market-regime robustness test, **not** chronological forward validation.
- **Run:** a single run of the frozen most-selected development settings.
- **Pre-declared consequence:** because no variant passed development, this test cannot lead to a shadow test.
  It can only show whether the conclusion is regime-specific.

### Data (pre-registered rules applied)
**Sources and checksums:**
- **Trades:** 17 monthly aggTrades archives with 82,541,550 aggregate trades covering 91,611,608 trades
  (2017-08-17 04:00 → 2018-12-31). Every archive matched its published SHA-256.
- **Candles:** the official 1m kline archives, also checksum-verified, used for the cross-check.

**Integrity:**
- **Aggregate ids:** contiguous, with 0 gaps and 0 duplicates.
- **Outage months:** 33 underlying trade-id discontinuities, 22 invalid rows (excluded) and 9 timestamp
  reversals, all in December 2017 and January 2018. Those months contain Binance's outages.
- **Gaps:** 978 no-trade gaps longer than 5 minutes are listed in the manifest and never filled.
  - **Thin early trading:** 957 of them fall in Aug–Oct 2017, the first months of the listing.
  - **Exchange outages / maintenance:** the largest are 2018-02-08 (2,011 min), 2018-06-26 (600),
    2018-07-04 (457), 2017-09-06 (420), 2018-11-14 (420) and 2018-10-19 (210).
- **Resources:** 2017–2018 download had 0 memory pauses, with a minimum of 2.32 GB free.

**Kline archive defect (deviation from the pre-registration):**
- **What:** after the 2017-12-04 06:00 UTC outage, Binance's 1m kline archive stamps candles at hh:mm:20.799. That
  covers 20,401 candles to 2017-12-18, plus 1,201 candles on 2018-02-09/10.
- **Cause and decision:** the existing loader fails closed on such rows. The 1m bars were therefore built from
  the verified trades themselves (clock-aligned; minutes without trades absent; `research.vp_data btc-bars`),
  instead of from the kline archive named in the pre-registration. This was decided after the data-quality check
  and **before any strategy result for this period was computed**. Parameters, rules and criteria are unchanged.
- **Cross-check:** klines were used only for the cross-check, with the misaligned rows excluded and recorded.

**Cross-check result** (pre-registered rule: ≥ 99.9% of minutes):
- **Volume:** per-minute trade volume equals kline volume on 99.993% of 672,951 aligned minutes.
- **Mismatched minutes:** in all 48, the klines report less volume than the trades contain; the largest is
  2018-03-04 14:37–14:57, where the klines show zero volume.
- **OHLC:** matches on 99.994–99.997% of minutes.
- **Internal flag:** the stricter internal flag (which requires zero invalid rows) is false because of the
  22 excluded rows.

**Profiles:**
- **Coverage:** 493 of 502 day profiles met coverage.
- **Volume vs time-at-price:** volume POC equals TPO POC on only 8% of days, with VAH 17 bp away on median.
  Volume levels are distinct from time-at-price levels.

### Results (single run of frozen parameters; base costs; 10,000 account; long-only)
| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Beats random (random median R) | TPO-level R | Range-level R | Stress net % | Low-fee net % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 273 | −22.5 | −0.18 (−0.33, −0.04) | +0.10 | 0.28 | 20/20 (−0.30) | −0.20 | −0.33 | −34.5 | −17.1 |
| A + 4H | 49 | −2.7 | −0.11 (−0.40, +0.19) | +0.14 | 0.25 | 17/20 (−0.23) | −0.02 | −0.20 | −5.0 | −1.8 |
| B | 117 | −9.2 | −0.16 (−0.34, +0.01) | −0.00 | 0.16 | 5/20 (−0.12) | −0.14 | −0.19 | −6.6 | −6.6 |
| B + 4H | 75 | −6.4 | −0.18 (−0.40, +0.07) | +0.09 | 0.27 | 18/20 (−0.27) | −0.23 | −0.27 | −5.7 | −4.8 |

**Pre-registered checks:**
- **Mean R:** no variant has a CI lower bound above 0.
- **Stress costs:** no variant is net positive.
- **Random entries:** A and B + 4H beat ≥ 18/20 random seeds. The random entries themselves lost heavily in
  this long-only, mostly falling market, so beating them is not the same as making money.
- **Outcome:** **no variant passes.**

**Volume vs controls:** no difference has a CI excluding zero. The largest is A volume − range at +0.15
(−0.04, +0.34).

**Regimes:**
- **Profitable cells:** the only positive cells are small: A in up-high-volatility (56 trades, +0.09 R) and
  B + 4H in up-low-volatility (44 trades, +0.08 R).
- **Years:** both years are negative for A, B and B + 4H. A + 4H is +0.01 R in 2018 and −0.34 R in 2017.

**Drawdown:**
- **Max drawdown:** 4–25%.
- **Underwater periods:** 379–469 days.
- **Profitable months:** 2/16 to 7/15.

**Interpretation:**
- **Before costs:** in this earlier regime the rejection and 4H variants had small positive gross results
  (+0.09 to +0.14 R). That is somewhat better than 2024–2026, but nowhere near the ~0.25 R cost of a 0.1% taker
  fee each side.
- **Cheaper fees:** even at a 0.075% fee every variant stays negative.
- **Consistency:** the conclusion from development holds in an independent regime.

## 5. Conclusions
1. **Gold:** the broker supplies no traded volume, and its quote-activity profile is close to time-at-price. The
   value-area rules lost money on development data, with gross R ≤ 0, and on the 2025–2026 validation.
2. **BTC:**
   - Measured exchange volume gives levels that really differ from time-at-price levels.
   - Rejection and breakout-retest at those levels have roughly zero gross edge, and the 0.1% taker fee makes
     every variant negative.
   - Volume levels do no better than TPO or prior-range levels.
   - The best grid cells are consistent with selection noise (deflated Sharpe ≤ 0.17).
3. **4H confirmation** reduces trade count. It does not make any variant pass in either BTC period.
4. **2017–2018:** the independent earlier-regime test agrees. Gross R is at most +0.14 against about 0.25 R of costs, and nothing passes.
5. **Shadow test:** none is justified. Any future Volume Profile idea, such as maker execution to remove the fee
   or order-flow (delta) features, would be a new hypothesis needing its own pre-registration and prospective
   data.

## Reproduce
    python -m research.vp_data btc --first 2024-06 --last 2026-05 --base-bin 5 --output data/processed/vp/btc_2024-06_2026-05 --background --health-cmd "<runner gate>"
    python -m research.vp_data btc-validate --bins data/processed/vp/btc_2024-06_2026-05 --klines data/raw/binance_btcusdt_1m_2023-01_2026-11.csv
    python -m research.vp_data xau --start 2019-01-01 --end 2021-01-01 --base-bin 0.10 --output data/processed/vp/xau_2019_2020
    python -m research.vp_data xau --start 2025-05-26 --end 2026-11-07 --base-bin 0.10 --output data/processed/vp/xau_2025_2026 --bars-output data/raw/mt5_xauusd_1m_ticks_2025_2026.csv
    python -m research.vp_research dev --dataset xau_dev | btc_dev
    python -m research.vp_research frozen --dataset xau_val --from xau_dev
    python -m research.vp_research frozen --dataset btc_final --prereg research/forward_test/vp_btc_2017_2018_preregistration.json

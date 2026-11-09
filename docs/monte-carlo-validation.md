# Kronos Monte Carlo validation (research only)

Branch `research/monte-carlo-validation`, stacked on PR #24. Code is in `research/montecarlo/`, the configuration
in `research/configs/montecarlo_v1.json`, and the tests in `tests/test_montecarlo.py`.

**What it is:**
- a reproducible way to study path risk (drawdown, losing streaks, recovery, threshold crossings) and sensitivity
  (ordering, dependence, regime mix, execution costs, position sizing);
- built **only from recorded, closed, net-of-cost trades**.

**What it is not:**
- It does not change signals, shadow runners, trading permissions or risk controls.
- It cannot create evidence the trades do not contain.
- It cannot override a failed historical validation.

## 1. Data integrity

**Records.** A dataset is one market × one strategy × one partition × one simulated book:
- `development`, `historical_holdout`, `forward` or `synthetic_fixture` (tests only; the engine refuses it);
- one book only, so the same signal is never counted twice across alternative equity or cost books.

**Validation.** Anything that fails is refused, never repaired. The checks are:
- closed trades only;
- no duplicate trade or signal;
- `net = gross − costs`;
- `R = net / planned risk`;
- every trade inside the declared partition period;
- each timestamp carries a time zone;
- the ledger trade count equals the report summary.

Warm-up signals, candidate signals, rejections and open positions are never trades. The forward loader also
refuses any trade whose signal is warm-up or predates the pre-registered boundary.

**Provenance**, stored in every report:
- the source file and its SHA-256, plus the research config and dataset hashes;
- the git commit where the source recorded it;
- the holdout span, the selected parameters, and the book (equity and commission scenario);
- the cost model, flagged as modelled costs and not broker fills;
- a dataset fingerprint (SHA-256 of the canonical records), so any later edit is detected.

**Regimes:**
- The rule is the research code's own `trading.evaluation.regime_labels`: EMA200 trend × ATR(14)% against its rolling
  500-bar median. It is causal and keyed at the trade's opening bar close.
- It is evaluated on the **hash-verified dataset** that produced the trades.
- Labels are used only if they reproduce the source report's per-regime trade counts and net P&L exactly. All 6
  complete datasets reproduce them; otherwise regime methods would be omitted.

**Available records (2026-11-09):**

| Market | Strategy | Partition | Closed trades | Status |
| --- | --- | --- | --- | --- |
| XAUUSD | NY ORB (same parameters as the frozen forward test) | historical holdout 2025-12-10 → 2026-11-06 | 32 | complete |
| XAUUSD | London ORB · GoldPilot · order-block retest | historical holdout | 81 · 151 · 75 | complete |
| XAUUSD | liquidity sweep | historical holdout | 20 | insufficient evidence |
| BTC/USDT | volume breakout (not the frozen Donchian) | historical holdout 2026-05 → 2026-11 | 31 | complete |
| BTC/USDT | Kronos forecast | historical holdout | 247 | complete |
| BTC/USDT | trend following | historical holdout | 12 | insufficient evidence |
| BTC/USDT | **Donchian (frozen)** | historical | — | **no trade records**: Phase 2E kept summary statistics only; regenerating the trades means re-running the walk-forward research (deferred) |
| XAUUSD | **Strategy A (frozen)** | historical | — | **no trade records**: the MTF report kept summaries only |
| all three frozen strategies | | forward | 0 | insufficient evidence (forward tests began 2026-11-09, or begin 2026-11-16 for Strategy A) |

All historical holdouts were **examined** during research and informed the Phase 2C pre-registration. They are
historical evidence, not untouched forward evidence, and every report says so.

## 2. Methods

These can be configured independently:

| Method | What is resampled | Preserves |
| --- | --- | --- |
| A. Trade resampling (`iid`) | net R of individual trades, with replacement | outcome distribution; breaks ordering |
| B. Block bootstrap (`trade_block`) | circular blocks of L consecutive trades (L = 2, 4, 8) | serial dependence within blocks |
| B'. Calendar bootstrap (`calendar_block`) | whole months or weeks, **including periods without trades** | calendar horizon, clustering, trade count varies by path |
| C. Regime-stratified / regime-conditional | trades within the observed regime of each slot; or only one regime ("if every trade came from X") | regime mix; conditional outcomes (regimes with ≥ 10 trades only) |
| D. Adverse execution | recorded trades with **assumed** extra spread, slippage, latency and commission | — (cost sensitivity) |

**Adverse scenarios.** These are assumptions, labelled as such everywhere:
- **XAUUSD:**
  - +0.20 spread (recorded median 0.07, p99 0.33);
  - +0.10 slippage and +0.05 latency, each per side;
  - +7 USD/lot round-trip commission;
  - contract size 100 oz, from the report's broker specification.
- **BTC/USDT:** +5 bp of notional per side.

## 3. Path metrics (definitions)

**Equity and sizing:**
- Equity starts at 1. Trade j: `pnl = R_j × f_j × equity`, with `f_j` decided before the trade.
- Sizing is idealised: risk is exactly `f × equity`, with no lot rounding and no minimum-lot rejections.

**Drawdown and streaks:**
- Drawdown = 1 − equity ÷ running peak. Max drawdown is its maximum over the path.
- Losing streak = consecutive taken trades with R < 0.

**Recovery and time underwater:**
- An **underwater spell** is the run of trades while equity is below its running peak. It is *recovered* when equity
  regains that peak.
- Spells still open at the end are **censored**. They are reported separately (share of censored paths and open
  spell length), never as recovered. Durations are counted in trades.
- Time underwater = share of taken trades after which equity was below its peak.

**Thresholds:**
- **Daily-loss threshold (simulated):** after any trade, the loss since that day's start equity is ≥ 1.5%. The rest
  of that day's trades are then skipped.
  - In trade-level methods, resampled trades occupy the observed trade slots and their dates.
  - In calendar methods, trades keep their own days.
  - This is **not** the operational circuit breaker, which also acts on open positions, quotes and other books.
- **Drawdown thresholds:** 2, 3, 5, 8 and 10%. **Equity-loss thresholds:** start − 5% and start − 10%.

**Outputs.** Every distribution reports the 5th, 25th, 50th, 75th and 95th percentiles, plus histograms.

## 4. Sizing: fixed versus adaptive (research only)

| Policy | Rule (causal) | Fraction |
| --- | --- | --- |
| fixed (operational) | — | 0.5% |
| drawdown step | drawdown at decision ≥ 3% | 0.25%, else 0.5% |
| losing-streak step | ≥ 3 consecutive losses | 0.25%, else 0.5% |
| volatility regime | verified high-volatility regime at signal time | 0.25%, else 0.5% |

Guarantees:
- No policy can exceed 0.5%; the code refuses it, and each batch asserts it.
- Policies only reduce risk: there is no martingale and no loss recovery.
- The daily halt rule applies to every policy.
- The operational sizing policy is **not** changed by any result.

## 5. Reproducibility, uncertainty, selection

**Reproducibility:**
- Draws use numpy PCG64 with `SeedSequence([seed, crc32(variant key)])`, spawned per batch of 1,000.
- Results are identical for the same data, code, configuration and seed, regardless of the worker count or a resume
  from a checkpoint.
- Verified on the real NY ORB dataset: result hash `2486bc7e…`, identical from scratch with 1 and 2 workers and
  equal to the saved report.

**Simulation error vs strategy uncertainty (reported separately):**
- **Monte Carlo error** (`mc_se`) is simulation noise only. Binomial standard errors cover probabilities; batch means
  cover the median, 5th and 95th percentiles.
- **Strategy uncertainty** covers:
  - number of closed trades, and weeks or months with trades;
  - effective sample size from lag-1 autocorrelation;
  - overlapping trades;
  - bootstrap 95% CI of mean R;
  - concentration: share of gains from the top 1 and top 5 trades, net R without the best 3 trades, net R without
    the best month;
  - regime coverage and session mix.

**Selection bias:**
- Every report states how many strategies and configurations the source compared, and gives a Bonferroni-adjusted CI
  of mean R. For example, the Phase 2 XAUUSD report compared 5 strategies.
- Monte Carlo is never used to choose between configurations, and no forward-test result is used for tuning.

**Insufficient evidence:**
- Fewer than 30 closed trades, or fewer than 6 months with trades, gives status `insufficient_evidence`: no
  distribution is produced.
- The 10,000-scenario count is never used to manufacture apparent precision.

## 6. Results (10,000 seeded scenarios per variant, seed 20261109)

Historical holdout, fixed 0.5%, recorded costs, trade resampling. Calendar and block methods give similar medians
with wider tails. All figures are idealised-sizing simulations of examined history.

| Dataset | Trades (n_eff) | Mean R [95% CI] | Median return | 5th / 95th pct return | Median / 95th pct max DD | P(DD ≥ 5%) | Stress-cost median |
| --- | --- | --- | --- | --- | --- | --- | --- |
| XAU NY ORB | 32 (32.0) | +0.095 [−0.276, +0.480] | +1.51% | −3.45% / +6.76% | 2.48% / 5.18% | 6.0% | +0.76% |
| XAU London ORB | 81 (63.8) | −0.181 [−0.400, +0.054] | −7.25% | −14.0% / +0.5% | 9.1% / 15.0% | 90.3% | −9.14% |
| XAU GoldPilot | 151 (151) | −0.112 [−0.326, +0.109] | −8.46% | −20.1% / +5.0% | 12.9% / 22.1% | 98.7% | −11.14% |
| XAU order block | 75 (75) | −0.068 [−0.376, +0.243] | −2.70% | −12.0% / +7.7% | 7.2% / 13.4% | 79.5% | −4.84% |
| BTC volume breakout | 31 (14.2) | +0.075 [−0.348, +0.519] | +1.12% | −4.35% / +7.27% | 3.0% / 6.1% | 13.3% | +0.01% |
| BTC Kronos forecast | 247 (235) | −0.182 [−0.229, −0.132] | −19.7% | −23.3% / −15.6% | 20.0% / 23.4% | 100% | −27.0% |

**What this supports:**
- **NY ORB** (the frozen forward candidate) has a positive point estimate, but the 95% CI of mean R spans zero widely,
  and so does the selection-adjusted CI (−0.38 to +0.58).
  - Without its best 3 trades, net R is −1.3.
  - Stress costs halve the median return.
  - 84% of paths end below their peak (censored).
  - This is consistent with "unproven". It is not evidence of an edge.
- **Kronos forecast (BTC)** is reliably negative: the CI excludes zero after adjustment. Its paths cross the
  simulated 1.5% daily threshold in 94% of iid scenarios, because it trades several times a day.
- **Adaptive sizing** consistently lowers tail drawdown when the sample is negative or flat (e.g. GoldPilot: 95th
  percentile DD 22.1% → 13.5% with the drawdown step).
  - It cannot create returns.
  - For NY ORB, the volatility policy *hurt* (median +1.51% → −0.07%) because its high-volatility trades were the
    winners in this small sample.
  - The adaptive policies are research only.
- **Daily-loss knife edge:** with 0.5% risk, three full −1 R losses on one day lose 1.49%. So crossings are driven by
  losses slightly beyond 1 R (costs and gaps), as the stress scenarios show (e.g. GoldPilot calendar method 0% → 99.8%).

**Not supported:** any statement about the frozen Donchian or Strategy A (no trade records), or about any forward
test (0 closed trades).

## 7. Resources and operation

**Operation:**
- `python -m research.montecarlo check` checks the datasets and memory.
- `python -m research.montecarlo run [--only market:strategy]` runs the study.
- Single worker by default (`max_workers` is configurable), at below-normal process priority.
- Memory is bounded by batches of 1,000 scenarios. A job is deferred when available memory is under
  `min_free_memory_mb` (400) or its estimate.
- Each completed variant is checkpointed. Creating `<output_dir>/CANCEL` stops after the current batch, and a rerun
  resumes with identical results.

**Measured 2026-11-09** (BTC Kronos, 247 trades, the largest job):
- 37 variants × 10,000 scenarios in 18 s on one core;
- peak working set 117 MB; peak private 490 MB, mostly libraries;
- the whole study took 49 s of simulation time.

**Dashboard:**
- Reports are written to `research/output/montecarlo/v1/reports` (git-ignored, versioned by algorithm, dataset and
  config fingerprints) with an `index.json`.
- The Command Center serves them read-only and never imports the engine.

## 8. Limitations
- The sample sizes are small. Tail percentiles of the 31–32 trade sets rest on a few trades.
- Idealised sizing ignores lot granularity, minimum-lot rejections and concurrent-position limits, so absolute
  figures differ from the shadow books and the reports.
- Costs are modelled, and the adverse scenarios are assumptions.
- The historical holdouts were examined before the pre-registration.
- Correlation *between* strategies is not modelled: each strategy is analysed alone, and no combined portfolio is
  simulated.
- Two frozen candidates have no trade-level history. Their evidence will come only from the forward tests.

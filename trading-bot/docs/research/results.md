# Test results

Every test of the ORB strategy: what it tested, why, what happened, and whether it worked. Paths
are relative to `trading-bot/`, except links to other docs.

**Up:** [`../README.md`](../README.md) · Strategy research and sources: [`orb.md`](orb.md) ·
How a strategy change is judged: [`orb.md`, "How results are judged"](orb.md#how-results-are-judged-from-2026-11-02)

## Adding a test

1. Add a row to the register, newest first.
2. Add the details below: date, universe, period, settings, the numbers, and what they mean.

Verdicts:

- **Pass:** it met the bar set before the run.
- **Fail:** it missed that bar, or lost money.
- **Inconclusive:** the sample is too small to tell.
- **Info:** a measurement, not a strategy test.

Strategy changes are judged only by a pre-registered walk-forward spec
(`../../CLAUDE.md`, "Research discipline").

## Register

All R figures are net per trade, after costs, unless stated.

| Date | Test | Data | Result | Verdict | Why |
|---|---|---|---|---|---|
| 2026-11-02 | Walk-forward v1: 48 variants, picked by earlier years | Store, 2017–2025 (2019–2025 out of sample) | Pick (5-min range, range stop, RelVol ≥ 10, skip quiet) +0.032R, t 1.57, 6/7 years positive | Fail | Not significant: t below 3, Deflated Sharpe 0.19 (needs 0.95). Random direction earns +0.014R, so the edge is mostly stock selection |
| 2026-11-02 | Baseline: defaults, 3 stops x 5/15/30 min | Store, 2017-01..2026-10 | Every cell negative. Range stop −0.014..−0.016R; 10%-ATR stop −0.48..−0.61R | Fail | Costs: the tight stop pays 0.34–0.51R per trade; the range stop's +0.02..+0.05R gross doesn't cover its costs |
| 2026-11-01 | Opening range 5 vs 15 vs 30 min | All stocks, 2024 and 2025 | No length profitable in both years; 15 min best with the range stop by 0.01–0.02R | Fail | The gap is within one standard error; the paper's 5-minute lead does not replicate |
| 2026-11-01 | Stock subsets (features known before the trade) | Range-stop trades, 2024–2025 | Per-stock results don't persist (rank correlation 0.03). Skipping quiet setups is the one consistent subset, about breakeven | Inconclusive | Found using both years; needed fresh data, so it went into walk-forward v1 |
| 2026-11-01 | Practitioner filters, one at a time on 2024, combination once on 2025 | All stocks | 2024 combination +0.079R; on 2025 −0.049R | Fail | Overfit: the 2024 gain was selection luck (random directions also made +0.054R) |
| 2026-11-01 | Full universe: default and variants | All active symbols, 2024–2025 | None profitable. Best: range stop −0.033R. Paper's stop −0.67R (worst-case fills), −0.12R (optimistic) | Fail | Costs exceed any gross edge; minute bars can't measure the tight stop |
| 2026-11-01 | Smoke test | 12 mega-caps, Sep 2026 | Default −0.32R (cost 0.32R); range stop −0.006R | Inconclusive | 71 trades; showed costs dominate the paper's stop |
| 2026-11-01 | Quoted spreads at 09:35 and 15:50 | 12 mega-caps, 2026-09-15 | Median about 4 bps at 09:35, 2–3x the close | Info | Set the 4 bps default; Stocks in Play are often less liquid than these |

## Details

**2026-11-01: quoted spreads (SIP historical quotes, 2026-09-15, median over one minute).**

| Name | Spread at 09:35 | Spread at 15:50 |
|---|---|---|
| AAPL | 2.1 bps | 0.9 bps |
| MSFT | 5.0 bps | 1.2 bps |
| NVDA | 1.9 bps | 0.5 bps |
| TSLA | 3.9 bps | 1.4 bps |
| AMD | 10.5 bps | 4.4 bps |
| META | 6.2 bps | 3.0 bps |
| AMZN | 2.0 bps | 1.2 bps |
| GOOGL | 2.6 bps | 1.2 bps |
| NFLX | 2.6 bps | 1.3 bps |
| PLTR | 6.5 bps | 2.3 bps |
| COIN | 14.4 bps | 5.8 bps |
| SMCI | 5.4 bps | 2.8 bps |

- The median at 09:35 is about 4 bps, which supports the default `spread_bps=4`. Spreads at the
  open are 2–3× wider than near the close.
- Most ORB exits are early stop-outs, so they also pay opening-hour spreads.

**2026-11-01: smoke test.** 12 mega-caps (above), 2026-09-01..2026-09-30, defaults otherwise.

| Run | Trades | Gross R | Cost R | Net R | Return |
|---|---|---|---|---|---|
| default (stop 10% ATR) | 71 | −0.006 | 0.316 | −0.322 | −0.6% |
| `--zero-costs` | 71 | −0.006 | 0 | −0.006 | +0.7% |
| `--stop-atr 0.25` | 71 | +0.039 | 0.120 | −0.082 | +1.2% |
| `--stop-mode range` | 71 | +0.056 | 0.062 | −0.006 | +3.0% |
| `--direction random` | 52 | −0.229 | 0.312 | −0.541 | −1.1% |
| `--direction inverse` | 39 | −0.768 | 0.337 | −1.105 | −2.7% |

- **What it shows:** with a 10%-of-ATR stop, costs are about 0.3R per trade, far above any edge
  reported in the literature. Widening the stop cuts cost per trade sharply.
- **What it does not show:** anything statistically meaningful.
  - The sample is tiny (one month, 71 trades).
  - Mega-caps are rarely Stocks in Play.
  - Dollar returns and average R differ because the leverage cap shrinks many positions.
- **Next:** run `--universe active` over multiple years, then compare stop widths and placebo
  directions on that.

**2026-11-01: full universe, 2024-01-02..2025-12-31 (502 days).** `--universe active` (13,189
symbols, ETFs included, survivorship-biased), SIP, $25,000 start, defaults otherwise. The first
run took 35 minutes and 4,490 requests; each variant then ran from the cache in about 2 minutes.
"Worst case" is the default fill assumption: if the entry minute also reaches the stop, the trade
is stopped out. `--optimistic-intrabar` checks the stop from the next minute instead.

| Run | Intrabar | Trades | Win | Gross R | Cost R | Net R | $25k → | Max DD |
|---|---|---|---|---|---|---|---|---|
| default (stop 10% ATR) | worst case | 7,737 | 8.2% | −0.309 | 0.358 | −0.667 | $601 | 97.6% |
| `--zero-costs` | worst case | 7,956 | 8.4% | −0.312 | 0 | −0.312 | $2,801 | 89.4% |
| `--direction random` | worst case | 6,373 | 9.3% | −0.331 | 0.359 | −0.690 | $943 | 96.3% |
| `--direction inverse` | worst case | 5,020 | 10.9% | −0.360 | 0.359 | −0.719 | $1,304 | 94.8% |
| `--stop-atr 0.25` | worst case | 7,933 | 23.4% | −0.042 | 0.138 | −0.180 | $1,723 | 93.6% |
| `--stop-mode range` | worst case | 7,965 | 41.7% | +0.040 | 0.073 | −0.033 | $15,931 | 51.9% |
| default (stop 10% ATR) | optimistic | 7,965 | 12.8% | +0.236 | 0.356 | −0.120 | $21,335 | 44.1% |
| `--direction random` | optimistic | 6,428 | 13.8% | +0.169 | 0.356 | −0.187 | $16,023 | 57.2% |
| `--direction inverse` | optimistic | 5,038 | 15.0% | +0.032 | 0.356 | −0.324 | $7,704 | 69.4% |
| `--stop-mode range` | optimistic | 7,965 | 41.7% | +0.039 | 0.073 | −0.034 | $15,911 | 52.0% |

SPY returned +43.5% over the same period. Outputs are in `backtests/` and `backtests/v-*/`.

- **No variant is profitable after costs.** The best, the range stop, loses 0.03R per trade and
  36% of equity.
- **The 10%-of-ATR stop is too tight to backtest on minute bars.** The median stop is 0.34% of
  price. 53% of default trades are stopped out in their entry minute under the worst-case
  assumption. Gross R swings from −0.31 (worst case) to +0.24 (optimistic) on that one
  assumption, so minute bars cannot say whether the paper's setup has a gross edge.
- **Even the optimistic bound loses after costs.** It costs 0.36R per trade against at most
  0.24R of gross edge. The optimistic bound is also inflated: the random-direction placebo earns
  +0.17R gross under it.
- **The opening direction carries a small edge.** Optimistic gross R is +0.24 with the opening
  direction, +0.17 random and +0.03 inverse. That is about 0.07R over random, a fifth of the
  cost per trade.
- **The range stop is robust to the fill assumption** (+0.040 vs +0.039 gross), because a wide
  stop is rarely reached inside the entry minute. Its cost is only 0.07R, but its gross edge is
  also small.
- **Exits:** 91% of default trades end at the stop. The 9% held to the close average +6.9R
  gross, which matches the papers' "few large winners" profile.
- **Not yet tested:** stocks only (no ETFs), a delisted-inclusive universe, and tick-level
  resolution of the entry minute (Alpaca historical trades) to replace the two bounds with one
  number.

**2026-11-01: practitioner optimizations, developed on 2024, tested once on 2025.**

*What the web says.* Searched for how traders optimize ORB (sources in [`orb.md`](orb.md#sources), under
"Optimization research"). Almost all suggestions come from TradingView scripts and vendor
blogs with no published results: VWAP and EMA trend filters, breakout-bar volume, candle-close
confirmation, opening-range width limits (0.35% of price; 0.3–2.5× ATR), gap and catalyst
filters for Stocks in Play (gap 2–5%), breakeven and trailing stops, and early flattening.
The QuantConnect replication thread adds that a unit-less ATR filter (ATR > 1% of price) kept
Sharpe above 2 in 2016, a price filter above $50 and momentum filters hurt, and the results
cover single years. The 2026 studies are skeptical: a pre-registered 225-configuration futures
study (Fetna) found no gross edge and no survival after costs, and a search-aware replication
(Subhani) found deflated Sharpe ratios of 0.42–0.78.

*Method.* Universe `--universe stocks` (7,130 symbols, no ETFs), `--stop-mode range` as the
baseline (it is robust to the intrabar assumption), worst-case fills, default costs. Each
option was changed alone on 2024. Rule fixed in advance: combine the options that beat the
baseline in both halves of 2024, then run the combination once on 2025. A VWAP entry filter was
not tested: at a first breakout of the range, price is always on the right side of VWAP.

*2024 (development).* Net R per trade; baseline −0.030 ± 0.018 (standard error).

| Option | Trades | Net R | ± SE | Both halves better? |
|---|---|---|---|---|
| `--min-relvol 10` | 814 | +0.064 | 0.046 | yes |
| `--min-range-pct 0.02` | 3,914 | +0.001 | 0.016 | yes |
| `--entry-window 15` | 3,100 | −0.009 | 0.022 | yes |
| `--min-gap 0.02` | 3,962 | −0.010 | 0.018 | no |
| `--gap-aligned` | 3,962 | −0.020 | 0.018 | no |
| `--min-range-atr 0.25` | 3,961 | −0.020 | 0.017 | no |
| `--entry-window 30` | 3,393 | −0.025 | 0.021 | no |
| `--min-atr-pct 0.01` | 3,975 | −0.031 | 0.018 | no (filters almost nothing) |
| `--entry-mode close` | 3,701 | −0.032 | 0.018 | no |
| `--min-relvol 3` | 3,627 | −0.033 | 0.019 | no |
| `--breakeven-r 1` / `2` | 3,974 | −0.035 | 0.017 | no |
| `--vwap-exit` | 3,974 | −0.045 | 0.012 | no |
| `--direction random` (placebo) | 3,180 | −0.049 | 0.019 | no |
| `--stop-mode atr` (paper stop) | 3,972 | −0.790 | 0.043 | no |

*Combination* (`--min-relvol 10 --min-range-pct 0.02 --entry-window 15`):

| Run | Trades | Net R | ± SE | Return | Max DD |
|---|---|---|---|---|---|
| 2024 combination | 519 | +0.079 | 0.046 | +26.8% | 10.5% |
| 2024 combination, random direction | 347 | +0.054 | 0.054 | +5.3% | 10.9% |
| **2025 combination (holdout)** | 611 | **−0.049** | 0.038 | **−16.6%** | 29.4% |
| 2025 combination, random direction | 402 | +0.037 | 0.058 | +14.2% | 11.4% |
| 2025 baseline | 3,925 | −0.006 | 0.022 | −49.7% | 61.5% |

SPY returned +16.4% in 2025.

- **The combination failed out of sample.** It lost 0.049R per trade in 2025, worse than the
  baseline and worse than its own random-direction placebo.
- **The 2024 gain was mostly selection luck, not direction.** In 2024 the random-direction
  version of the combination also earned +0.054R.
- **The width pattern reversed.** Net R by opening-range width (stop as % of price):

  | Width | 2024 | 2025 |
  |---|---|---|
  | 0–1% | −0.155 | −0.098 |
  | 1–2% | −0.122 | +0.135 |
  | 2–3% | +0.007 | +0.009 |
  | 3–5% | +0.051 | −0.047 |
  | over 5% | +0.035 | −0.040 |

- **Dollar results follow the wide ranges.** With the range stop and the leverage cap, a
  wide-range trade risks 5–8× the dollars of a narrow one (about $230 vs $31). So the 2025
  baseline lost 49.7% while its average R was nearly zero: its wide-range trades lost.
  Average R and dollar P&L will disagree until risk per trade is made more uniform.
- **One year of development data is too little** for filters that cut trades to ~500 a year
  (standard error ≈ 0.05R, larger than any effect found). 2025 has now been used as a holdout;
  re-tuning on it would no longer be an honest test.

**2026-11-01: does ORB work better on a subset of stocks?** Trades from the full-universe
range-stop run (`backtests/v-stop-range/`, 7,965 trades, 2024–2025, worst-case fills, default
costs), each tagged with features known before the trade. A subset counts only if 2024 and 2025
agree.

*What the literature says.* The Stocks in Play paper's best tickers (cumulative R, in sample)
are high-volume, volatile, retail-popular names (DDD, FSLR, NVDA, TSLA, AMD); its worst are
mostly energy, industrials and utilities (CMC, TRGP, CSX, CNP, WMB, PSX). Gao et al. (2018)
find intraday momentum stronger on volatile, high-volume and news days, and similar across
liquid and illiquid stocks. A Swedish study finds intraday momentum "not robust across most
individual stocks". Practitioners agree on a catalyst and unusual volume but disagree on
float and market cap. No source tests whether a subset persists out of sample.

*Results.*

- **Stocks do not keep their ORB performance from year to year.** Of 357 symbols with at least
  3 trades in both years, the rank correlation of 2024 vs 2025 average net R is **0.03**. The
  best fifth in 2024 (+0.69R) averaged −0.02R in 2025; the worst fifth (−0.74R) averaged −0.03R.
  A ticker list like the paper's best performers would not have helped.
- **The one consistent subset: skip quiet setups.** The bottom fifth by ATR (below 2.1% of
  price), by overnight gap (below 0.66%) or by opening-range width (below 1.18% of price) is
  the worst bucket in both years. Mostly because of costs:

  | Subset | 2024 trades | Gross R | Cost R | Net R (± SE) | 2025 trades | Gross R | Cost R | Net R (± SE) |
  |---|---|---|---|---|---|---|---|---|
  | Any of the three quiet signs | 1,674 | −0.029 | 0.137 | −0.165 ± 0.034 | 1,317 | +0.124 | 0.153 | −0.029 ± 0.048 |
  | The rest | 2,342 | +0.036 | 0.032 | +0.004 ± 0.022 | 2,632 | +0.045 | 0.029 | +0.016 ± 0.028 |

  The rest is about breakeven after costs in both years, not clearly profitable. This rule was
  found using both years, so it still needs a test on data not yet used (2020–2023 or 2026). It
  maps to existing flags: `--min-atr-pct 0.021 --min-gap 0.0066 --min-range-pct 0.0118`.
- **Inconsistent between years (no usable subset):** relative volume (2024: the higher the
  better; 2025: the reverse, top fifth −0.088R), ETFs vs stocks, price, dollar volume, long vs
  short, trading with or against the 20-day trend, the gap's direction, and trading with or
  against SPY's opening candle.

**2026-11-01: 5- vs 15- vs 30-minute opening ranges.** `--universe stocks --or-minutes 5,15,30`,
each year run separately, default costs. One download of the 30-minute window serves all three
lengths. RelVol is measured over the same window as the range. Outputs:
`backtests/or-compare/<year>-<stop>/`.

Net R per trade (equal-weight = capital split across the three lengths, no rebalancing):

| Stop / fills | Year | 5 min | 15 min | 30 min | Equal-weight return |
|---|---|---|---|---|---|
| Range, worst case | 2024 | −0.030 | −0.015 | −0.014 | +24.7% (max DD 15.2%) |
| Range, worst case | 2025 | −0.006 | **+0.012** | −0.008 | −26.9% (max DD 40.4%) |
| 10% ATR, worst case | 2024 | −0.790 | −0.660 | −0.444 | −75.3% |
| 10% ATR, worst case | 2025 | −0.581 | −0.694 | −0.571 | −87.1% |
| 10% ATR, optimistic | 2024 | −0.220 | −0.183 | −0.108 | +5.3% |
| 10% ATR, optimistic | 2025 | −0.016 | −0.162 | −0.218 | −33.6% |

Dollar returns for the range stop, 5/15/30 min: 2024 +13.6% / +52.6% / +7.9%; 2025 −49.7% /
−4.4% / −26.7%.

- **The paper's ranking (5 minutes far ahead: Sharpe 2.81 vs 1.43 vs 0.21) does not replicate.**
  No length is profitable after costs in both years.
- **With the range stop, 15 minutes is best in both years**, mainly through costs: a wider range
  means a wider stop, so cost per trade falls (2024: 0.053R → 0.039R → 0.032R). Gross R barely
  changes. The gap (0.01–0.02R) is within one standard error (about 0.02R), so this is a lead,
  not a finding.
- **With the paper's stop, the ranking flips between years** (optimistic gross R: 2024 rises
  with length, 0.14 → 0.25; 2025 falls, 0.29 → 0.09). Costs (0.30–0.37R) exceed every gross
  result.
- **Combining the three lengths smooths the equity curve** (2024 max drawdown 15% vs 20–24% for
  single lengths), as the paper reports, but it cannot turn three losing sleeves into a winner.

**2026-11-02: baseline, 2017-01-03..2026-10-30.** Downloaded store (7,149 symbols, survivorship-
biased), defaults otherwise, each year from $25k. Outputs: `backtests/baseline-2017-2026/`.
Net R per trade, all years pooled (33k–40k trades per cell):

| Stop / fills | 5 min | 15 min | 30 min | Years positive (5 min) |
|---|---|---|---|---|
| 10% ATR, worst case (default) | −0.607 | −0.574 | −0.477 | 0/10 |
| 10% ATR, optimistic | −0.109 | −0.191 | −0.192 | 3/10 |
| Range, worst case | −0.014 | −0.016 | −0.015 | 2/10 |

- The 10%-ATR stop costs 0.34–0.51R per trade, more than its best-case gross (+0.16..+0.23R).
- The range stop grosses +0.02..+0.05R and costs 0.03–0.06R. Years not used before (2017–2021)
  match 2022–2025 within 0.002R.

**2026-11-02: walk-forward v1 FAILED** (`walkforward-orb-v1.toml`, report in
`backtests/walkforward/orb-wf-v1/20261102-114805/`). 2026 not used.

- Every year the rule picked the same variant: 5 minutes, range stop, RelVol ≥ 10, quiet setups
  skipped.
- Out of sample 2019–2025: 3,648 trades, gross +0.060, cost 0.027, **net +0.032R** (day-clustered
  t 1.57, 95% CI −0.008..+0.073), 6 of 7 years positive, +0.005R with costs doubled, PBO 0.00.
- Failed: t (needs 3) and Deflated Sharpe (0.19; needs 0.95; 67 trials).
- The random-direction placebo earns +0.014R, so the opening direction adds only about 0.02R.
  Most of the result comes from which stocks are traded.
- Every 10%-ATR variant lost 0.37–0.83R per trade.
- At this trade count, t ≥ 3 needs about +0.06R per trade.

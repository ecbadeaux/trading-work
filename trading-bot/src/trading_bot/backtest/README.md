# backtest

The opening range breakout (ORB) backtester, the `trading-bot-backtest` CLI, and the research
tools around it: the run ledger (`trading-bot-ledger`), the walk-forward runner
(`trading-bot-walkforward`) and the tick downloader for tick fills (`trading-bot-ticks`). It
reads market data only and never places orders. Paths are relative to this folder
(`trading-bot/src/trading_bot/backtest/`).

**Up:** [`../README.md`](../README.md) · Strategy rules are in
[`../strategies/README.md`](../strategies/README.md), the research in
`../../../docs/research/orb.md` and every test's result in `../../../docs/research/results.md`.

## Running

From `trading-bot/` with the venv active (paper keys are fine):

```sh
trading-bot-backtest --start 2026-09-01 --end 2026-09-30 \
  --symbols AAPL,MSFT,NVDA --env-file ~/.config/trading-bot/.env
trading-bot-backtest --start 2024-01-02 --end 2025-12-31 --universe active \
  --env-file ~/.config/trading-bot/.env          # the paper's setup; slow the first time
trading-bot-backtest --start 2024-01-02 --end 2024-12-31 --universe stocks \
  --or-minutes 5,15,30 --env-file ~/.config/trading-bot/.env   # compare range lengths
trading-bot-backtest --help                      # every option, with defaults
```

**Comparing opening-range lengths.** `--or-minutes 5,15,30` runs the same settings once per
length and adds an equal-weight combination (capital split equally, no rebalancing, as in the
paper's combined portfolio). All lengths run in one pass over the data (`run_backtests`), which
fetches only the longest window and cuts the shorter ranges out of it.

**Universe** (pick one):

- `--symbols A,B,...`
- `--symbols-file f` (one per line, `#` comments)
- `--universe active` (today's listed stocks, so survivorship bias)
- `--universe stocks` (the same without ETFs, ETNs and funds; a name/exchange heuristic, see
  `../data/README.md`)
- `--universe store` (the symbols saved in `--store` by `trading-bot-download`, plus its ETFs)
- `--universe store+delisted` (plus the delisted stocks `trading-bot-download
  --include-delisted` added; see `../data/README.md`)

**Offline data.** `--store data-store/bars.sqlite` reads bars from the local store filled by
`trading-bot-download` (see `../data/README.md`). Missing data comes from Alpaca unless
`--offline`, which never calls Alpaca and needs no API keys.
- `--max-symbols N` trims any of these for quick runs.

**Option groups** (see `--help`):

- *data:* `--feed`, `--cache-dir`, `--store`, `--offline`, `--out`, `--ledger`, `--no-ledger`,
  `--chunk-size`, `--rpm`, `--benchmark`.
- *strategy:* mirrors `OrbParams`.
- *portfolio:* `--equity`, `--risk`, `--max-leverage`, `--optimistic-intrabar`, `--seed`.
- *costs:* `--spread-bps`, `--min-spread`, `--slippage-bps`, `--stop-slippage-bps`,
  `--commission`, `--zero-costs`.
- *fills:* `--fills bars|ticks`, `--ticks`, `--tick-latency-ms`, `--tick-rpm`, `--tick-chunk`,
  `--tick-threads` (see "Tick fills").

**Useful comparisons.** Once the data is cached, each re-run takes seconds:

| Flag | Question it answers |
|---|---|
| `--zero-costs` | Does the edge exist before costs (the papers' assumption)? |
| `--direction random` / `inverse` | Placebos: does the opening direction carry an edge? |
| `--stop-atr 0.25`, `--stop-mode range` | A wider stop makes costs smaller in R |
| `--or-minutes 15`, `--min-relvol 2`, `--top-n 10`, `--vwap-exit` | Robustness of the setup |
| `--min-gap`, `--gap-aligned`, `--min-atr-pct`, `--min-range-pct`, `--min-range-atr`, `--max-range-atr` | Setup filters (off by default) |
| `--entry-mode close`, `--entry-window N`, `--breakeven-r N` | Entry confirmation, entry cutoff, breakeven stop |
| `--fills ticks` | Do the results survive real spreads and the real order of trades? |
| `--universe store+delisted` | Do they survive adding the stocks that later delisted? |

Exit codes: 0 ok, 1 error during the run, 2 bad arguments or missing keys (not needed with
`--offline`). `--end` must be before today.

## How it works

| Module | Role |
|---|---|
| `engine.py` | `run_backtests(source, universe, variants, costs, config)` (several `OrbParams` in one pass) and `run_backtest()` (one), `BacktestConfig`, `DayResult`, `BacktestResult`, the `DataSource` protocol |
| `simulator.py` | `simulate_trade()` → `Trade` (gross and net P&L, R, cost in R; with tick fills, `fill_source`, `entry_spread`, `tick_notes`) |
| `costs.py` | `CostModel` (spread, slippage, fees) |
| `ticks.py` | Tick fill rules: `elect()` (when a stop triggers), `market_fill()`, `EXCLUDED` trade conditions |
| `tickplan.py` | `trading-bot-ticks`, and the planning shared with `--fills ticks`: `plan_backtest()`, `windows_for()`, `request_groups()`, `fetch()` |
| `metrics.py` | `summarize()` → `Summary`, including the benchmark alpha/beta regression; `combine_equal_weight()` and `curve_stats()` for the combination |
| `report.py` | `write_outputs()` (CSV/JSON), `format_summary()` (console); `write_comparison()` and `format_comparison()` for several lengths; `fill_stats()` for tick fills |
| `cli.py` | `trading-bot-backtest`: argument parsing → the pieces above, then a ledger record |
| `ledger.py` | `trading-bot-ledger`: the run ledger (`strategy_key()`, `append()`, `trial_keys()`, `backfill()`) |
| `stats.py` | Research statistics on R summed per day (`Agg`, `DayAgg`): `ratio_stats()` (day/month-clustered SE), `bootstrap_ratio_ci()`, `probabilistic_sharpe()`, `deflated_sharpe()`, `pbo_cscv()` |
| `walkforward.py` | `trading-bot-walkforward`: spec parsing, the year runs (parallel, cached), selection, checks and the report |

The engine works in two passes. With several variants, pass 1 and every bar read are shared;
each variant keeps its own equity, random stream and diagnostics, so results equal separate
runs.

1. **Pass 1 (daily data only):** for each test day, compute ATR and average volume from the
   **prior** 14 sessions and keep the symbols that pass the daily filters. Symbols with a split
   anywhere in the window, including today, are skipped (`split_guard_skips`).
2. **Pass 2 (day by day):**
   - Fetch opening-range bars only for symbols some test day needs. That is, a symbol is fetched
     on day j if it is eligible on day j or on a later day whose 14-day RelVol window includes j.
   - Compute RelVol and select the top N.
   - Fetch the session's minute bars for the selected symbols and size each trade from
     start-of-day equity.
   - Simulate each trade and add the day's net P&L to equity.

**Fill model:**

- Stop entry fills at the trigger, or at the open if the bar gapped past it.
- The stop is anchored at `entry ∓ R`.
- A stop-loss fills at the stop, or at the open on a gap.
- If the entry bar also touches the stop, it counts as stopped out (turn this off with
  `--optimistic-intrabar`).
- A bar touching both stop and target counts as the stop.
- Targets fill at the limit price.
- Flatten-time and VWAP exits fill at the next bar's open.
- Close-confirmed entries fill at the open of the bar after the confirming close.
- A breakeven move applies from the bar after the one that reached it, and exits as
  `breakeven`.
- If data ends early, the trade exits at the last close.

**Cost model** (`CostModel` defaults):

- Market and stop fills cross half of a **4 bps** spread (1¢ floor) and pay **1 bp** of slippage.
- Stop fills pay **2 bps** more.
- Limit fills pay nothing extra.
- Commission is 0 (Alpaca). SEC (0.00278%) and FINRA TAF fees apply on sells.
- The 4 bps is the median quoted spread of 12 mega-caps at 09:35. Stocks in Play are often
  wider; tick fills measure it.

**Tick fills** (`--fills ticks`, `BacktestConfig.fills`). The same reference prices and stops,
but the SIP tape decides what happened inside a minute and what each fill cost. The rules follow
`us/docs/orders-at-alpaca.md`:

- **A stop elects on the first eligible trade at or through its price, inside the NBBO in force**
  (`ticks.elect`). Prints with conditions that keep them off the regular tape don't count
  (`ticks.EXCLUDED`: extended hours, out of sequence, average price, official open/close and
  so on).
- **An elected stop or market order fills at the far side of the quote** in force
  `--tick-latency-ms` (default 100) later: the ask to buy, the bid to sell. Only `slippage_bps`
  and fees are added; the modelled spread and the extra stop slippage are not used. Quote sizes
  are ignored.
- **The entry minute's trades decide** whether the stop elected after the entry, so the
  worst-case guess isn't needed. After that minute, stops are still found on the minute bars,
  then filled from that minute's tape (an odd-lot print beyond a bar's low can't trigger one).
  Flatten-time and VWAP exits fill from the quotes at the exit time.
- **Fallbacks:** a fill whose ticks are missing uses the bar model. Each trade records
  `fill_source` (`ticks`, `mixed`, `bars`) and `tick_notes`:

  | Note | Meaning |
  |---|---|
  | `missing_entry`, `missing_stop`, `missing_exit` | The window was never downloaded |
  | `entry_not_elected` | No eligible print reached the trigger in the bar's entry minute; bar fill |
  | `stop_not_elected` | The bar touched the stop, but only via non-electing prints; still counted as stopped (conservative), at the bar fill |
  | `no_entry_quote`, `no_stop_quote`, `no_exit_quote` | No valid quote in the window |

- **Which windows:** an optimistic bar pass lists each trade's entry minute and the exit it
  reaches when the stop doesn't elect in the entry minute (`tickplan.windows_for`). Those are the
  only two paths the tape can take. Windows at the same time share one multi-symbol request.
  `trading-bot-backtest --fills ticks` fetches what's missing first (not with `--offline`);
  `trading-bot-ticks SPEC` does it for a walk-forward spec.
- **Cost:** about 2–3 requests per trade, mostly one page each, so roughly 1–2 hours per 10,000
  trades at 180 requests/min. `--tick-threads` (default 4) keeps several in flight; the rate
  cap still holds.

**Sizing:**

- `shares = min(equity × risk / R, equity × max_leverage / top_n / trigger)`, so gross exposure
  never exceeds `max_leverage`.
- Trades on the same day are sized independently from start-of-day equity.

## Outputs

`--out` (default `backtests/`) gets a `<YYYYmmdd-HHMMSS>/` folder:

- **`trades.csv`:** one row per trade, with fills, exit reason, P&L, `gross_r`, `net_r`,
  `cost_r`, `fill_source` and `entry_spread_bps` (tick fills only).
- **`equity.csv`:** one row per day (start equity, gross and net P&L, trades, end equity).
- With several `--or-minutes`, each length gets its own `or<N>/` subfolder with the three files
  below, plus **`comparison.csv`** (one row per length and the combination) and
  **`combined_equity.csv`** (the combination's end-of-day equity).
- **`summary.json`:**
  - `summary`: return, CAGR, volatility, Sharpe, max drawdown, win rate, average gross/cost/net
    R, profit factor, benchmark alpha/beta;
  - `params`, `costs`, `config`;
  - `diagnostics`: counts for `days`, `candidates`, `not_triggered`, `skipped_zero_qty`,
    `split_guard_skips`, and `exit_<reason>`; with tick fills also `fills_<source>` and
    `tick_<note>`;
  - universe size and data request count;
  - with tick fills, `fill_stats` (sources, fallbacks, spread percentiles at entry) and
    `tick_misses`.

**The key number is average cost R vs gross R.** If costs eat the gross edge, nothing else
matters.

## Research tools

**Ledger.** Every `trading-bot-backtest` and `trading-bot-walkforward` run appends one line per
parameter set to `backtests/ledger.jsonl` (`--ledger PATH`, `--no-ledger`). A *trial* is a
distinct strategy key: the `OrbParams`, the `CostModel` and the fill assumption. The period,
account size and placebo runs don't make a new trial. `trading-bot-ledger` prints the count;
`trading-bot-ledger --backfill backtests` adds runs from before the ledger. Ad hoc scripts
aren't recorded, so the count is a lower bound.

**Walk-forward.** `trading-bot-walkforward SPEC` runs a pre-registered protocol, offline from
the store and without keys (spec v1: `../../../docs/research/walkforward-orb-v1.toml`):

1. Backtest every grid variant on every year, each year starting at the same equity. One
   `run_backtests` pass per year; years run in parallel (`--workers`).
2. For each test year, the spec's rule picks a variant using earlier years only (`anchored` or
   `rolling` training): the highest mean net R per trade among variants with `min_trades`,
   averaged with neighbours on numeric axes if `score = "neighborhood"` (favours plateaus).
3. Rerun each pick with optimistic fills and with random directions (`placebo_seeds`). With
   tick fills there is no optimistic bound, and the placebo runs on bar fills against the pick
   on bar fills (the placebo's trades have no tick windows), so it still compares like with
   like.
4. Judge the out-of-sample record against `[criteria]`:

| Check | How |
|---|---|
| Day-clustered t, 95% CI | Mean net R per trade, cluster-robust SE by day (same-day trades are correlated); also by month, and a day bootstrap |
| Positive test years | Share of test years with mean net R > 0 |
| Costs ×N | `gross R − N × cost R` (exact in R) |
| Placebo | Out-of-sample net R minus the random-direction reruns' |
| PBO | Combinatorially symmetric cross-validation over the grid (Bailey et al. 2015): the development days are cut into `pbo_blocks` blocks; for every half as in-sample, the rule picks and the pick is ranked out of sample |
| Deflated Sharpe | Daily-R Sharpe of the final pick (trained on every development year) vs the expected best of N unskilled trials, N = ledger trials plus the grid. Noise variance is the null 1/(T−1): the grid's own spread mixes in consistently losing variants |
| Splits (report only) | Long vs short; terciles of SPY 20-day realized volatility |

**Holdout.** The spec's `final` year is never computed without `--final`. The first `--final`
writes `<out>/<name>/holdout.json`; `--final` with any other spec (any edit changes its hash) is
refused unless `--reuse-holdout`.

**Outputs** go to `backtests/walkforward/<name>/<timestamp>/`: `report.md` (also printed),
`summary.json`, `selections.csv` (per test year), `grid.csv` (every variant × year) and
`oos_trades.csv` (the picks' trades). Year runs are cached in `<name>/cache/`, keyed by
everything that affects them (`--fresh` ignores the cache). If the store, or the tick store with
tick fills (`--ticks`), lacks data a year needs, the run stops (`--allow-missing` accepts biased
results, and missing ticks use the bar model). Exit codes: 0 ran (the verdict is in the report),
1 store incomplete, 2 bad spec or arguments.

**Ticks for a spec.** `trading-bot-ticks SPEC --env-file ...` runs the grid on bars
(optimistic) over the development years, lists the tick windows, and downloads the missing
ones. It is resumable, and `--estimate` prints the size without keys. `--variants v08,v24`
limits it to some variants; `--include-final` also covers the holdout year, without showing
results. `--workers` sets the processes for the bar runs, `--threads` the requests in flight.

**Spec format** (TOML; unknown keys are errors): `name`, `notes` (printed as caveats),
`universe` (`store`, `stocks`, or either with `+delisted`), `[years]`, `[selection]`,
`[criteria]`, `[checks]`, fixed `[params]`/`[costs]`/`[portfolio]`, `[fills]` (`mode = "bars"`
or `"ticks"`, `latency_ms`), and `[grid]`. A grid axis is an `OrbParams` field with a
list of values, or any name with a list of labelled bundles, e.g.
`setup = [{ label = "all" }, { label = "no-quiet", min_gap = 0.0066 }]`. Variants are the
product of the axes. Changing a spec after seeing results means a new version.

## Limits and extending

- **Known limits:**
  - survivorship bias with `--universe active` and `store`. `store+delisted` reduces it, but
    its coverage is partial (`../data/README.md`);
  - bar fills model the spread and guess the order inside a minute; tick fills measure both,
    but ignore quote size and queue position;
  - no daily loss limit;
  - trades within a day are not interleaved in time.
- **Speed:** from the store, a year of the full universe takes under a minute, for one variant
  or a 48-variant grid (loading the data dominates). From Alpaca: about 150 requests/min; a
  12-symbol month takes about 25 s cold, `--universe active` roughly 10–30 min per year cold.
- **New data sources** implement `DataSource` (four methods). `tests/test_engine.py::FakeSource`
  is a minimal in-memory example.

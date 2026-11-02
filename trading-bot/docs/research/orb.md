# Opening Range Breakout (ORB): research consensus

Research done 2026-11-01 from web sources (listed at the bottom). This is the design basis for
`src/trading_bot/strategies/orb.py` and the backtester in `src/trading_bot/backtest/`. Paths are
relative to `trading-bot/`.

**Up:** [`../README.md`](../README.md) · The code is documented in
[`../../src/trading_bot/strategies/README.md`](../../src/trading_bot/strategies/README.md) and
[`../../src/trading_bot/backtest/README.md`](../../src/trading_bot/backtest/README.md).

**Bottom line.** Every serious source finds a real but small directional edge after the open,
typically no more than about 0.1R per trade (R = the amount risked on the trade). Whether it
survives trading costs depends on three things:

1. **What you trade:** stocks with abnormally high opening volume ("Stocks in Play").
2. **How you exit:** a tight stop scaled to volatility, no profit target, and hold to the close.
3. **Costs relative to the stop distance.**

The headline returns (1,600%+) come from backtests that assume no slippage. Independent
replications that add realistic costs mostly end up near zero. **The backtester exists to settle
this for our own costs before any money is risked.**

## Variants studied

### Classic methods (practitioner books; evidence is anecdotal)

| Variant | Rules | Idea that survived |
|---|---|---|
| Crabel ORB (1990) | Buy and sell stops at the open ± a "stretch": the 10-day average of the smaller move from the open to the high or low. Prefers quiet days before entry (NR4, NR7, inside days). Takes only the side of a clear bias ("ORB preference"). | Trade one direction only; scale thresholds to volatility |
| Williams volatility breakout (1982) | Open ± k × yesterday's range, k ≈ 0.5 | Volatility-scaled triggers |
| Fisher ACD (2002) | "A" levels offset from the opening range; price must *stay* past A for about half the opening-range period. A prior-day pivot range sets the bias; "C" levels mark reversals. | Time confirmation; filter by bias |

### Quantitative studies

| Study | Market | Key finding |
|---|---|---|
| Holmberg, Lönnbark & Lundström (2013) | Crude oil futures, 1983–2011 | Returns significantly above zero; most of the profit came in 2000–2011 |
| Lundström (2017) | Crude oil and S&P futures | Returns rise with volatility: about 150 bps/day better in high- vs low-volatility S&P regimes |
| Tsai et al. (2018), via citation | Dow, S&P, Nasdaq | Best profits with an opening range of 5 minutes or less |
| Gao, Han, Li & Zhou (2018, JFE) | SPY and 10 ETFs | The first half-hour's return predicts the last half-hour's. Stronger on high-volatility, high-volume and news days |
| Wu et al. (2021) | Taiwan index futures | Adding a stop-loss raised annual return about 2.7 points and Sharpe about 0.9, and halved max drawdown. Adding a take-profit caused a "sharp drop in performance" |
| Zarattini & Aziz (2023) | QQQ, 2016–2023 | 5-minute ORB: 675% total return vs 169% buy-and-hold, Sharpe 1.12, 24% win rate, +0.13R per trade. Best variant: stop at 5% of ATR14, held to the close. **Assumed no slippage** |
| Zarattini, Barbon & Aziz (2024) | 7,000 US stocks, 2016–2023 | Stocks in Play. All stocks: 29% total, Sharpe 0.48. Top 20 by relative volume: 1,637% total, Sharpe 2.81, max drawdown 12%. **Commission only; no spread or slippage** |
| Zarattini, Aziz & Barbon (2024/25) | SPY, 2007–2024 | "Noise Area", a more general version of ORB. 1,985% total, Sharpe 1.33. The only one of these papers that measured slippage ($0.001/share on SPY) |

### Independent replications

- **QuantConnect** (Stocks in Play, 1,000-stock universe):
  - Sharpe 2.4, but 2016 only and with no cost model.
  - Commenters report weak results in other years.
  - **Alpaca rejected some stops as wash trades.**
- **MQL5** (index CFDs, 2015–2026):
  - Gross results matched the paper (+0.131R vs +0.13R).
  - Net of costs: about 0 to −0.08R. Every market lost money in 2015–2017.
  - Out of sample (2021–2026), the best market made only +0.06R.
  - The first-candle direction beat a random direction by 0.10–0.13R (t ≥ 2). This is the part that replicates.
- **Girlando** (QQQ, 2004–2024): with 2¢ entry and 4¢ exit slippage, the profit "vanished entirely". The edge also decays after 2021.

## Consensus: what makes ORB work (strongest evidence first)

1. **Trade only Stocks in Play.** Relative volume (RelVol) = volume in the first 5 minutes ÷
   the average first-5-minute volume over the previous 14 days. Average profit per trade by
   RelVol:

   | RelVol | Avg profit per trade |
   |---|---|
   | below 1× | −0.02R |
   | above 1× | +0.08R |
   | above 30× | +0.38R |

   Why: ORB bets that the opening imbalance between buyers and sellers lasts all day. Abnormal
   volume, usually from a news catalyst, is the evidence that the imbalance is real.
2. **Trade only in the direction of the opening move.** If the first candle closes up, take
   longs only; if down, shorts only; if flat (a doji), no trade. This is the most
   independently confirmed piece.
3. **Use a short opening range for stocks.**

   | Opening range | Sharpe |
   |---|---|
   | 5 minutes | 2.81 |
   | 15 minutes | 1.43 |
   | 30 minutes | 0.21 |
   | 60 minutes | 0.40 |

   Combining all four ranges cut max drawdown to 7%.
4. **Use a tight stop scaled to volatility, no profit target, and hold to the close.** The
   win rate is about 17–25% per trade, and a few big trend days carry all the profit. Expect
   long losing streaks; you need many trades before the results mean anything.
5. **Size by risk and cap leverage.** The papers risk 1% of equity per trade with a 4× cap. The
   cap often binds: 60% of QQQ trades were sized about 40% below what the risk rule called for.
6. **Volatility helps.** Sharpe rises with the VIX (about 1.5 normally, 3.5 above VIX 40).
   Don't switch the strategy off on wild days.
7. **Filter out noise with time and VWAP.** On SPY, acting only at :00/:30 and trailing the stop
   at VWAP doubled Sharpe (0.61 → 1.24). Filters such as "bar must close beyond the range" or
   "volume must confirm" are plausible but untested.
8. **Be flat by the close.**

## Where the consensus breaks: costs

Tight stops make R small, so fixed costs are large when measured in R. Example:

- A $50 stock with ATR $2 has a stop at 10% of ATR = $0.20, so 1R = $0.20.
- Paying 1¢ of spread plus 1¢ of slippage on both entry and exit costs $0.04 = **0.2R**.
- The paper's average edge for stocks above 1× RelVol is **0.08R**.
- Stop entries also fire when price moves fastest, which is the worst moment to get filled.

Other cautions:

- The papers are in-sample only (2016–2023), with no out-of-sample period.
- Two replications report the edge decaying after 2021.
- The authors run trading-education businesses. Nobody has independently confirmed the
  Stocks in Play result with costs included.

## The combined algorithm (what we implement)

```
Before the open (daily bars, no lookahead)
  Universe: price > $5, 14-day avg volume ≥ 1M shares, 14-day ATR > $0.50

End of the opening range (default 5 minutes, i.e. 09:35)
  RelVol = opening-range volume / average opening-range volume over the prior 14 days
  Keep RelVol ≥ 1, rank, take the top N (default 20)
  Direction: candle closed up → long only; down → short only; doji → skip

Entry:  buy stop at the opening-range high (sell stop at the low)
Stop:   10% of ATR14 from the entry price
Target: none; hold until the flatten time (default 10 minutes before the close)
Size:   shares = min(equity × risk / stop distance, equity × max leverage / N / price)
```

Variants worth testing walk-forward (fit on one period, test on the next), without over-tuning:

- opening range of 1, 5 or 15 minutes;
- stop at 5–25% of ATR, or at the far side of the opening range;
- RelVol threshold and N;
- VWAP exit on or off;
- a cutoff time for entries;
- random-direction and inverse-direction runs as a placebo check.

## Mapping to Alpaca

- **Order type: OTO (entry plus stop-loss only), not bracket.** Bracket orders require a
  take-profit, which the research says hurts.
  - The OTO stop price is fixed at submission, so slippage on entry widens the risk. The
    alternative is to place the stop after the fill arrives on the trading stream.
  - `us/docs/user-protection.md` names only bracket, OCO and trailing stop orders as exempt from
    wash-trade protection. Test OTO on paper first.
- **Flatten:** at the flatten time, cancel the stop leg, then close the position. A `cls`
  (market-on-close) order must be in by 3:50pm and conflicts with an open stop on the same
  shares.
- **Data:** RelVol needs full-market (SIP) volume for 1,000+ symbols at 09:35.
  - IEX has about 2.5% of volume, and the Basic plan streams only 30 symbols, so live trading
    needs Algo Trader Plus and a REST request for many symbols at once.
  - Backtesting works on the free plan: historical SIP bars are available except for the latest
    15 minutes.
- **Costs:** Alpaca charges no commission, but spread, slippage and the small SEC/FINRA fees on
  sells still apply.

## Backtester (implemented)

`trading-bot-backtest` (`src/trading_bot/backtest/`) implements the algorithm above with a cost
model:

- spread in bps with a 1¢ floor;
- slippage on every fill, plus extra slippage on stop-triggered fills;
- SEC and FINRA fees on sells.

It reports gross R, net R and **cost in R per trade**, which decides viability. It also offers:

- `--direction random` and `--direction inverse` placebo runs;
- `--zero-costs` to reproduce the papers' assumptions;
- `--fills ticks`: fills from real SIP trades and quotes instead of the modelled spread, which
  also settles whether a stop came before or after the entry inside a minute;
- `--universe store+delisted`: adds stocks that stopped trading before today.

Known limitations:

- **Survivorship bias, reduced but not gone.** The delisted list comes from Alpaca's inactive
  assets and corporate actions. It is thin before 2019 and misses some delistings (SIVB, FRC).
- **Bar fills model the spread.** The 4 bps default comes from mega-caps; tick fills measure it.
  Tick fills ignore quote size and queue position, and assume a 100 ms delay.
- **No daily loss limit yet.**

Usage and internals are in `src/trading_bot/backtest/README.md`.

## How results are judged (from 2026-11-02)

Comparing single backtests by eye overfits: the 2024 optimizations below looked best and then
failed the 2025 holdout. From now on a strategy is judged by a pre-registered walk-forward run,
[`walkforward-orb-v1.toml`](walkforward-orb-v1.toml), written before any of its results:

- **Grid:** 48 variants (opening range 5/15/30, range or 10%-ATR stop, RelVol ≥ 1/2/5/10,
  quiet setups kept or skipped), backtested on every year 2017–2025, each year from $25k.
- **Out of sample:** for each year 2019–2025, a fixed rule picks one variant using earlier years
  only (best mean net R per trade, smoothed over neighbouring values). The picks' results are
  the record.
- **Pass bar, all required:** day-clustered t ≥ 3 (Harvey & Liu: about 3 after many tries); 5 of
  7 years positive; still positive with costs doubled; better than random directions;
  probability of backtest overfitting ≤ 0.25; Deflated Sharpe ≥ 0.95, counting every strategy
  in the ledger as a trial.
- **Holdout:** 2026 (Jan–Oct), evaluated once with `--final`.
- **Known weaknesses:** survivorship bias (the universe is today's listed stocks); the grid was
  chosen after seeing 2024–2025; the trial count misses ad hoc analyses; bar fills. Later specs
  can use `[fills] mode = "ticks"` and `universe = "store+delisted"`.

## Test results

Every backtest and walk-forward run, with what it tested and why it did or did not work, is
in [`results.md`](results.md).

## What others found (web search, 2026-11-02)

Sources under "Replications and improvements" below. Reddit is not reachable from our
tools.

- **Nobody has published a cost-inclusive replication of the Stocks in Play paper.**
  - QuantConnect's version covers 2016 only, with no costs.
  - A GitHub reproduction implements only the base strategy, with no results yet.
  - The authors have published no results after 2023.
  - Reviews note that the paper charges commission only ($0.0035/share; "slippage" never
    appears) and attributes most of the edge to stock selection, which matches our placebo.
- **QQQ version with costs:** a close replication (Sharpe 1.06 vs the paper's 1.12) falls to
  Sharpe 0.23 with 2¢ entry and 4¢ stop slippage, and breaks even at about 2.2¢/share.
- **Live stop entries are worse than our cost model.** A QuantConnect user paper-trading a
  tight-stop ORB at IBKR measured entries a median 0.47R (mean 0.87R) past the breakout level.
  Midpoint limits saved $3–14 per fill, but 20–55% of them never filled.
- **Changes reported to help:**
  - Longer ranges with wider stops (QQQ: profit factor 0.97 at 5 minutes, 1.17 at 30; in
    sample).
  - A pre-open confirming signal: NQ futures' 09:25 bar agreeing with QQQ's direction, t 2.05
    vs 1.27 for QQQ's own 09:25 bar as a placebo. But 76% of the profit came in 2022, and it
    has no out-of-sample test.
  - Clean retests (+0.31R) vs deep ones (−0.04R): a single unreviewed author, and only 47% of
    breaks retest at all.
- **Reported to hurt** (QuantConnect users): momentum filters, price above $50, mega caps only.
- **No published evidence:** VIX bands, ML or meta-labeling filters, and trailing-stop or target
  comparisons for stock ORB.
- **Rigorous tests stay negative.** A 2026 walk-forward of 14 intraday signal families on MNQ
  futures found none passing; ORB long had t 0.88.

## Sources

- Zarattini, Barbon & Aziz, *A Profitable Day Trading Strategy For The U.S. Equity Market*
  (SSRN 4729284): https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/
- Zarattini & Aziz, *Can Day Trading Really Be Profitable?* (SSRN 4416622): https://papers.ssrn.com/abstract=4416622
- Zarattini, Aziz & Barbon, *Beat the Market: An Effective Intraday Momentum Strategy for SPY*:
  https://concretumgroup.com/beat-the-market-an-effective-intraday-momentum-strategy-for-sp500-etf-spy/
- Wu, Syu, Lin & Ho, *Evolutionary ORB-based model with protective closing strategies*
  (Knowledge-Based Systems 216, 2021): https://strategyquant.com/wp-content/uploads/2026/06/Evolutionary-ORB-based-model-with-protective-closing-strategies.pdf
- Holmberg, Lönnbark & Lundström (2013), Finance Research Letters 10(1): https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html
- Lundström, *Day trading returns across volatility states*: https://ideas.repec.org/p/hhs/umnees/0861.html
- Gao, Han, Li & Zhou, *Market intraday momentum* (summary): https://alphaarchitect.com/2014/08/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/
- MQL5 replication (gross reproduced, net zero): https://www.mql5.com/en/blogs/post/776235
- QuantConnect replication: https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- Girlando QQQ replication: https://portfoliolucagirlando.odoo.com/p16
- Concretum ORB backtest code walkthrough: https://concretumgroup.com/backtesting-the-opening-range-breakout-orb-strategy-using-polygon-io/
- ACD method: https://www.luxalgo.com/library/concept/acd-method/
- NR4/NR7: https://www.luxalgo.com/library/concept/nr4-nr7-narrow-range-bars/
- Volatility breakout: https://www.luxalgo.com/library/concept/volatility-breakout/
- Crabel stretch and narrow range: https://oxfordstrat.com/trading-strategies/narrow-range/
- SMB stocks in play: https://www.smbtraining.com/blog/smb-radar-update-2
- Optimization research (2026-11-01):
  - Fetna, *Opening-Range Breakout Does Not Survive Trading Costs* (pre-registered, 225 cells,
    futures): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7428398
  - Subhani, *Opening-Range Breakouts on Event-Free Fridays* (search-aware replication):
    https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7589980
  - Pineda, *Anatomy of the Retest in the QQQ ORB*: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6745958
  - Review of the two Zarattini papers: https://danfin.net/opening-range-breakout-research
  - QuantConnect thread (filters tried by users): https://www.quantconnect.com/forum/discussion/18444/opening-range-breakout-for-stocks-in-play/
  - MNQ ORB with SMA trend filter, in/out of sample: https://backtestsnotsignals.substack.com/p/opening-range-breakout-real-edge
  - Range-width filter in ATRs: https://algorithmictoken.substack.com/p/systematic-edge-the-30-minute-edge
  - Range-width filter as % of price (TradingView): https://www.tradingview.com/script/8vjWAdLN-Opening-Range-Breakout/
  - VWAP and volume filters (TradingView): https://es.tradingview.com/script/hapKLoXr-Opening-Range-Breakout-VWAP-Volume-ORB-Strategy
  - Gap and premarket criteria for Stocks in Play: https://scanz.com/gap-and-go-strategy/
- Stock-subset research (2026-11-01):
  - Gao, Han, Li & Zhou, *Intraday Momentum: The First Half-Hour Return Predicts the Last
    Half-Hour Return*: https://www.ssrn.com/abstract=2552752
  - Swedish individual-stock intraday momentum study: https://www.diva-portal.org/smash/get/diva2:1878991/FULLTEXT01.pdf
  - Practitioner ORB stock criteria (float, catalyst, volume): https://scanz.com/orb-trading-strategy/
- Replications and improvements (2026-11-02):
  - QQQ replication with slippage sweep and NQ filter: https://github.com/giovannibrusco/zarattini-2023-orb-qqq
  - Stocks in Play reproduction (base strategy only so far): https://github.com/MK27MK/orb_paper_reproduction
  - Live ORB entry slippage at IBKR: https://www.quantconnect.com/forum/discussion/21585/breakout-entries-on-us-stocks-eat-most-of-the-edge-how-do-you-cut-entry-cost/
  - Review of the 1,637% paper (costs, stop width): https://blog.tradoki.com/posts/opening-range-breakout-stocks-in-play-research
  - QQQ range-length and target sweep: https://kaiyasuda.substack.com/p/the-opening-range-teardown
  - Mesfin, *Structural Limits of OHLCV-Based Intraday Momentum Signals in MNQ Futures*:
    https://arxiv.org/abs/2605.04004
  - Concretum's Alpaca ORB tutorial (TQQQ, $0.0035/share): https://concretumgroup.substack.com/p/how-to-backtest-a-orb-strategy-in
- Testing methodology (2026-11-02):
  - Bailey, Borwein, López de Prado & Zhu, *The Probability of Backtest Overfitting*:
    https://papers.ssrn.com/abstract=2326253
  - Bailey & López de Prado, *The Deflated Sharpe Ratio*: https://papers.ssrn.com/abstract=2460551
  - Harvey & Liu, *Backtesting*: https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
  - Walk-forward pitfalls: https://arxiv.org/pdf/2603.09219
- Alpaca docs (local copy): `thridparty-repos/alpaca-docs/us/docs/orders-at-alpaca.md` (bracket,
  OTO, `cls`) and `user-protection.md` (wash-trade exemptions)

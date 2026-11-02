# strategies

Trading strategies. Paths are relative to this folder (`trading-bot/src/trading_bot/strategies/`).

**Up:** [`../README.md`](../README.md)

## Live interface (`base.py`)

- `Signal(symbol, side: "buy"|"sell", qty: Decimal, reason="")` is a strategy's intent. The
  `RiskManager` checks it and the `OrderManager` turns it into an order.
- `Strategy` is an ABC:
  - set `name`;
  - implement `on_tick(clients) -> Iterable[Signal]`;
  - optionally implement `on_start()` and `on_stop()`.
- To add a strategy:
  1. Create a new module here that subclasses `Strategy`.
  2. Pass an instance via `Application(strategies=[...])` in `main.py`.
  3. Add a test.

This polling interface predates the day-trading design. Event-driven strategies will receive
bars instead (`on_bar`); see `../README.md`, "Status".

## Opening range breakout (`orb.py`)

Pure functions with no Alpaca calls, shared by the backtester and (later) live trading. The
evidence for every rule and default is in `../../../docs/research/orb.md`.

| Function / type | Purpose |
|---|---|
| `OrbParams` | All strategy parameters (validated in `__post_init__`) |
| `opening_range(bars)` → `OpeningRange` | Aggregates the opening bars; `.direction` is long, short, or `None` for a doji |
| `average_true_range(daily)`, `daily_stats(history, lookback)` → `DailyStats` | ATR (simple mean of true range) and average volume from prior sessions only. Returns `None` if history is short or a split falls in the window |
| `relative_volume(or_volume, prior)` | Today's opening-range volume ÷ its average over prior days |
| `passes_daily_filters`, `passes_setup_filters`, `gap` | Price, volume and ATR filters; the optional gap and opening-range width filters |
| `select_candidates(inputs, params, rng)` → `[OrbCandidate]` | Applies the filters, drops dojis and low RelVol, ranks by RelVol (ties broken by symbol) and keeps the top N. Applies placebo direction modes |
| `OrbCandidate.trigger` | Entry stop price: range high for longs, range low for shorts |
| `stop_distance(candidate, params)` | 1R per share: `stop_atr_fraction × ATR`, or the range width |
| `position_size(...)` | `min(equity × risk / R, equity × max_leverage / top_n / price)`, rounded down to whole shares |

`OrbParams` defaults (from Zarattini, Barbon & Aziz 2024, except the flatten time). Options
marked "optional" are off by default; they are the practitioner optimizations tested in
`../../../docs/research/results.md`:

| Parameter | Default | Meaning |
|---|---|---|
| `or_minutes` | 5 | Opening range length (minutes) |
| `lookback_days` | 14 | Window for ATR, average volume and RelVol |
| `min_price`, `min_avg_volume`, `min_atr` | 5.0, 1,000,000, 0.50 | Daily filters |
| `min_relvol`, `top_n` | 1.0, 20 | Stocks-in-play selection |
| `min_gap`, `gap_aligned` | 0, False | Optional: minimum overnight gap (fraction); gap must point the candle's way |
| `min_atr_pct` | 0 | Optional: minimum ATR as a fraction of the open |
| `min_range_pct`, `min_range_atr`, `max_range_atr` | 0, 0, `None` | Optional: opening-range width limits (fraction of the open; ATRs) |
| `entry_mode` | `stop` | `stop` at the trigger; `close`: next bar's open after a bar closes beyond the range; `market`: first bar after the range |
| `entry_window_minutes` | `None` | Optional: no entries later than N minutes after the range ends |
| `stop_mode`, `stop_atr_fraction` | `atr`, 0.10 | Stop placement |
| `target_r` | `None` | No profit target; hold to the flatten time |
| `breakeven_r` | `None` | Optional: move the stop to the entry price after +N R (from the next bar) |
| `vwap_exit` | False | Exit after a bar closes across session VWAP |
| `exit_minutes_before_close` | 10 | Flatten at 15:50 (the papers use 0) |
| `last_entry_minutes_before_close` | `None` | Entry cutoff (`None` means until the flatten time) |
| `direction_mode` | `opening` | `random` and `inverse` are placebo checks |

If you change a default, update the research doc and `../backtest/cli.py` (the CLI repeats the
defaults) in the same change. `tests/test_backtest_cli.py` fails if the CLI defaults drift from
`OrbParams()`.

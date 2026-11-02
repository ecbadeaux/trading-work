# trading-bot

Alpaca-based day-trading bot and backtester. Paper trading by default. Paths and commands are
relative to this folder (`trading-bot/`); run them with the venv active.

**Up:** [`../README.md`](../README.md) · **Children:**

- [`src/trading_bot/README.md`](src/trading_bot/README.md): code architecture and packages.
- [`docs/README.md`](docs/README.md): configuration and strategy research.
- [`tests/README.md`](tests/README.md): test conventions and fakes.

## Status

| Part | State |
|---|---|
| `trading-bot` (live/paper bot) | Connects, logs the account and polls every 60 s. **No strategy is registered yet, so it never trades.** The event-driven day-trading design (streams, session manager) is not built yet |
| `trading-bot-backtest` | Working. Backtests the opening range breakout (ORB) on Stocks in Play over Alpaca history, with a cost model or with fills from real trades and quotes (`--fills ticks`). Results go in `docs/research/results.md` |
| `trading-bot-download` | Working. Downloads daily and full-session minute bars into a local SQLite store so backtests run offline (resumable); `--include-delisted` adds stocks that delisted before today |
| `trading-bot-ticks` | Working. Downloads the trades and quotes a walk-forward spec needs for tick fills (only the minutes it trades in) |
| `trading-bot-walkforward` | Working. Runs a pre-registered walk-forward test of a parameter grid from the store, with overfitting checks (PBO, Deflated Sharpe) and pass criteria |
| `trading-bot-ledger` | Working. Counts the strategies tried so far (every backtest is recorded) |

## Setup

Requires Python 3.11+. The macOS system Python 3.9 is too old for alpaca-py.

```sh
brew install python@3.12          # if needed
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"           # the package (editable) plus pytest, ruff, mypy
```

**Keys:** copy `.env.example` to `~/.config/trading-bot/.env`, `chmod 600` it, add **paper**
keys, and pass `--env-file ~/.config/trading-bot/.env`. `ALPACA_PAPER` alone picks paper vs live.
The env vars, the live guard and why there is no base-URL setting are all covered in
[`docs/configuration.md`](docs/configuration.md).

## Run the bot

```sh
trading-bot --once --dry-run --env-file ~/.config/trading-bot/.env   # connect, one tick, no orders
trading-bot --env-file ~/.config/trading-bot/.env                    # loop until Ctrl+C
python -m trading_bot ...                                            # same as trading-bot
```

- **Flags:** `--once`, `--dry-run`, `--log-level`, `--env-file`, `--version`.
- **Exit codes:** 0 ok, 1 fatal error, 2 config error.
- **Logs** go to the console and `logs/trading-bot.log` (rotating).

## Backtest

```sh
trading-bot-backtest --start 2026-09-01 --end 2026-09-30 \
  --symbols AAPL,MSFT,NVDA --env-file ~/.config/trading-bot/.env
trading-bot-backtest --help
```

- **Data only:** it never places orders.
- **Output** goes to `backtests/<timestamp>/`; downloaded bars are cached in `data-cache/`. Both
  are gitignored.
- **Offline backtests:** fill a local store once, then point backtests at it:

  ```sh
  trading-bot-download --start 2017-01-03 --end 2026-10-30 --env-file ~/.config/trading-bot/.env --estimate
  trading-bot-download --start 2017-01-03 --end 2026-10-30 --env-file ~/.config/trading-bot/.env
  trading-bot-backtest --start 2022-01-03 --end 2025-12-31 --universe store \
    --store data-store/bars.sqlite --offline          # no keys needed offline
  ```

  The first command only plans and prints the size. The store (`data-store/`, gitignored) is
  described in [`src/trading_bot/data/README.md`](src/trading_bot/data/README.md).
- **More accurate backtests:** add the stocks that later delisted, and price fills from the real
  tape:

  ```sh
  trading-bot-download --start 2017-01-03 --end 2026-10-30 --include-delisted \
    --env-file ~/.config/trading-bot/.env
  trading-bot-backtest --start 2024-01-02 --end 2024-12-31 --universe store+delisted \
    --store data-store/bars.sqlite --stop-mode range --min-relvol 10 --fills ticks \
    --env-file ~/.config/trading-bot/.env              # downloads the ticks it needs
  ```
- **Judging a strategy:** run the pre-registered walk-forward protocol rather than comparing
  single backtests by eye:

  ```sh
  trading-bot-walkforward docs/research/walkforward-orb-v1.toml    # 2017-2025, no keys
  trading-bot-ledger                                               # strategies tried so far
  ```

  `--final` adds the one-shot 2026 holdout. Every backtest is recorded in
  `backtests/ledger.jsonl`, because the overfitting checks need to know how many strategies
  were tried. What has been tested so far, and why it worked or didn't, is in
  [`docs/research/results.md`](docs/research/results.md).
- The options, the fill and cost model, the outputs and the limits are in
  [`src/trading_bot/backtest/README.md`](src/trading_bot/backtest/README.md).

## Develop

```sh
pytest && ruff check src tests && mypy src
```

- **Tests never touch the network or real keys** (see [`tests/README.md`](tests/README.md)).
- **Keep alpaca-py imports lazy** (see [`src/trading_bot/README.md`](src/trading_bot/README.md)).
- **When behavior changes, update the README at that level of the tree.**
- **To build a wheel:** `pip install build && python -m build`.

## Layout

```
pyproject.toml       package, deps, console scripts, pytest/ruff/mypy config
.env.example         settings template (copy outside the repo; see docs/configuration.md)
src/trading_bot/     the code                  → src/trading_bot/README.md
  strategies/        Strategy interface, ORB   → strategies/README.md
  backtest/          backtester + CLI          → backtest/README.md
  data/              records, loader, stores   → data/README.md
docs/                configuration, research   → docs/README.md
tests/               pytest suite              → tests/README.md
scripts/             one-off scripts (empty)
logs/ backtests/ data-cache/ data-store/ .venv/   generated, gitignored
```

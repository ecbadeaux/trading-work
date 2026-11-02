# trading_bot package

Architecture of the code. Paths are relative to this folder (`trading-bot/src/trading_bot/`).

**Up:** [`../../README.md`](../../README.md) · **Children:**

- [`strategies/README.md`](strategies/README.md): the Strategy interface and the ORB logic.
- [`backtest/README.md`](backtest/README.md): the backtester.
- [`data/README.md`](data/README.md): market data records and the Alpaca loader.

## Modules

| Path | Role |
|---|---|
| `main.py` | `trading-bot` entry point: CLI args, settings, logging, SIGINT/SIGTERM, then `Application.run()` |
| `__main__.py` | `python -m trading_bot` |
| `config.py` | `Settings` and `load_settings()` (env/.env loading, paper default, live guard). See `../../docs/configuration.md` |
| `logging_setup.py` | `setup_logging(level, log_dir)`: logs to the console and to `logs/trading-bot.log` (rotating, 5 MB × 5) |
| `app.py` | `Application`: startup, a polling `tick()` loop, shutdown |
| `broker/clients.py` | `create_clients()` → `AlpacaClients(trading, stock_data, crypto_data)`. **The only place that constructs alpaca-py clients** |
| `strategies/` | `Strategy` ABC and `Signal` (`strategies/base.py`); ORB decision logic (`strategies/orb.py`) |
| `risk/manager.py` | `RiskManager.approve(signal)`: currently a max-quantity check (default 100) |
| `execution/orders.py` | `OrderManager.submit(signal)`: market DAY orders; only logs in dry-run |
| `data/` | Plain `Bar`/`DailyBar`/`TradingDay` records; `AlpacaHistoricalSource` (bars, cache); `BarStore`/`StoreSource` (local SQLite store) and the `trading-bot-download` CLI |
| `backtest/` | ORB backtester (`trading-bot-backtest`), the run ledger (`trading-bot-ledger`) and the walk-forward runner (`trading-bot-walkforward`) |
| `utils/` | Empty placeholder |

## Flows

**Bot** (`trading-bot`):

1. `main()` → `load_settings()` → `setup_logging()` → `Application(settings).run()`.
2. `startup()` logs the account, then creates `RiskManager` and `OrderManager` and calls each
   strategy's `on_start()`.
3. Each `tick()` (every `TRADING_BOT_LOOP_INTERVAL` seconds) logs the market clock. Then, per
   strategy, `on_tick()` produces `Signal`s, `RiskManager.approve()` checks each one, and
   `OrderManager.submit()` places the order.
4. SIGINT or SIGTERM calls `stop()`, which ends the loop. `shutdown()` then calls each
   strategy's `on_stop()`.

**Status:**

- `main.py` registers **no strategies yet**, so the bot connects and polls but never trades.
- The day-trading design will replace polling with an asyncio event loop driven by
  `StockDataStream` and `TradingStream`, plus a session manager for the cutoff and flatten
  times. None of that is built yet.

**Backtester** (`trading-bot-backtest`):

1. `backtest/cli.py` → `load_settings()` → `create_clients()` → `AlpacaHistoricalSource`
   (with `--offline`: only `StoreSource`, no keys).
2. `run_backtests()` → `summarize()` → `write_outputs()` → a ledger record.

**Walk-forward** (`trading-bot-walkforward`): `load_spec()` → one `run_backtests()` per year
in worker processes (cached) → selection → checks (`backtest/stats.py`) → `report.md`.

See [`backtest/README.md`](backtest/README.md).

## Design rules

- **alpaca-py imports are lazy:** inside functions, or under `TYPE_CHECKING` for type hints.
  Importing any module needs no SDK, network or keys, and tests stay offline. Use
  `cast("SdkType", ...)` where the SDK returns `Model | RawData` unions.
- **Strategy logic is pure.** `strategies/orb.py` works on plain records from `data/models.py`,
  so the same code drives the backtest and, later, live trading.
- **Paper by default.** `ALPACA_PAPER` is the only paper/live switch: no base-URL setting and no
  `url_override` (see `../../docs/configuration.md`).
- **Every order path must respect `dry_run`.**
- **Timezones:** market times are timezone-aware US/Eastern (`data.models.ET`). Bar timestamps
  are tz-aware bar-start times.

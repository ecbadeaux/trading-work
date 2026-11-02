# tests

pytest suite for `trading_bot`. Paths are relative to this folder (`trading-bot/tests/`).

**Up:** [`../README.md`](../README.md)

Run from `trading-bot/` with the venv active:

```sh
pytest                    # the whole suite
pytest tests/test_engine.py -k split
ruff check src tests
mypy src                  # mypy covers src only
```

## Rules

- **No network, no keys.**
  - Alpaca clients are always fakes or mocks.
  - `conftest.py` clears every bot env var and changes into a temp dir for each test, so a
    developer's real `.env` or keys can never leak in.
  - The `keys` fixture sets dummy keys when a test needs them.
- **Don't import alpaca-py at module level** in code under test; it stays lazy (see
  [`../src/trading_bot/README.md`](../src/trading_bot/README.md)).
- **Each source module gets a test file.**

## Files and fakes

| File | Covers | Useful pieces |
|---|---|---|
| `helpers.py` | shared builders | `et()` (ET datetime), `bar()`, `session()` (a TradingDay), `weekdays()` |
| `tick_helpers.py` | shared tick builders | `trade()`, `quote()`, `us()` (epoch µs); **`FakeTicks`**, a dict-backed `TickSource`; `fill_from_bars()` writes a plausible tape for planned windows from a bar store |
| `conftest.py` | env isolation | `clean_env` (autouse), `keys` |
| `test_config.py` | `config.py` | |
| `test_app.py` | `app.py` | `FakeStrategy`; `MagicMock` clients |
| `test_orb.py` | `strategies/orb.py` | |
| `test_costs.py` | `backtest/costs.py` | |
| `test_simulator.py` | `backtest/simulator.py` | `run()` helper; fixed long/short candidates |
| `test_engine.py` | `backtest/engine.py`, `backtest/report.py` | **`FakeSource`**, an in-memory `DataSource` that records opening-range requests; `build_source()`; `run_backtests` must equal separate runs |
| `test_metrics.py` | `backtest/metrics.py` | hand-built `Trade`/`DayResult` |
| `test_historical.py` | `data/historical.py` | `FakeStock`/`FakeTrading` imitating alpaca-py responses; real `StockBarsRequest` objects are inspected |
| `test_backtest_cli.py` | `backtest/cli.py` | argument parsing; exit codes without keys; an `--offline` run without keys writes ledger records; `--fills ticks` with and without stored ticks |
| `test_ticks.py` | `data/ticks.py`, `backtest/ticks.py`, `backtest/tickplan.py`, tick fills in `simulator.py` | the stop election rules; the tape settling an ambiguous entry minute; fallbacks; batched tick requests (`FakeTickClient`) |
| `test_delisted.py` | `data/delisted.py`, store universes, `download._download_delisted` | renames and reused tickers; the download step against fake Alpaca clients |
| `test_ledger.py` | `backtest/ledger.py` | trial counting, backfill from `write_outputs()` folders |
| `test_stats.py` | `backtest/stats.py` | `trade()` (a one-share trade whose dollars equal R); PBO cases with known answers (0 and 1) |
| `test_walkforward.py` | `backtest/walkforward.py`, `trading-bot-ticks` | `build_store()`: a synthetic 2021-2024 store (one stock in play every 4th day); end-to-end runs, the cache, the holdout guard, tick fills and the delisted universe |
| `test_store.py` | `data/store.py` | `Fallback` (records what `StoreSource` could not serve); `build_store()` (everything `build_source()` has); a store must give the same trades as the source |
| `test_download.py` | `data/download.py` | `FakeClient` (queued pages and errors for `BarFetcher`); a fake clock for throttling and backoff |

`helpers.py` and `tick_helpers.py` are imported as top-level modules (`from helpers import
bar`). This works because pytest puts `tests/` on `sys.path`.

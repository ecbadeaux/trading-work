"""Which tick windows a backtest needs, fetching the missing ones, and ``trading-bot-ticks``.

Tick fills (``simulator.py``) need, per trade, the entry minute and the exit. The bar backtest
with *optimistic* intrabar fills gives both paths a trade can take with ticks:

- its entry minute, whose trades also settle the worst case (a stop right after the entry);
- the exit it reaches when the stop is not hit in the entry minute: the stop's minute, or a
  few seconds of quotes at a market exit (flatten time, VWAP exit).

Windows with the same kind and time go out as one multi-symbol request (``--tick-chunk``
symbols), so the 15:50 exits of a whole day cost one request.

``trading-bot-ticks SPEC`` does this for every variant of a walk-forward spec over its
development years (``--include-final`` adds the holdout year; downloading it shows no results).
``trading-bot-backtest --fills ticks`` plans and fetches for its own run.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import BacktestConfig, DataSource, run_backtests
from trading_bot.backtest.simulator import Trade
from trading_bot.data.ticks import (
    TickStore,
    Window,
    minute_windows,
    moment_window,
    quote_rows,
    trade_rows,
)
from trading_bot.strategies.orb import OrbParams

if TYPE_CHECKING:
    from trading_bot.data.download import BarFetcher

log = logging.getLogger(__name__)

DEFAULT_TICKS = Path("data-store/ticks.sqlite")
DEFAULT_CHUNK = 50


def windows_for(
    symbol: str, entry_time: datetime, exit_time: datetime, reason: str
) -> list[Window]:
    """The windows one trade needs (``reason`` is its exit reason in an optimistic bar run)."""
    out = list(minute_windows(symbol, entry_time))
    if reason in ("stop", "breakeven"):
        out.extend(minute_windows(symbol, exit_time))
    elif reason in ("close", "vwap"):
        out.append(moment_window(symbol, exit_time))
    return out


def plan_trades(trades: Iterable[Trade]) -> set[Window]:
    return {
        w for t in trades for w in windows_for(t.symbol, t.entry_time, t.exit_time, t.exit_reason)
    }


def plan_backtest(
    source: DataSource,
    universe: Sequence[str],
    variants: Sequence[OrbParams],
    costs: CostModel,
    config: BacktestConfig,
) -> set[Window]:
    """Windows for a tick-fill run of ``variants``: one optimistic bar run (same eligibility)."""
    bar_config = replace(config, fills="bars", worst_case_intrabar=False)
    results = run_backtests(source, universe, variants, costs, bar_config)
    return plan_trades(t for r in results for t in r.trades)


def missing(store: TickStore, windows: Iterable[Window]) -> list[Window]:
    have = store.stored()
    return sorted(w for w in windows if w.key() not in have)


def request_groups(
    windows: Iterable[Window],
    request_symbol: Callable[[str], tuple[str, str | None]],
    chunk: int = DEFAULT_CHUNK,
) -> list[tuple[str, datetime, datetime, str | None, dict[str, str]]]:
    """``(kind, start, end, asof, {ticker: key})`` requests covering ``windows``, in time order.

    A ticker appears once per request; two keys for one ticker (not expected) are split."""
    grouped: dict[tuple[str, datetime, datetime, str | None], list[tuple[str, str]]] = (
        defaultdict(list)
    )
    for w in windows:
        ticker, asof = request_symbol(w.symbol)
        grouped[(w.kind, w.start, w.end, asof)].append((ticker, w.symbol))
    out = []
    for (kind, start, end, asof), pairs in sorted(
        grouped.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][3] or "")
    ):
        batch: dict[str, str] = {}
        for ticker, key in sorted(pairs):
            if ticker in batch or len(batch) >= chunk:
                out.append((kind, start, end, asof, batch))
                batch = {}
            batch[ticker] = key
        if batch:
            out.append((kind, start, end, asof, batch))
    return out


def fetch(
    fetcher: BarFetcher,
    store: TickStore,
    windows: Sequence[Window],
    request_symbol: Callable[[str], tuple[str, str | None]],
    chunk: int = DEFAULT_CHUNK,
    threads: int = 4,
) -> int:
    """Download ``windows`` into ``store``, ``threads`` requests in flight. Returns the requests
    made (pages are counted by the fetcher). Saves after every request, so an interrupted run
    resumes."""
    from trading_bot.data.download import ordered_map

    groups = request_groups(windows, request_symbol, chunk)
    started = time.monotonic()

    def get(group: tuple[str, datetime, datetime, str | None, dict[str, str]]) -> Any:
        kind, start, end, asof, batch = group
        return fetcher.ticks(kind, sorted(batch), start, end, asof=asof)

    for n, (group, raw) in enumerate(ordered_map(get, groups, threads), 1):
        kind, start, end, asof, batch = group
        parse = trade_rows if kind == "trades" else quote_rows
        store.put_many(
            (Window(key, kind, start, end), parse(raw.get(ticker, [])))  # type: ignore[arg-type]
            for ticker, key in batch.items()
        )
        if n % 200 == 0 or n == len(groups):
            elapsed = time.monotonic() - started
            eta = (len(groups) - n) * elapsed / n / 60 if n else float("nan")
            log.info("Ticks: request %d/%d (%s), %d pages, %d retries, ETA %.0f min",
                     n, len(groups), start.date(), fetcher.pages, fetcher.retries, eta)
    return len(groups)


def describe(windows: Sequence[Window], todo: Sequence[Window], groups: int, rpm: float) -> str:
    minutes = groups / rpm if rpm > 0 else math.inf
    return (
        f"Tick windows: {len(windows):,} needed, {len(windows) - len(todo):,} stored, "
        f"{len(todo):,} to fetch in about {groups:,} requests (~{minutes:.0f} min at "
        f"{rpm:g}/min; busy minutes take extra pages)"
    )


# -- trading-bot-ticks --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-bot-ticks",
        description="Download the trades and quotes a walk-forward spec needs for tick fills "
        "(data only; never places orders). Resumable: re-run to continue.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("spec", type=Path, help="Walk-forward spec (TOML)")
    p.add_argument("--store", type=Path, default=Path("data-store/bars.sqlite"))
    p.add_argument("--ticks", type=Path, default=DEFAULT_TICKS, help="Tick store to fill")
    p.add_argument("--out", type=Path, default=Path("backtests/walkforward"),
                   help="Walk-forward output root (its cache is reused)")
    p.add_argument("--variants", help="Only these variant ids, e.g. v08,v24 (default: all)")
    p.add_argument("--include-final", action="store_true",
                   help="Also the holdout year (no results are shown; for a later --final)")
    p.add_argument("--env-file", help="Env file with API keys (e.g. ~/.config/trading-bot/.env)")
    p.add_argument("--rpm", type=float, default=180, help="Max requests per minute (Basic: 200)")
    p.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="Symbols per request")
    p.add_argument("--threads", type=int, default=4, help="Requests in flight at once")
    p.add_argument("--workers", type=int, default=None, help="Processes for the bar runs")
    p.add_argument("--estimate", action="store_true", help="Plan only; print the size")
    p.add_argument("--log-level", default=None, help="Override TRADING_BOT_LOG_LEVEL")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from trading_bot.backtest import walkforward as wf
    from trading_bot.config import ConfigError, load_settings
    from trading_bot.logging_setup import setup_logging

    args = build_parser().parse_args(argv)
    try:
        spec = wf.load_spec(args.spec)
    except (OSError, wf.SpecError) as exc:
        print(f"Invalid spec: {exc}", file=sys.stderr)
        return 2
    if not args.store.is_file():
        print(f"Store not found: {args.store}", file=sys.stderr)
        return 2
    wanted = None if not args.variants else {v.strip() for v in args.variants.split(",")}
    variants = [v for v in spec.variants if wanted is None or v.id in wanted]
    if not variants or (wanted and len(variants) != len(wanted)):
        print(f"Unknown variant ids: {sorted((wanted or set()) - {v.id for v in variants})}",
              file=sys.stderr)
        return 2
    settings = None
    if not args.estimate:
        try:
            settings = load_settings(env_file=args.env_file)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    level = (args.log_level or (settings.log_level if settings else "INFO")).upper()
    setup_logging(level, settings.log_dir if settings else Path("logs"))

    try:
        return _run(args, spec, variants, settings)
    except KeyboardInterrupt:
        log.warning("Interrupted. Everything downloaded so far is saved; re-run to resume.")
        return 130
    except Exception:
        log.exception("Tick download failed. Re-run the same command to resume.")
        return 1


def _run(args: argparse.Namespace, spec: Any, variants: Sequence[Any], settings: Any) -> int:
    from trading_bot.backtest import walkforward as wf
    from trading_bot.data.store import BarStore

    store = BarStore(args.store)
    try:
        universe = tuple(store.universe(spec.universe))
        cal_range = store.calendar_range()
        if cal_range is None or not universe:
            raise wf.StoreIncomplete(f"{args.store} has no calendar or universe yet")
        lookback = max(v.params.lookback_days for v in spec.variants)
        years = spec.dev_years + ([spec.final_year] if args.include_final and spec.final_year
                                  else [])
        windows_by_year = wf.year_windows(years, cal_range, lookback)
        delisted = store.delisted()
    finally:
        store.close()

    tasks = [
        wf.YearTask(
            store=str(args.store), universe=universe, year=y, start=windows_by_year[y][0],
            end=windows_by_year[y][1], ids=tuple(v.id for v in variants),
            variants=tuple(v.params for v in variants), seeds=tuple(spec.seed for _ in variants),
            costs=spec.costs, config=spec.config(*windows_by_year[y]),
        )
        for y in years
    ]
    needed: set[Window] = set()
    for year, found in _plan_years(tasks, args.workers or wf.default_workers()):
        log.info("Planned %d: %d windows", year, len(found))
        needed |= found
    ticks = TickStore(args.ticks)
    try:
        todo = missing(ticks, needed)
        groups = request_groups(todo, lambda key: _lookup(key, delisted), args.chunk)
        print(describe(sorted(needed), todo, len(groups), args.rpm), flush=True)
        if args.estimate or not todo:
            return 0
        from trading_bot.broker import create_clients
        from trading_bot.data.download import BarFetcher

        clients = create_clients(settings)
        fetcher = BarFetcher(clients.stock_data, pages_per_minute=args.rpm)
        fetch(fetcher, ticks, todo, lambda key: _lookup(key, delisted), args.chunk, args.threads)
        stats = ticks.stats()
        print(f"Done. {stats['windows']:,} windows ({stats['empty']:,} empty), "
              f"{stats['bytes'] / 1e9:.2f} GB at {ticks.path}; {fetcher.pages:,} pages")
        return 0
    finally:
        ticks.close()


def plan_year(task: Any) -> tuple[int, set[Window]]:
    """Windows for one ``walkforward.YearTask`` (its variants, optimistic bar fills). Runs in
    worker processes."""
    from trading_bot.backtest.engine import eligibility_key
    from trading_bot.data.store import BarStore, StoreSource

    store = BarStore(task.store)
    try:
        source = StoreSource(store)
        groups: dict[tuple[int, float, float, float], list[OrbParams]] = defaultdict(list)
        for params in task.variants:
            groups[eligibility_key(params)].append(params)
        found: set[Window] = set()
        for members in groups.values():
            found |= plan_backtest(source, task.universe, members, task.costs, task.config)
        return task.year, found
    finally:
        store.close()


def _plan_years(tasks: Sequence[Any], workers: int) -> Iterable[tuple[int, set[Window]]]:
    if workers <= 1 or len(tasks) <= 1:
        for task in tasks:
            yield plan_year(task)
        return
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from trading_bot.backtest.walkforward import _quiet_worker

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks)), mp_context=context,
                             initializer=_quiet_worker) as pool:
        futures = [pool.submit(plan_year, task) for task in tasks]
        for future in as_completed(futures):
            yield future.result()


def _lookup(key: str, delisted: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
    """Like ``BarStore.request_symbol``, from a copy of the delisted list (store closed)."""
    record = delisted.get(key)
    return (str(record["symbol"]), "-") if record is not None else (key, None)


if __name__ == "__main__":
    raise SystemExit(main())

"""``trading-bot-backtest``: run the ORB backtest on Alpaca historical data.

Data only: this command never places orders. It needs API keys (paper keys are fine) to read
market data, except with ``--offline`` (local store only). Every run is recorded in the ledger
(``ledger.py``). See docs/research/orb.md for the strategy and README.md for examples.

``--fills ticks`` prices fills from real trades and quotes: a first, optimistic bar pass lists
the tick windows the run needs (``tickplan.py``), missing ones are downloaded into ``--ticks``
(not with ``--offline``), then the run is simulated on the tape (``simulator.py``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_bot.backtest import ledger
from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import BacktestConfig, BacktestResult, run_backtests
from trading_bot.backtest.ledger import DEFAULT_LEDGER
from trading_bot.backtest.metrics import combine_equal_weight, curve_stats, summarize
from trading_bot.backtest.report import (
    fill_stats,
    format_comparison,
    format_fill_stats,
    format_summary,
    write_comparison,
    write_outputs,
)
from trading_bot.config import ConfigError, Settings, load_settings
from trading_bot.data.models import ET
from trading_bot.logging_setup import setup_logging
from trading_bot.strategies.orb import OrbParams

if TYPE_CHECKING:
    from trading_bot.backtest.engine import DataSource
    from trading_bot.data.historical import AlpacaHistoricalSource
    from trading_bot.data.store import BarStore
    from trading_bot.data.ticks import StoreTicks

log = logging.getLogger(__name__)


def minutes_list(text: str) -> list[int]:
    """Parse ``--or-minutes``: one length or a comma-separated list, e.g. ``5,15,30``."""
    try:
        values = sorted({int(v) for v in text.split(",") if v.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected minutes like 5 or 5,15,30: {text!r}") from exc
    if not values or values[0] <= 0:
        raise argparse.ArgumentTypeError("opening range lengths must be positive")
    return values


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-bot-backtest",
        description="Backtest the opening range breakout on Stocks in Play (data only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", required=True, type=date.fromisoformat, help="First day (YYYY-MM-DD)")
    p.add_argument("--end", required=True, type=date.fromisoformat, help="Last day (YYYY-MM-DD)")

    u = p.add_argument_group("universe (pick one)")
    src = u.add_mutually_exclusive_group(required=True)
    src.add_argument("--symbols", help="Comma-separated symbols, e.g. AAPL,MSFT,NVDA")
    src.add_argument("--symbols-file", type=Path, help="File with one symbol per line")
    src.add_argument(
        "--universe",
        choices=["active", "stocks", "store", "store+delisted"],
        help="'active': every active, tradable US equity listed on Alpaca (survivorship bias); "
        "'stocks': the same without ETFs, ETNs and funds (name/exchange heuristic); "
        "'store': the symbols saved in --store by trading-bot-download (plus its extras); "
        "'store+delisted': plus the delisted stocks it downloaded (--include-delisted)",
    )
    u.add_argument("--max-symbols", type=int, help="Use only the first N symbols (quick runs)")

    d = p.add_argument_group("data")
    d.add_argument("--env-file", help="Env file with API keys (e.g. ~/.config/trading-bot/.env)")
    d.add_argument("--feed", default="sip", choices=["sip", "iex"])
    d.add_argument("--cache-dir", type=Path, default=Path("data-cache"))
    d.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Read bars from this trading-bot-download SQLite store (e.g. "
        "data-store/bars.sqlite); anything missing comes from Alpaca unless --offline",
    )
    d.add_argument(
        "--offline",
        action="store_true",
        help="With --store: never call Alpaca (no API keys needed)",
    )
    d.add_argument("--out", type=Path, default=Path("backtests"), help="Output root directory")
    d.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="Append one line per run to this ledger (counts strategies tried)",
    )
    d.add_argument("--no-ledger", action="store_true", help="Don't record this run")
    d.add_argument("--chunk-size", type=int, default=200, help="Symbols per bars request")
    d.add_argument("--rpm", type=float, default=150, help="Max bar requests per minute")
    d.add_argument("--benchmark", default="SPY", help="Benchmark symbol ('' to skip)")

    s = p.add_argument_group("strategy")
    s.add_argument(
        "--or-minutes",
        type=minutes_list,
        default=[5],
        help="Opening range length in minutes, or a list such as 5,15,30 to run each length "
        "and compare them (one pass over the data, longest window fetched once)",
    )
    s.add_argument("--lookback", type=int, default=14, help="Days for ATR/volume/RelVol")
    s.add_argument("--top-n", type=int, default=20)
    s.add_argument("--min-relvol", type=float, default=1.0)
    s.add_argument("--min-price", type=float, default=5.0)
    s.add_argument("--min-avg-volume", type=float, default=1_000_000)
    s.add_argument("--min-atr", type=float, default=0.50)
    s.add_argument("--min-atr-pct", type=float, default=0.0, help="Min ATR / open, e.g. 0.01")
    s.add_argument("--min-gap", type=float, default=0.0, help="Min |overnight gap|, e.g. 0.02")
    s.add_argument("--gap-aligned", action="store_true", help="Gap must match the candle")
    s.add_argument("--min-range-pct", type=float, default=0.0, help="Min range width / open")
    s.add_argument("--min-range-atr", type=float, default=0.0, help="Min range width in ATRs")
    s.add_argument("--max-range-atr", type=float, default=None, help="Max range width in ATRs")
    s.add_argument("--entry-mode", choices=["stop", "close", "market"], default="stop")
    s.add_argument(
        "--entry-window", type=int, default=None, help="Entries only N minutes after the range"
    )
    s.add_argument("--stop-mode", choices=["atr", "range"], default="atr")
    s.add_argument("--stop-atr", type=float, default=0.10, help="Stop as a fraction of ATR14")
    s.add_argument("--target-r", type=float, default=None, help="Profit target in R (default none)")
    s.add_argument(
        "--breakeven-r", type=float, default=None, help="Move the stop to entry at +N R"
    )
    s.add_argument("--vwap-exit", action="store_true", help="Exit when a bar closes across VWAP")
    s.add_argument("--exit-before-close", type=int, default=10, help="Flatten N minutes early")
    s.add_argument("--last-entry-before-close", type=int, default=None)
    s.add_argument(
        "--direction",
        choices=["opening", "random", "inverse"],
        default="opening",
        help="'random'/'inverse' are placebo runs to test the directional edge",
    )

    a = p.add_argument_group("portfolio")
    a.add_argument("--equity", type=float, default=25_000)
    a.add_argument("--risk", type=float, default=0.01, help="Equity fraction risked per trade")
    a.add_argument("--max-leverage", type=float, default=4.0)
    a.add_argument(
        "--optimistic-intrabar",
        action="store_true",
        help="Don't assume the stop was hit inside the entry bar",
    )
    a.add_argument("--seed", type=int, default=0)

    t = p.add_argument_group("fills")
    t.add_argument(
        "--fills",
        choices=["bars", "ticks"],
        default="bars",
        help="'bars': the cost model on minute bars; 'ticks': real quotes and trades "
        "(downloads the windows it needs into --ticks unless --offline)",
    )
    t.add_argument("--ticks", type=Path, default=Path("data-store/ticks.sqlite"),
                   help="Tick store for --fills ticks")
    t.add_argument("--tick-latency-ms", type=int, default=100,
                   help="Delay from a decision to the quote a market order fills against")
    t.add_argument("--tick-rpm", type=float, default=180, help="Max tick requests per minute")
    t.add_argument("--tick-chunk", type=int, default=50, help="Symbols per tick request")
    t.add_argument("--tick-threads", type=int, default=4, help="Tick requests in flight at once")

    c = p.add_argument_group("costs")
    c.add_argument("--spread-bps", type=float, default=4.0, help="Full spread, bps of price")
    c.add_argument("--min-spread", type=float, default=0.01, help="Full spread floor, dollars")
    c.add_argument("--slippage-bps", type=float, default=1.0)
    c.add_argument("--stop-slippage-bps", type=float, default=2.0, help="Extra on stop fills")
    c.add_argument("--commission", type=float, default=0.0, help="Dollars per share")
    c.add_argument("--zero-costs", action="store_true", help="No costs (the papers' assumption)")

    p.add_argument("--log-level", default=None, help="Override TRADING_BOT_LOG_LEVEL")
    return p


def params_from_args(args: argparse.Namespace) -> tuple[OrbParams, CostModel, BacktestConfig]:
    """Translate parsed CLI args into validated parameter objects (raises ValueError)."""
    params = OrbParams(
        or_minutes=args.or_minutes[0],
        lookback_days=args.lookback,
        min_price=args.min_price,
        min_avg_volume=args.min_avg_volume,
        min_atr=args.min_atr,
        min_relvol=args.min_relvol,
        top_n=args.top_n,
        min_gap=args.min_gap,
        gap_aligned=args.gap_aligned,
        min_atr_pct=args.min_atr_pct,
        min_range_pct=args.min_range_pct,
        min_range_atr=args.min_range_atr,
        max_range_atr=args.max_range_atr,
        entry_mode=args.entry_mode,
        entry_window_minutes=args.entry_window,
        stop_mode=args.stop_mode,
        stop_atr_fraction=args.stop_atr,
        target_r=args.target_r,
        breakeven_r=args.breakeven_r,
        vwap_exit=args.vwap_exit,
        exit_minutes_before_close=args.exit_before_close,
        last_entry_minutes_before_close=args.last_entry_before_close,
        direction_mode=args.direction,
    )
    costs = (
        CostModel.zero()
        if args.zero_costs
        else CostModel(
            spread_bps=args.spread_bps,
            min_spread=args.min_spread,
            slippage_bps=args.slippage_bps,
            stop_slippage_bps=args.stop_slippage_bps,
            commission_per_share=args.commission,
        )
    )
    config = BacktestConfig(
        start=args.start,
        end=args.end,
        initial_equity=args.equity,
        risk_per_trade=args.risk,
        max_leverage=args.max_leverage,
        worst_case_intrabar=not args.optimistic_intrabar,
        seed=args.seed,
        fills=args.fills,
        tick_latency_ms=args.tick_latency_ms,
    )
    today = datetime.now(ET).date()
    if args.end >= today:
        raise ValueError(f"--end must be before today ({today}); recent data may be incomplete")
    return params, costs, config


def read_symbols(args: argparse.Namespace) -> list[str] | None:
    """Symbols from --symbols/--symbols-file, or None when --universe was chosen."""
    if args.symbols:
        raw = args.symbols.split(",")
    elif args.symbols_file:
        raw = [
            line.split("#", 1)[0] for line in args.symbols_file.read_text().splitlines()
        ]
    else:
        return None
    return sorted({s.strip().upper() for s in raw if s.strip()})


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        params, costs, config = params_from_args(args)
        symbols = read_symbols(args)
        if (args.universe in ("store", "store+delisted") or args.offline) and args.store is None:
            raise ValueError("--universe store, store+delisted and --offline need --store")
        if args.offline and args.universe in ("active", "stocks"):
            raise ValueError("--offline needs --universe store or explicit symbols")
    except (ValueError, OSError) as exc:
        print(f"Invalid arguments: {exc}", file=sys.stderr)
        return 2

    settings: Settings | None = None
    try:
        settings = load_settings(env_file=args.env_file)
    except ConfigError as exc:
        if not args.offline:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    if settings is None:
        setup_logging(args.log_level.upper() if args.log_level else "INFO", Path("logs"))
        log.info("Offline run without API settings")
    else:
        if args.log_level:
            settings = settings.with_overrides(log_level=args.log_level.upper())
        setup_logging(settings.log_level, settings.log_dir)

    from trading_bot.data.store import BarStore, StoreSource

    try:
        source: AlpacaHistoricalSource | StoreSource
        store: BarStore | None = None
        if args.store is not None:
            if not args.store.is_file():
                raise FileNotFoundError(f"store not found: {args.store}")
            store = BarStore(args.store)
        trading = None
        clients = None
        if args.offline:
            assert store is not None
            source = StoreSource(store)
        else:
            assert settings is not None
            from trading_bot.broker import create_clients
            from trading_bot.data.historical import AlpacaHistoricalSource

            clients = create_clients(settings)
            trading = clients.trading
            alpaca = AlpacaHistoricalSource(
                clients.stock_data,
                clients.trading,
                feed=args.feed,
                cache_dir=args.cache_dir,
                chunk_size=args.chunk_size,
                max_requests_per_minute=args.rpm,
            )
            source = alpaca if store is None else StoreSource(store, fallback=alpaca)
        if symbols is not None:
            universe = symbols
        elif args.universe in ("store", "store+delisted"):
            assert store is not None
            universe = store.universe(args.universe)
        else:
            from trading_bot.data.historical import active_us_equities

            assert trading is not None
            universe = active_us_equities(trading, stocks_only=args.universe == "stocks")
        if args.max_symbols:
            universe = universe[: args.max_symbols]
        log.info(
            "Backtesting ORB %s..%s on %d symbols (feed=%s)",
            config.start,
            config.end,
            len(universe),
            args.feed,
        )

        def progress(done: int, total: int, day: date) -> None:
            if done == total or done % 20 == 0:
                log.info("Day %d/%d (%s), %d data requests", done, total, day, source.requests)

        lengths: list[int] = args.or_minutes
        variants = [replace(params, or_minutes=m) for m in lengths]
        tick_source = None
        if config.fills == "ticks":
            tick_source = _prepare_ticks(args, source, universe, variants, costs, config, store,
                                         clients)
        results: list[tuple[str, BacktestResult]] = list(
            zip(
                [f"or{m}" for m in lengths],
                run_backtests(source, universe, variants, costs, config, progress,
                              ticks=tick_source),
                strict=True,
            )
        )

        closes: dict[date, float] = {}
        if args.benchmark:
            bench = source.daily_bars(
                [args.benchmark], config.start - timedelta(days=10), config.end
            )
            closes = {b.day: b.adj_close or b.close for b in bench.get(args.benchmark, [])}

        out_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
        extra = {
            "universe_size": len(universe),
            "feed": args.feed,
            "benchmark": args.benchmark or None,
            "data_requests": source.requests,
            "store": str(args.store) if args.store else None,
            "store_misses": dict(source.misses) if isinstance(source, StoreSource) else None,
            "universe": args.universe,
            "ticks": str(args.ticks) if tick_source is not None else None,
            "tick_misses": dict(tick_source.misses) if tick_source is not None else None,
        }
        rows: list[dict[str, Any]] = []
        reports: list[str] = []
        records: list[dict[str, Any]] = []
        for label, result in results:
            summary = summarize(result, closes or None)
            run_dir = out_dir / label if len(results) > 1 else out_dir
            fills = fill_stats(result.trades) if tick_source is not None else None
            write_outputs(result, summary, run_dir, extra={**extra, "fill_stats": fills})
            rows.append({"label": label, **summary.to_dict()})
            text = format_summary(summary, result)
            if fills is not None:
                text += "\n" + format_fill_stats(fills)
            reports.append(f"== {label}\n{text}")
            records.append(
                ledger.make_record(
                    tool="backtest",
                    params=result.params,
                    costs=costs,
                    worst_case_intrabar=config.worst_case_intrabar,
                    start=config.start.isoformat(),
                    end=config.end.isoformat(),
                    trades=summary.trades,
                    avg_net_r=summary.avg_net_r,
                    label=label,
                    out=str(run_dir),
                    universe_size=len(universe),
                    fills=config.fills,
                    tick_latency_ms=config.tick_latency_ms if config.fills == "ticks" else None,
                )
            )
        if not args.no_ledger:
            ledger.append(args.ledger, records)

        if len(results) > 1:
            combined = combine_equal_weight([r for _, r in results])
            rows.append(
                {"label": "equal-weight", **curve_stats(config.initial_equity, combined)}
            )
            write_comparison(rows, combined, out_dir)
    except Exception:
        log.exception("Backtest failed")
        return 1
    if isinstance(source, StoreSource) and source.misses:
        log.info("Not in the store (%s): %s", "skipped" if args.offline else "fetched from Alpaca",
                 dict(source.misses))

    if len(results) == 1:
        print(reports[0].split("\n", 1)[1])
        print(f"\nWrote {out_dir}/ (trades.csv, equity.csv, summary.json)")
    else:
        print("\n\n".join(reports))
        print(f"\n{format_comparison(rows)}")
        print(
            f"\nWrote {out_dir}/ (comparison.csv, combined_equity.csv, and one folder per "
            "length with trades.csv, equity.csv, summary.json)"
        )
    return 0


def _prepare_ticks(
    args: argparse.Namespace,
    source: DataSource,
    universe: Sequence[str],
    variants: Sequence[OrbParams],
    costs: CostModel,
    config: BacktestConfig,
    store: BarStore | None,
    clients: Any,
) -> StoreTicks:
    """Plan the run's tick windows, fetch the missing ones (unless offline), open the store."""
    from trading_bot.backtest import tickplan
    from trading_bot.data.ticks import StoreTicks, TickStore

    ticks = TickStore(args.ticks)
    log.info("Tick fills: planning windows with an optimistic bar pass")
    needed = tickplan.plan_backtest(source, universe, variants, costs, config)
    todo = tickplan.missing(ticks, needed)

    def request_symbol(key: str) -> tuple[str, str | None]:
        return store.request_symbol(key) if store is not None else (key, None)

    groups = tickplan.request_groups(todo, request_symbol, args.tick_chunk)
    log.info(tickplan.describe(sorted(needed), todo, len(groups), args.tick_rpm))
    if todo and not args.offline:
        from trading_bot.data.download import BarFetcher

        assert clients is not None
        fetcher = BarFetcher(clients.stock_data, feed=args.feed, pages_per_minute=args.tick_rpm)
        tickplan.fetch(fetcher, ticks, todo, request_symbol, args.tick_chunk, args.tick_threads)
    elif todo:
        log.warning("Offline: %d tick windows are missing; those fills use the bar model",
                    len(todo))
    return StoreTicks(ticks)


if __name__ == "__main__":
    raise SystemExit(main())

"""``trading-bot-download``: fill a ``BarStore`` with everything ORB backtests need.

Data only: never places orders. Steps (all resumable; re-running skips what is stored):

1. The universe (``--universe stocks`` or ``active``) plus extra symbols (liquid ETFs by
   default). The list is saved in the store and reused on later runs, so a resumed download
   does not drift as listings change.
2. The trading calendar and daily bars (raw, plus the split-adjusted close) from 14 sessions
   before ``--start`` through ``--end``. With ``--include-delisted``, also the stocks that
   stopped trading before today (``delisted.py``), once; later runs reuse the saved list.
3. A plan: the symbol-days the backtester will ask for, using its own daily filters
   (``daily_eligibility``/``needed_by_day``) plus every extra symbol on every day.
4. Full-session minute bars for the plan, ``--chunk-size`` symbols per request so a request
   normally fits one 10,000-bar page.

Every page counts against ``--rpm`` (Alpaca's Basic plan allows 200 historical calls per
minute). HTTP 429/5xx and connection errors back off exponentially with jitter.
``--estimate`` stops after step 3 and prints the expected size and time.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import random
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from trading_bot.data.models import ET, DailyBar, TradingDay
from trading_bot.data.store import BarStore, MinuteRow

if TYPE_CHECKING:
    from alpaca.data.historical import StockHistoricalDataClient

log = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")

PAGE_LIMIT = 10_000
"""Alpaca's maximum data points per bars response (all symbols together)."""
BYTES_PER_SYMBOL_DAY = 6_500
"""Measured: about 379 minute bars per liquid symbol-day at about 17 bytes compressed."""
DEFAULT_EXTRA_SYMBOLS = (
    "SPY,QQQ,IWM,DIA,TQQQ,SMH,XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLU,XLB,XLC,XLRE,GLD,TLT"
)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _at_or_after(ts: str, cut: str) -> bool:
    """``ts`` (RFC 3339 UTC with ``Z``, any fraction) is at or after ``cut`` (whole seconds)."""
    return ts[:19] >= cut[:19]


def is_retryable(exc: BaseException) -> bool:
    """Rate limits, server errors and network errors (requests' errors subclass OSError)."""
    status = getattr(exc, "status_code", None)
    return status in RETRY_STATUS or isinstance(exc, OSError)


class BarFetcher:
    """Fetches bars, trades or quotes one page at a time, spacing every page to
    ``pages_per_minute``. Thread-safe: with ``ordered_map`` several requests can be in flight,
    which matters because one request takes about 0.7 s (a single thread tops out near 80 per
    minute)."""

    def __init__(
        self,
        stock_data: StockHistoricalDataClient,
        *,
        feed: str = "sip",
        pages_per_minute: float = 180,
        max_retries: int = 8,
        max_backoff: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if pages_per_minute <= 0:
            raise ValueError("pages_per_minute must be > 0")
        self._client = stock_data
        self._feed = feed
        self._interval = 60.0 / pages_per_minute
        self._max_retries = max_retries
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._next_at = 0.0
        self._lock = threading.Lock()
        self.pages = 0
        self.retries = 0

    def bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        adjustment: str = "raw",
        asof: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Raw bar dicts (``t, o, h, l, c, v, vw, ...``) per symbol, following every page.

        ``asof="-"`` skips Alpaca's symbol mapping (the data of whoever traded under the ticker);
        the default (today) maps a current symbol to its entity, including former names."""
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "adjustment": adjustment,
            "feed": self._feed,
            "limit": PAGE_LIMIT,
        }
        if asof is not None:
            params["asof"] = asof
        return self._paged("/stocks/bars", "bars", params)

    def ticks(
        self,
        kind: str,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        asof: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Raw ``trades`` or ``quotes`` per symbol in ``[start, end)``, following every page.

        Alpaca's ``end`` is inclusive, so ticks stamped exactly at ``end`` are dropped here."""
        if kind not in ("trades", "quotes"):
            raise ValueError("kind must be 'trades' or 'quotes'")
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "feed": self._feed,
            "limit": PAGE_LIMIT,
        }
        if asof is not None:
            params["asof"] = asof
        out = self._paged(f"/stocks/{kind}", kind, params)
        cut = _rfc3339(end)
        return {
            sym: [r for r in rows if not _at_or_after(r["t"], cut)] for sym, rows in out.items()
        }

    def _paged(
        self, path: str, key: str, params: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        while True:
            response = self.request(path, params)
            for sym, rows in (response.get(key) or {}).items():
                out[sym].extend(rows)
            token = response.get("next_page_token")
            if not token:
                return dict(out)
            params["page_token"] = token

    def request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One throttled GET with retries (any client with ``get(path, params)``)."""
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            try:
                response = self._client.get(path, dict(params))
                with self._lock:
                    self.pages += 1
                return response if isinstance(response, dict) else {}
            except Exception as exc:
                if not is_retryable(exc) or attempt == self._max_retries:
                    raise
                with self._lock:
                    self.retries += 1
                delay = min(self._max_backoff, 5.0 * 2**attempt) * (1 + 0.25 * self._jitter())
                log.warning("Request %s failed (%s); retrying in %.0f s", path, exc, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        """Reserve the next request slot (thread-safe), then sleep until it."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_at)
            self._next_at = slot + self._interval
        if slot > now:
            self._sleep(slot - now)


def ordered_map(
    fn: Callable[[T], R], items: Iterable[T], workers: int, ahead: int | None = None
) -> Iterator[tuple[T, R]]:
    """``(item, fn(item))`` in input order, computed by ``workers`` threads with at most
    ``ahead`` (default ``4 x workers``) in flight. The caller consumes results in one thread,
    so writes (e.g. to SQLite) stay single-threaded."""
    if workers <= 1:
        for item in items:
            yield item, fn(item)
        return
    queue: deque[tuple[T, Future[R]]] = deque()
    source = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in itertools.islice(source, ahead or 4 * workers):
            queue.append((item, pool.submit(fn, item)))
        while queue:
            item, future = queue.popleft()
            result = future.result()
            for more in itertools.islice(source, 1):
                queue.append((more, pool.submit(fn, more)))
            yield item, result


def daily_from_raw(
    raw: Mapping[str, Sequence[Mapping[str, Any]]],
    split: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[DailyBar]]:
    """Merge raw and split-adjusted daily bars into ``DailyBar`` records (ET session dates)."""
    out: dict[str, list[DailyBar]] = {}
    for sym, rows in raw.items():
        adj = {_parse_ts(r["t"]).astimezone(ET).date(): float(r["c"]) for r in split.get(sym, [])}
        bars = []
        for r in rows:
            day = _parse_ts(r["t"]).astimezone(ET).date()
            bars.append(
                DailyBar(day, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                         float(r["v"]), adj.get(day))
            )
        out[sym] = bars
    return out


def minutes_from_raw(
    day: TradingDay, raw: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, list[MinuteRow]]:
    """Minute rows as seconds after the open, keeping bars that start inside the session."""
    out: dict[str, list[MinuteRow]] = {}
    for sym, rows in raw.items():
        kept = []
        for r in rows:
            ts = _parse_ts(r["t"])
            if day.open <= ts < day.close:
                offset = int((ts - day.open).total_seconds())
                vwap = r.get("vw")
                kept.append(
                    [offset, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                     float(r["v"]), None if vwap is None else float(vwap)]
                )
        out[sym] = kept
    return out


def plan_minutes(
    days: Sequence[TradingDay],
    daily: Mapping[str, Sequence[DailyBar]],
    first_test: int,
    extras: Iterable[str],
    *,
    min_price: float,
    min_avg_volume: float,
    min_atr: float,
    lookback_days: int,
) -> dict[date, set[str]]:
    """Symbol-days the backtester will request, plus every extra symbol from the first
    RelVol history day onward."""
    from trading_bot.backtest.engine import daily_eligibility, needed_by_day
    from trading_bot.strategies.orb import OrbParams

    params = OrbParams(
        min_price=min_price,
        min_avg_volume=min_avg_volume,
        min_atr=min_atr,
        lookback_days=lookback_days,
    )
    eligible = daily_eligibility(days, daily, params, first_test)
    needed = needed_by_day(eligible, lookback_days)
    extra = set(extras)
    start = max(0, first_test - lookback_days)
    plan: dict[date, set[str]] = {}
    for j in range(start, len(days)):
        plan[days[j].day] = set(needed.get(j, ())) | extra
    return plan


def _calendar(trading: Any, start: date, end: date) -> list[TradingDay]:
    from alpaca.trading.requests import GetCalendarRequest

    sessions = trading.get_calendar(GetCalendarRequest(start=start, end=end))
    # Alpaca returns naive open/close times in US/Eastern.
    return [
        TradingDay(c.date, c.open.replace(tzinfo=ET), c.close.replace(tzinfo=ET)) for c in sessions
    ]


def _chunks(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def build_parser() -> argparse.ArgumentParser:
    from trading_bot.strategies.orb import OrbParams

    d = OrbParams()
    p = argparse.ArgumentParser(
        prog="trading-bot-download",
        description="Download daily and minute bars into a local store for offline backtests "
        "(data only; never places orders). Resumable: re-run to continue.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", required=True, type=date.fromisoformat, help="First test day")
    p.add_argument("--end", required=True, type=date.fromisoformat, help="Last day")
    p.add_argument("--universe", choices=["stocks", "active"], default="stocks")
    p.add_argument(
        "--extra-symbols",
        default=DEFAULT_EXTRA_SYMBOLS,
        help="Comma-separated symbols downloaded every day ('' for none)",
    )
    p.add_argument("--refresh-universe", action="store_true",
                   help="Re-read the symbol list from Alpaca instead of the one saved in the store")
    p.add_argument("--include-delisted", action="store_true",
                   help="Also download stocks delisted before today (Alpaca's inactive assets "
                   "and corporate actions); backtest them with --universe store+delisted")
    p.add_argument("--delisted-csv", type=Path,
                   help="Extra delisted stocks: a CSV of symbol,last_day (with --include-delisted)")
    p.add_argument("--refresh-delisted", action="store_true",
                   help="Rebuild the delisted list instead of reusing the one saved in the store")
    p.add_argument("--db", type=Path, default=Path("data-store/bars.sqlite"))
    p.add_argument("--env-file", help="Env file with API keys (e.g. ~/.config/trading-bot/.env)")
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--rpm", type=float, default=180, help="Max pages per minute (Basic: 200)")
    p.add_argument("--workers", type=int, default=4,
                   help="Requests in flight at once (one takes ~0.7 s; --rpm still caps the rate)")
    p.add_argument("--chunk-size", type=int, default=25, help="Symbols per minute-bar request")
    p.add_argument("--daily-chunk-size", type=int, default=200, help="Symbols per daily request")
    p.add_argument("--estimate", action="store_true",
                   help="Stop after planning; print the expected size and time")
    p.add_argument("--min-price", type=float, default=d.min_price)
    p.add_argument("--min-avg-volume", type=float, default=d.min_avg_volume)
    p.add_argument("--min-atr", type=float, default=d.min_atr)
    p.add_argument("--lookback", type=int, default=d.lookback_days)
    p.add_argument("--log-level", default=None, help="Override TRADING_BOT_LOG_LEVEL")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.end < args.start:
        print("Invalid arguments: --end is before --start", file=sys.stderr)
        return 2
    if args.end >= datetime.now(ET).date():
        print("Invalid arguments: --end must be before today", file=sys.stderr)
        return 2
    if args.chunk_size <= 0 or args.daily_chunk_size <= 0 or args.workers <= 0:
        print("Invalid arguments: chunk sizes and --workers must be > 0", file=sys.stderr)
        return 2
    if (args.delisted_csv or args.refresh_delisted) and not args.include_delisted:
        print("Invalid arguments: --delisted-csv and --refresh-delisted need --include-delisted",
              file=sys.stderr)
        return 2
    if args.delisted_csv and not args.delisted_csv.is_file():
        print(f"Invalid arguments: {args.delisted_csv} not found", file=sys.stderr)
        return 2

    from trading_bot.config import ConfigError, load_settings
    from trading_bot.logging_setup import setup_logging

    try:
        settings = load_settings(env_file=args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.log_level:
        settings = settings.with_overrides(log_level=args.log_level.upper())
    setup_logging(settings.log_level, settings.log_dir)

    try:
        return _run(args, settings)
    except KeyboardInterrupt:
        log.warning("Interrupted. Everything downloaded so far is saved; re-run to resume.")
        return 130
    except Exception:
        log.exception("Download failed. Re-run the same command to resume.")
        return 1


def _run(args: argparse.Namespace, settings: Any) -> int:
    from trading_bot.backtest.engine import history_start
    from trading_bot.broker import create_clients
    from trading_bot.data.historical import active_us_equities

    clients = create_clients(settings)
    store = BarStore(args.db)
    fetcher = BarFetcher(clients.stock_data, feed=args.feed, pages_per_minute=args.rpm)
    extras = sorted({s.strip().upper() for s in args.extra_symbols.split(",") if s.strip()})

    # 1. Universe (saved in the store so resumed runs use the same list).
    saved = store.get_meta("universe")
    if saved and saved.get("kind") == args.universe and not args.refresh_universe:
        symbols = list(saved["symbols"])
        log.info("Using the %d %s symbols saved on %s", len(symbols), args.universe, saved["as_of"])
    else:
        symbols = active_us_equities(clients.trading, stocks_only=args.universe == "stocks")
        store.set_meta(
            "universe",
            {"kind": args.universe, "symbols": symbols, "as_of": date.today().isoformat()},
        )
        log.info("Saved %d %s symbols from Alpaca", len(symbols), args.universe)
    store.set_meta("extra_symbols", extras)
    everything = sorted(set(symbols) | set(extras))

    # 2. Calendar and daily bars.
    hist_start = history_start(args.start, args.lookback)
    days = _calendar(clients.trading, hist_start, args.end)
    store.put_calendar(days, hist_start, args.end)
    coverage = store.daily_coverage()
    todo = [s for s in everything if coverage.get(s) != (hist_start, args.end)]
    log.info("Daily bars: %d of %d symbols to fetch (%s..%s)",
             len(todo), len(everything), hist_start, args.end)
    start_dt = datetime.combine(hist_start, dtime(0, 0), tzinfo=ET)
    end_dt = datetime.combine(args.end, dtime(23, 59), tzinfo=ET)
    for n, chunk in enumerate(_chunks(todo, args.daily_chunk_size), 1):
        raw = fetcher.bars(chunk, "1Day", start_dt, end_dt, "raw")
        split = fetcher.bars(chunk, "1Day", start_dt, end_dt, "split")
        merged = daily_from_raw(raw, split)
        for sym in chunk:
            store.put_daily(sym, merged.get(sym, []), hist_start, args.end)
        if n % 5 == 0:
            got = min(n * args.daily_chunk_size, len(todo))
            log.info("Daily bars: %d/%d symbols, %d pages", got, len(todo), fetcher.pages)

    # 2b. Delisted stocks (opt-in, once).
    if args.include_delisted:
        _download_delisted(args, settings, clients, fetcher, store, everything, hist_start)
    delisted = store.delisted()
    planned = sorted(set(everything) | set(delisted))

    # 3. Plan.
    first_test = next((i for i, d in enumerate(days) if d.day >= args.start), len(days))
    log.info("Planning minute bars (applying the backtester's daily filters)")
    plan = plan_minutes(
        days,
        store.daily(planned, hist_start, args.end),
        first_test,
        extras,
        min_price=args.min_price,
        min_avg_volume=args.min_avg_volume,
        min_atr=args.min_atr,
        lookback_days=args.lookback,
    )
    by_date = {d.day: d for d in days}
    remaining = {day: sorted(syms - store.minute_symbols(day)) for day, syms in plan.items()}
    total = sum(len(s) for s in plan.values())
    left = sum(len(s) for s in remaining.values())
    requests = sum(math.ceil(len(s) / args.chunk_size) for s in remaining.values())
    hours = requests / args.rpm / 60
    print(
        f"Plan: {len(plan)} trading days ({min(plan)}..{max(plan)}), {total:,} symbol-days "
        f"(median {sorted(len(s) for s in plan.values())[len(plan) // 2]:,} symbols/day)\n"
        f"To download: {left:,} symbol-days in about {requests:,} requests, "
        f"~{left * BYTES_PER_SYMBOL_DAY / 1e9:.1f} GB, ~{hours:.1f} hours at {args.rpm:g}/min\n"
        f"Store now: {store.stats()['bytes'] / 1e9:.2f} GB at {store.path}",
        flush=True,
    )
    if args.estimate:
        return 0

    # 4. Minute bars, day by day: requests in parallel (--workers), writes in order.
    download_minutes(fetcher, store, by_date, remaining, delisted, args.chunk_size, args.workers)

    stats = store.stats()
    print(
        f"Done. {stats['minute_symbol_days']:,} symbol-days over {stats['minute_days']} days, "
        f"{stats['daily_symbols']:,} daily symbols, {stats['bytes'] / 1e9:.2f} GB at {store.path}"
    )
    return 0


def download_minutes(
    fetcher: BarFetcher,
    store: BarStore,
    by_date: Mapping[date, TradingDay],
    remaining: Mapping[date, Sequence[str]],
    delisted: Mapping[str, Mapping[str, Any]],
    chunk_size: int,
    workers: int,
) -> int:
    """Fetch the ``remaining`` symbol-days, ``workers`` requests in flight, written in order.

    Delisted store keys are requested by their ticker with ``asof="-"``. Returns the
    symbol-days stored."""
    left = sum(len(s) for s in remaining.values())
    started = time.monotonic()
    done = 0
    days_left = [(day_key, syms) for day_key, syms in sorted(remaining.items()) if syms]
    jobs: list[tuple[str, date, list[str]]] = []
    for day_key, syms in days_left:
        for chunk in _chunks([s for s in syms if s not in delisted], chunk_size):
            jobs.append(("current", day_key, chunk))
        # Delisted stocks: the ticker as traded that day (no symbol mapping), stored by key.
        for chunk in _chunks([s for s in syms if s in delisted], chunk_size):
            jobs.append(("delisted", day_key, chunk))
    last_job = {job[1]: i for i, job in enumerate(jobs)}
    day_number = {day_key: n for n, (day_key, _) in enumerate(days_left, 1)}

    def fetch_job(job: tuple[str, date, list[str]]) -> dict[str, list[MinuteRow]]:
        kind, day_key, chunk = job
        day = by_date[day_key]
        if kind == "current":
            rows = minutes_from_raw(day, fetcher.bars(chunk, "1Min", day.open, day.close, "raw"))
            return {sym: rows.get(sym, []) for sym in chunk}
        # Runs in a worker thread: no store access here (SQLite connections are per thread).
        tickers = {str(delisted[key]["symbol"]): key for key in chunk}
        raw = fetcher.bars(sorted(tickers), "1Min", day.open, day.close, "raw", asof="-")
        rows = minutes_from_raw(day, raw)
        return {key: rows.get(t, []) for t, key in tickers.items()}

    for i, (job, rows) in enumerate(ordered_map(fetch_job, jobs, workers)):
        _, day_key, chunk = job
        store.put_minutes(by_date[day_key], rows)
        done += len(chunk)
        n = day_number[day_key]
        if last_job[day_key] == i and (n % 20 == 0 or n == len(days_left)):
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            eta = (left - done) / rate / 3600 if rate > 0 else float("nan")
            log.info(
                "Minute bars: day %d/%d (%s), %s/%s symbol-days, %d pages, %d retries, "
                "%.2f GB, ETA %.1f h",
                n, len(days_left), day_key, f"{done:,}", f"{left:,}", fetcher.pages,
                fetcher.retries, store.stats()["bytes"] / 1e9, eta,
            )

    return done


def _download_delisted(
    args: argparse.Namespace,
    settings: Any,
    clients: Any,
    fetcher: BarFetcher,
    store: BarStore,
    current_symbols: Sequence[str],
    hist_start: date,
) -> None:
    """Find delisted stocks, store their daily bars and save the list (``delisted.py``)."""
    from alpaca.data.historical.corporate_actions import CorporateActionsClient

    from trading_bot.data import delisted as dl
    from trading_bot.data.historical import FUND_NAME

    if store.get_meta("delisted") is not None and not args.refresh_delisted:
        log.info("Using the %d delisted stocks saved in the store", len(store.delisted()))
        return
    assets = clients.trading.get("/assets", {"status": "inactive", "asset_class": "us_equity"})
    inactive = dl.from_inactive_assets(assets if isinstance(assets, list) else [], FUND_NAME)
    actions_fetcher = BarFetcher(
        CorporateActionsClient(settings.api_key, settings.secret_key),  # type: ignore[arg-type]
        pages_per_minute=args.rpm,
    )
    actions = dl.fetch_corporate_actions(actions_fetcher.request, hist_start, args.end)
    extra = dl.read_csv(args.delisted_csv) if args.delisted_csv else []
    found = dl.candidates(inactive, actions, extra)
    log.info("Delisted candidates: %d (%d inactive assets, %d corporate actions, %d from CSV)",
             len(found), len(inactive), len(actions), len(extra))

    start_dt = datetime.combine(hist_start, dtime(0, 0), tzinfo=ET)
    end_dt = datetime.combine(args.end, dtime(23, 59), tzinfo=ET)
    bars: dict[str, list[DailyBar]] = {}
    tickers = sorted(found)
    for n, chunk in enumerate(_chunks(tickers, args.daily_chunk_size // 2 or 1), 1):
        raw = fetcher.bars(chunk, "1Day", start_dt, end_dt, "raw", asof="-")
        split = fetcher.bars(chunk, "1Day", start_dt, end_dt, "split", asof="-")
        bars.update(daily_from_raw(raw, split))
        if n % 10 == 0:
            log.info("Delisted daily bars: %d/%d tickers, %d pages",
                     min(n * (args.daily_chunk_size // 2 or 1), len(tickers)), len(tickers),
                     fetcher.pages)
    kept, counts = dl.resolve(found, bars, store.daily(current_symbols, hist_start, args.end))
    for record, series in kept:
        store.put_daily(record.key, series, hist_start, args.end)
    records = {r.key: r.to_meta() for r, _ in kept}
    store.set_meta("delisted", records)
    by_year = dl.coverage(records.values())
    sources = Counter(s for r, _ in kept for s in r.sources)
    store.set_meta("delisted_info", {
        "as_of": date.today().isoformat(), "candidates": len(found), "counts": dict(counts),
        "sources": dict(sources), "by_year": by_year,
    })
    print(f"Delisted: kept {len(kept):,} of {len(found):,} candidates "
          f"(dropped: {dict((k, v) for k, v in counts.items() if k != 'kept')}); "
          f"by last year: {by_year}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

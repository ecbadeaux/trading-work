"""Historical market data from Alpaca, converted to plain records and cached on disk.

Uses the SIP feed by default (full market volume, which relative volume needs). On the free
Basic plan, SIP history is available except for the most recent 15 minutes, so backtests must
end before today.

Intraday bars are raw (unadjusted), so prices and per-share costs are what was actually traded.
Daily bars carry both the raw close and the split-adjusted close, so callers can detect splits.

alpaca-py is imported lazily inside methods, so this module imports without the SDK.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from trading_bot.data.models import ET, Bar, DailyBar, TradingDay

if TYPE_CHECKING:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.models import BarSet
    from alpaca.trading.client import TradingClient
    from alpaca.trading.models import Calendar

log = logging.getLogger(__name__)

US_EQUITY_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "NYSEARCA", "AMEX", "BATS"})

# Alpaca has no ETF flag, so "stocks only" is a heuristic: NYSE Arca and Cboe BZX primary
# listings are almost all exchange-traded products, and fund names give away most of the rest.
ETP_EXCHANGES = frozenset({"ARCA", "NYSEARCA", "BATS"})
FUND_NAME = re.compile(
    r"\b(ETFs?|ETNs?|ETPs?|Funds?|iShares|ProShares|SPDR|Direxion)\b|Trust,? Series",
    re.IGNORECASE,
)


class DiskCache:
    """Gzipped JSON files under ``root``. ``root=None`` disables caching."""

    def __init__(self, root: Path | None) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        assert self.root is not None
        return self.root / f"{key}.json.gz"

    def get(self, key: str) -> Any | None:
        if self.root is None:
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        return json.loads(gzip.decompress(path.read_bytes()))

    def put(self, key: str, value: Any) -> None:
        if self.root is None:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(gzip.compress(json.dumps(value, separators=(",", ":")).encode()))
        tmp.replace(path)


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _symbols_key(symbols: Iterable[str]) -> str:
    return hashlib.sha1(",".join(sorted(symbols)).encode()).hexdigest()[:16]


def _bar_row(bar: Any) -> list[Any]:
    return [
        bar.timestamp.isoformat(),
        float(bar.open),
        float(bar.high),
        float(bar.low),
        float(bar.close),
        float(bar.volume),
        None if bar.vwap is None else float(bar.vwap),
    ]


def _row_bar(row: Sequence[Any]) -> Bar:
    ts, o, h, low, c, v, vwap = row
    return Bar(datetime.fromisoformat(ts), o, h, low, c, v, vwap)


class AlpacaHistoricalSource:
    """Implements the backtester's ``DataSource`` protocol on top of alpaca-py clients."""

    def __init__(
        self,
        stock_data: StockHistoricalDataClient,
        trading: TradingClient,
        *,
        feed: str = "sip",
        cache_dir: Path | None = None,
        chunk_size: int = 200,
        max_requests_per_minute: float = 150,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        self._stock = stock_data
        self._trading = trading
        self._feed = feed
        self._cache = DiskCache(cache_dir)
        self._chunk_size = chunk_size
        self._min_interval = 60.0 / max_requests_per_minute if max_requests_per_minute > 0 else 0
        self._sleep = sleep
        self._clock = clock
        self._next_request_at = 0.0
        self.requests = 0
        """Number of bar requests sent (excluding pagination inside alpaca-py)."""

    # -- DataSource -------------------------------------------------------------------------

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        key = f"calendar/{start.isoformat()}_{end.isoformat()}"
        rows = self._cache.get(key)
        if rows is None:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = cast(
                "list[Calendar]",
                self._trading.get_calendar(GetCalendarRequest(start=start, end=end)),
            )
            # Alpaca returns naive open/close times in US/Eastern.
            rows = [[c.date.isoformat(), c.open.isoformat(), c.close.isoformat()] for c in calendar]
            self._cache.put(key, rows)
        return [
            TradingDay(
                date.fromisoformat(d),
                datetime.fromisoformat(o).replace(tzinfo=ET),
                datetime.fromisoformat(c).replace(tzinfo=ET),
            )
            for d, o, c in rows
        ]

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]:
        start_dt = datetime.combine(start, dtime(0, 0), tzinfo=ET)
        end_dt = datetime.combine(end, dtime(23, 59), tzinfo=ET)
        out: dict[str, list[DailyBar]] = {}
        for chunk in _chunks(sorted(set(symbols)), self._chunk_size):
            key = f"daily/{self._feed}/{start.isoformat()}_{end.isoformat()}/{_symbols_key(chunk)}"
            rows: dict[str, list[list[Any]]] | None = self._cache.get(key)
            if rows is None:
                raw = self._fetch(chunk, "day", start_dt, end_dt, "raw")
                adjusted = self._fetch(chunk, "day", start_dt, end_dt, "split")
                rows = {}
                for sym, bars in raw.items():
                    adj_close = {
                        b.timestamp.astimezone(ET).date(): float(b.close)
                        for b in adjusted.get(sym, [])
                    }
                    rows[sym] = []
                    for b in bars:
                        day = b.timestamp.astimezone(ET).date()
                        rows[sym].append(
                            [
                                day.isoformat(),
                                float(b.open),
                                float(b.high),
                                float(b.low),
                                float(b.close),
                                float(b.volume),
                                adj_close.get(day),
                            ]
                        )
                self._cache.put(key, rows)
            for sym, sym_rows in rows.items():
                out[sym] = [
                    DailyBar(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5], r[6])
                    for r in sym_rows
                ]
        return out

    def opening_bars(
        self, symbols: Sequence[str], day: TradingDay, minutes: int
    ) -> dict[str, list[Bar]]:
        end = day.open + timedelta(minutes=minutes)
        return self._intraday(f"or{minutes}", symbols, day, day.open, end)

    def session_bars(self, symbols: Sequence[str], day: TradingDay) -> dict[str, list[Bar]]:
        return self._intraday("session", symbols, day, day.open, day.close)

    # -- internals --------------------------------------------------------------------------

    def _intraday(
        self, kind: str, symbols: Sequence[str], day: TradingDay, start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        """Minute bars in ``[start, end)`` for ``symbols``. One cache file per kind and day,
        recording which symbols were fetched so later runs only request the missing ones."""
        key = f"{kind}/{self._feed}/{day.day.isoformat()}"
        cached: dict[str, Any] = self._cache.get(key) or {"fetched": [], "bars": {}}
        fetched = set(cached["fetched"])
        missing = sorted(set(symbols) - fetched)
        for chunk in _chunks(missing, self._chunk_size):
            result = self._fetch(chunk, "minute", start, end, "raw")
            for sym, bars in result.items():
                cached["bars"][sym] = [_bar_row(b) for b in bars if b.timestamp < end]
            fetched.update(chunk)
            cached["fetched"] = sorted(fetched)
            self._cache.put(key, cached)  # after every chunk, so an interrupted run resumes
        wanted = set(symbols)
        return {
            sym: [_row_bar(r) for r in rows]
            for sym, rows in cached["bars"].items()
            if sym in wanted and rows
        }

    def _fetch(
        self,
        symbols: Sequence[str],
        timeframe: Literal["minute", "day"],
        start: datetime,
        end: datetime,
        adjustment: Literal["raw", "split"],
    ) -> dict[str, list[Any]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        self._throttle()
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Minute if timeframe == "minute" else TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment(adjustment),
            feed=DataFeed(self._feed),
        )
        barset = cast("BarSet", self._stock.get_stock_bars(request))
        self.requests += 1
        return dict(barset.data)

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        now = self._clock()
        wait = self._next_request_at - now
        if wait > 0:
            self._sleep(wait)
            now += wait
        self._next_request_at = now + self._min_interval


def active_us_equities(trading: TradingClient, *, stocks_only: bool = False) -> list[str]:
    """Symbols of currently active, tradable US equities on the major exchanges.

    This is today's list, so backtests built on it carry survivorship bias: stocks that were
    delisted before today are missing. ``stocks_only`` drops exchange-traded products and funds
    (see ``ETP_EXCHANGES`` and ``FUND_NAME``; a heuristic, as Alpaca has no ETF flag).

    Reads the raw JSON instead of ``get_all_assets()``: the SDK's exchange enum rejects the whole
    list when Alpaca adds an exchange it doesn't know yet (TXSE broke alpaca-py 0.44.0).
    """
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    params = GetAssetsRequest(
        status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY
    ).to_request_fields()
    assets = cast("list[dict[str, Any]]", trading.get("/assets", params))
    return sorted(
        a["symbol"]
        for a in assets
        if a.get("tradable")
        and a.get("exchange") in US_EQUITY_EXCHANGES
        and not (stocks_only and _is_fund(a))
    )


def _is_fund(asset: dict[str, Any]) -> bool:
    return asset.get("exchange") in ETP_EXCHANGES or bool(FUND_NAME.search(asset.get("name") or ""))

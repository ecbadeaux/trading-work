"""A local SQLite store of daily and minute bars, so backtests can run offline.

One file holds everything ``trading-bot-download`` fetched:

- ``calendar``: trading sessions (open and close, US/Eastern);
- ``daily``: one compressed blob per symbol (raw OHLCV plus the split-adjusted close);
- ``minute``: one row per (day, symbol) with two compressed blobs, the first ``head_minutes``
  of the session and the rest. Opening-range reads only decompress the head. A row with empty
  blobs records that the symbol was fetched but had no bars that day.

``StoreSource`` implements the backtester's ``DataSource`` protocol on top of the store, with an
optional fallback source for anything the store does not hold. Delisted stocks stored under
``TICKER@YYYYMMDD`` keys (``delisted.py``) are never sent to the fallback.

Standard library only; no alpaca-py imports.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_bot.data.models import ET, Bar, DailyBar, TradingDay

if TYPE_CHECKING:
    from trading_bot.backtest.engine import DataSource

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calendar (day TEXT PRIMARY KEY, open TEXT NOT NULL, close TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily (
    symbol TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS minute (
    day TEXT NOT NULL, symbol TEXT NOT NULL, head BLOB NOT NULL, rest BLOB NOT NULL,
    PRIMARY KEY (day, symbol)
) WITHOUT ROWID;
"""

DEFAULT_HEAD_MINUTES = 30
UNIVERSES = ("stocks", "store", "stocks+delisted", "store+delisted")
"""``stocks``: the saved symbol list; ``store``: plus the extra symbols (ETFs); ``+delisted``:
plus the delisted stocks the download added."""

MinuteRow = list[Any]
"""``[seconds after the session open, open, high, low, close, volume, vwap]``."""


def _pack(rows: Sequence[Sequence[Any]]) -> bytes:
    if not rows:
        return b""
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode(), 6)


def _unpack(blob: bytes) -> list[list[Any]]:
    if not blob:
        return []
    rows: list[list[Any]] = json.loads(zlib.decompress(blob))
    return rows


class BarStore:
    """The SQLite file. Safe to reopen; writes commit immediately (resumable downloads)."""

    def __init__(self, path: Path | str, *, head_minutes: int = DEFAULT_HEAD_MINUTES) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        stored = self.get_meta("head_minutes")
        if stored is None:
            self.set_meta("head_minutes", head_minutes)
            stored = head_minutes
        self.head_minutes = int(stored)

    def close(self) -> None:
        self._db.close()

    # -- meta -------------------------------------------------------------------------------

    def get_meta(self, key: str) -> Any | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def set_meta(self, key: str, value: Any) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value))
            )

    def universe(self, kind: str) -> list[str]:
        """Symbols (store keys) of one of ``UNIVERSES``."""
        if kind not in UNIVERSES:
            raise ValueError(f"universe must be one of {', '.join(UNIVERSES)}")
        saved = self.get_meta("universe") or {}
        symbols = set(saved.get("symbols") or [])
        if kind.startswith("store"):
            symbols |= set(self.get_meta("extra_symbols") or [])
        if kind.endswith("+delisted"):
            symbols |= set(self.delisted())
        return sorted(symbols)

    def delisted(self) -> dict[str, dict[str, Any]]:
        """Store key -> ``{symbol, first, last, sources}`` for the delisted stocks."""
        value = self.get_meta("delisted")
        return dict(value) if isinstance(value, dict) else {}

    def request_symbol(self, key: str) -> tuple[str, str | None]:
        """What to ask Alpaca for a store key: ``(ticker, asof)``. Delisted stocks use
        ``asof="-"`` (the ticker as traded, no symbol mapping)."""
        record = self.delisted().get(key)
        if record is not None:
            return str(record["symbol"]), "-"
        return key, None

    # -- calendar ---------------------------------------------------------------------------

    def put_calendar(self, days: Iterable[TradingDay], start: date, end: date) -> None:
        """Store the sessions between ``start`` and ``end`` and remember that range."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO calendar (day, open, close) VALUES (?, ?, ?)",
                [(d.day.isoformat(), d.open.isoformat(), d.close.isoformat()) for d in days],
            )
        self.set_meta("calendar_range", [start.isoformat(), end.isoformat()])

    def calendar_range(self) -> tuple[date, date] | None:
        rng = self.get_meta("calendar_range")
        return None if rng is None else (date.fromisoformat(rng[0]), date.fromisoformat(rng[1]))

    def calendar(self, start: date, end: date) -> list[TradingDay]:
        rows = self._db.execute(
            "SELECT day, open, close FROM calendar WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [
            TradingDay(
                date.fromisoformat(d),
                datetime.fromisoformat(o).astimezone(ET),
                datetime.fromisoformat(c).astimezone(ET),
            )
            for d, o, c in rows
        ]

    # -- daily ------------------------------------------------------------------------------

    def put_daily(self, symbol: str, bars: Sequence[DailyBar], start: date, end: date) -> None:
        rows = [
            [b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.adj_close] for b in bars
        ]
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO daily (symbol, start, end, data) VALUES (?, ?, ?, ?)",
                (symbol, start.isoformat(), end.isoformat(), _pack(rows)),
            )

    def daily_coverage(self) -> dict[str, tuple[date, date]]:
        """Symbol -> (start, end) of the stored daily window."""
        return {
            s: (date.fromisoformat(a), date.fromisoformat(b))
            for s, a, b in self._db.execute("SELECT symbol, start, end FROM daily")
        }

    def daily(self, symbols: Iterable[str], start: date, end: date) -> dict[str, list[DailyBar]]:
        """Stored daily bars in ``[start, end]`` for the symbols the store has."""
        out: dict[str, list[DailyBar]] = {}
        lo, hi = start.isoformat(), end.isoformat()
        for chunk in _chunked(sorted(set(symbols)), 500):
            marks = ",".join("?" * len(chunk))
            for sym, blob in self._db.execute(
                f"SELECT symbol, data FROM daily WHERE symbol IN ({marks})", chunk
            ):
                out[sym] = [
                    DailyBar(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5], r[6])
                    for r in _unpack(blob)
                    if lo <= r[0] <= hi
                ]
        return out

    # -- minute -----------------------------------------------------------------------------

    def put_minutes(self, day: TradingDay, bars: Mapping[str, Sequence[MinuteRow]]) -> None:
        """Store minute rows (``MinuteRow``) per symbol. Empty lists mark "fetched, no bars"."""
        cut = self.head_minutes * 60
        records = []
        for sym, rows in bars.items():
            head = [r for r in rows if r[0] < cut]
            rest = [r for r in rows if r[0] >= cut]
            records.append((day.day.isoformat(), sym, _pack(head), _pack(rest)))
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO minute (day, symbol, head, rest) VALUES (?, ?, ?, ?)",
                records,
            )

    def minute_symbols(self, day: date) -> set[str]:
        """Symbols already fetched for ``day`` (with or without bars)."""
        rows = self._db.execute("SELECT symbol FROM minute WHERE day = ?", (day.isoformat(),))
        return {r[0] for r in rows}

    def minutes(
        self, day: TradingDay, symbols: Iterable[str], until_minutes: int | None = None
    ) -> tuple[dict[str, list[Bar]], set[str]]:
        """Bars starting in the first ``until_minutes`` (``None``: the whole session).

        Returns ``(bars, stored)``: bars for symbols that had any, and the set of requested
        symbols the store holds for that day (so callers can tell "no bars" from "not stored").
        """
        full = until_minutes is None or until_minutes > self.head_minutes
        column = "head, rest" if full else "head"
        limit = None if until_minutes is None else until_minutes * 60
        bars: dict[str, list[Bar]] = {}
        stored: set[str] = set()
        for chunk in _chunked(sorted(set(symbols)), 500):
            marks = ",".join("?" * len(chunk))
            query = f"SELECT symbol, {column} FROM minute WHERE day = ? AND symbol IN ({marks})"
            for row in self._db.execute(query, (day.day.isoformat(), *chunk)):
                sym = row[0]
                stored.add(sym)
                rows = _unpack(row[1]) + (_unpack(row[2]) if full else [])
                out = [
                    Bar(day.open + timedelta(seconds=r[0]), r[1], r[2], r[3], r[4], r[5], r[6])
                    for r in rows
                    if limit is None or r[0] < limit
                ]
                if out:
                    bars[sym] = out
        return bars, stored

    def stats(self) -> dict[str, Any]:
        def one(sql: str) -> Any:
            return self._db.execute(sql).fetchone()[0]

        return {
            "path": str(self.path),
            "bytes": sum(
                p.stat().st_size
                for p in (self.path, Path(f"{self.path}-wal"))
                if p.exists()
            ),
            "calendar_days": one("SELECT COUNT(*) FROM calendar"),
            "daily_symbols": one("SELECT COUNT(*) FROM daily"),
            "minute_days": one("SELECT COUNT(DISTINCT day) FROM minute"),
            "minute_symbol_days": one("SELECT COUNT(*) FROM minute"),
        }


def _covers(window: tuple[date, date], start: date, end: date) -> bool:
    return window[0] <= start and end <= window[1]


def _chunked(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


class StoreSource:
    """``DataSource`` backed by a ``BarStore``; asks ``fallback`` (if any) for what is missing.

    ``misses`` counts symbol-days and symbols the store could not serve, by method.
    """

    def __init__(self, store: BarStore, fallback: DataSource | None = None) -> None:
        self.store = store
        self.fallback = fallback
        self.misses: Counter[str] = Counter()

    @property
    def requests(self) -> int:
        return int(getattr(self.fallback, "requests", 0))

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        rng = self.store.calendar_range()
        if rng is not None and rng[0] <= start and end <= rng[1]:
            return self.store.calendar(start, end)
        self.misses["calendar"] += 1
        if self.fallback is not None:
            return self.fallback.trading_days(start, end)
        return self.store.calendar(start, end)

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]:
        coverage = self.store.daily_coverage()
        inside = [s for s in symbols if s in coverage and _covers(coverage[s], start, end)]
        out = self.store.daily(inside, start, end)
        outside = sorted(set(symbols) - set(inside))
        if outside:
            self.misses["daily_symbols"] += len(outside)
            fetchable = [s for s in outside if "@" not in s]
            if self.fallback is not None and fetchable:
                out.update(self.fallback.daily_bars(fetchable, start, end))
        return out

    def opening_bars(
        self, symbols: Sequence[str], day: TradingDay, minutes: int
    ) -> dict[str, list[Bar]]:
        return self._minutes(symbols, day, minutes)

    def session_bars(self, symbols: Sequence[str], day: TradingDay) -> dict[str, list[Bar]]:
        return self._minutes(symbols, day, None)

    def _minutes(
        self, symbols: Sequence[str], day: TradingDay, minutes: int | None
    ) -> dict[str, list[Bar]]:
        bars, stored = self.store.minutes(day, symbols, minutes)
        missing = sorted(set(symbols) - stored)
        if missing:
            self.misses["minute_symbol_days"] += len(missing)
            missing = [s for s in missing if "@" not in s]
            if self.fallback is not None and missing:
                extra = (
                    self.fallback.session_bars(missing, day)
                    if minutes is None
                    else self.fallback.opening_bars(missing, day, minutes)
                )
                bars.update(extra)
        return bars

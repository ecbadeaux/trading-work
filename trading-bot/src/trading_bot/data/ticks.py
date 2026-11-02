"""Trades and quotes for the moments a backtest trades in, stored locally.

Every quote of every stock is far too much to download, so only *windows* are fetched:

- ``minute_windows``: all trades in the minute a trade's entry or stop triggers, plus the quotes
  from ``QUOTE_LEAD`` seconds before that minute to its end (the quote in force when the first
  trade prints may be older than the minute);
- ``moment_window``: quotes around a market exit (``QUOTE_LEAD`` before to ``MOMENT_TAIL`` after).

``backtest/tickplan.py`` works out which windows a backtest needs and fetches them;
``backtest/ticks.py`` turns them into fills. ``StoreTicks`` serves windows to the simulator and
counts the ones the store lacks.

Standard library only; no alpaca-py imports.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

Kind = Literal["trades", "quotes"]

QUOTE_LEAD = timedelta(seconds=10)
MOMENT_TAIL = timedelta(seconds=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS windows (
    symbol TEXT NOT NULL, kind TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    data BLOB NOT NULL, PRIMARY KEY (symbol, kind, start, end)
) WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class TradeTick:
    t: int
    """Epoch microseconds (UTC)."""
    price: float
    size: int
    conditions: str
    """Condition codes without the "regular sale" codes (' ' and '@'); '' for a regular sale."""


@dataclass(frozen=True, slots=True)
class QuoteTick:
    t: int
    """Epoch microseconds (UTC)."""
    bid: float
    ask: float
    bid_size: int
    ask_size: int


@dataclass(frozen=True, slots=True)
class MinuteTicks:
    trades: Sequence[TradeTick]
    quotes: Sequence[QuoteTick]


@dataclass(frozen=True, order=True)
class Window:
    """``[start, end)`` of one kind of tick data for one symbol (``start``/``end`` in UTC)."""

    symbol: str
    """The store key (a delisted symbol may be stored as ``SYM@YYYYMMDD``)."""
    kind: Kind
    start: datetime
    end: datetime

    def key(self) -> tuple[str, str, str, str]:
        return (self.symbol, self.kind, _iso(self.start), _iso(self.end))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_windows(symbol: str, minute: datetime) -> tuple[Window, Window]:
    """Trades and quotes for the minute starting at ``minute``."""
    end = minute + timedelta(minutes=1)
    return (
        Window(symbol, "trades", minute, end),
        Window(symbol, "quotes", minute - QUOTE_LEAD, end),
    )


def moment_window(symbol: str, at: datetime) -> Window:
    """Quotes around ``at`` (a market order's time)."""
    return Window(symbol, "quotes", at - QUOTE_LEAD, at + MOMENT_TAIL)


def epoch_us(dt: datetime) -> int:
    delta = dt.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_ts_us(text: str) -> int:
    """RFC 3339 (Alpaca sends nanoseconds and ``Z``) to epoch microseconds."""
    if text.endswith("Z"):
        base, _, frac = text[:-1].partition(".")
        whole = datetime.fromisoformat(base).replace(tzinfo=UTC)
        return epoch_us(whole) + (int((frac + "000000")[:6]) if frac else 0)
    return epoch_us(datetime.fromisoformat(text))


def trade_rows(raw: Iterable[Mapping[str, Any]]) -> list[list[Any]]:
    """API trades to stored rows ``[t_us, price, size, conditions]``, time-ordered.

    Drops trades the tape later canceled or replaced (``u`` = canceled/incorrect)."""
    rows: list[list[Any]] = []
    for r in raw:
        if r.get("u") in ("canceled", "incorrect"):
            continue
        codes = "".join(c for c in (r.get("c") or []) if c not in (" ", "@"))
        rows.append([parse_ts_us(r["t"]), float(r["p"]), int(r["s"]), codes])
    rows.sort(key=lambda row: row[0])
    return rows


def quote_rows(raw: Iterable[Mapping[str, Any]]) -> list[list[Any]]:
    """API quotes to stored rows ``[t_us, bid, ask, bid_size, ask_size]``, time-ordered."""
    rows: list[list[Any]] = [
        [parse_ts_us(r["t"]), float(r["bp"]), float(r["ap"]), int(r["bs"]), int(r["as"])]
        for r in raw
    ]
    rows.sort(key=lambda row: row[0])
    return rows


def _pack(start_us: int, rows: Sequence[Sequence[Any]]) -> bytes:
    if not rows:
        return b""
    shifted = [[r[0] - start_us, *r[1:]] for r in rows]
    return zlib.compress(json.dumps(shifted, separators=(",", ":")).encode(), 6)


def _unpack(start_us: int, blob: bytes) -> list[list[Any]]:
    if not blob:
        return []
    rows: list[list[Any]] = json.loads(zlib.decompress(blob))
    for r in rows:
        r[0] += start_us
    return rows


class TickStore:
    """The SQLite file of tick windows. Writes commit immediately (resumable downloads)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def put(self, window: Window, rows: Sequence[Sequence[Any]]) -> None:
        """Store parsed rows (``trade_rows``/``quote_rows``); ``[]`` marks "fetched, none"."""
        self.put_many([(window, rows)])

    def put_many(self, items: Iterable[tuple[Window, Sequence[Sequence[Any]]]]) -> None:
        records = [
            (*w.key(), _pack(epoch_us(w.start), rows)) for w, rows in items
        ]
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO windows (symbol, kind, start, end, data) "
                "VALUES (?, ?, ?, ?, ?)",
                records,
            )

    def rows(self, window: Window) -> list[list[Any]] | None:
        """Stored rows, or ``None`` if the window was never fetched."""
        row = self._db.execute(
            "SELECT data FROM windows WHERE symbol = ? AND kind = ? AND start = ? AND end = ?",
            window.key(),
        ).fetchone()
        return None if row is None else _unpack(epoch_us(window.start), row[0])

    def trades(self, window: Window) -> list[TradeTick] | None:
        rows = self.rows(window)
        return None if rows is None else [TradeTick(r[0], r[1], r[2], r[3]) for r in rows]

    def quotes(self, window: Window) -> list[QuoteTick] | None:
        rows = self.rows(window)
        return None if rows is None else [QuoteTick(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def stored(self) -> set[tuple[str, str, str, str]]:
        """Keys of every stored window (see ``Window.key``)."""
        return {
            (s, k, a, b)
            for s, k, a, b in self._db.execute("SELECT symbol, kind, start, end FROM windows")
        }

    def stats(self) -> dict[str, Any]:
        windows, empty = self._db.execute(
            "SELECT COUNT(*), SUM(LENGTH(data) = 0) FROM windows"
        ).fetchone()
        size = sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal")) if p.exists())
        return {"path": str(self.path), "bytes": size, "windows": windows, "empty": empty or 0}


class TickSource(Protocol):
    """What the simulator needs: ticks for one minute, or quotes around one moment."""

    def minute(self, symbol: str, minute: datetime) -> MinuteTicks | None: ...

    def moment(self, symbol: str, at: datetime) -> Sequence[QuoteTick] | None: ...


class StoreTicks:
    """``TickSource`` over a ``TickStore``. ``misses`` counts windows the store lacks."""

    def __init__(self, store: TickStore) -> None:
        self.store = store
        self.misses: Counter[str] = Counter()

    def minute(self, symbol: str, minute: datetime) -> MinuteTicks | None:
        tw, qw = minute_windows(symbol, minute)
        trades, quotes = self.store.trades(tw), self.store.quotes(qw)
        if trades is None or quotes is None:
            self.misses["minute"] += 1
            return None
        return MinuteTicks(trades, quotes)

    def moment(self, symbol: str, at: datetime) -> list[QuoteTick] | None:
        quotes = self.store.quotes(moment_window(symbol, at))
        if quotes is None:
            self.misses["moment"] += 1
        return quotes

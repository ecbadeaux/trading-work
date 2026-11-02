"""Synthetic ticks for tests: a dict-backed ``TickSource`` and a tick store filled from bars."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from helpers import et

from trading_bot.data.models import ET, TradingDay
from trading_bot.data.store import BarStore
from trading_bot.data.ticks import (
    MinuteTicks,
    QuoteTick,
    TickStore,
    TradeTick,
    Window,
    epoch_us,
)


def us(day: date, hour: int, minute: int, second: float = 0.0) -> int:
    return epoch_us(et(day, hour, minute)) + int(second * 1_000_000)


def trade(t: int, price: float, conditions: str = "", size: int = 100) -> TradeTick:
    return TradeTick(t, price, size, conditions)


def quote(t: int, bid: float, ask: float) -> QuoteTick:
    return QuoteTick(t, bid, ask, 5, 5)


class FakeTicks:
    """``TickSource`` from dicts: ``minutes[(symbol, minute)]`` and ``moments[(symbol, at)]``."""

    def __init__(self) -> None:
        self.minutes: dict[tuple[str, datetime], MinuteTicks] = {}
        self.moments: dict[tuple[str, datetime], list[QuoteTick]] = {}

    def minute(self, symbol: str, minute: datetime) -> MinuteTicks | None:
        return self.minutes.get((symbol, minute))

    def moment(self, symbol: str, at: datetime) -> list[QuoteTick] | None:
        return self.moments.get((symbol, at))


def fill_from_bars(bar_store: Path, tick_store: Path, windows: Iterable[Window]) -> int:
    """Store a plausible tape for each window, built from the bar store's minute bars.

    Minute windows: prints at the bar's open, then low and high (high first on a down bar),
    then close, each inside a one-cent quote. Moment windows: one quote around the bar's open.
    Returns the number of windows written."""
    bars = BarStore(bar_store)
    ticks = TickStore(tick_store)
    written = 0
    try:
        calendar = {d.day: d for d in bars.calendar(date(1990, 1, 1), date(2100, 1, 1))}
        for w in windows:
            et_start = w.start.astimezone(ET)
            if w.kind == "quotes" and (w.end - w.start) < timedelta(seconds=60):
                at = w.start + timedelta(seconds=10)  # a moment window: QUOTE_LEAD before
                price = _bar_open(bars, calendar, w.symbol, at)
                t = epoch_us(at) - 1_000_000
                rows = [] if price is None else [[t, price - 0.01, price + 0.01, 5, 5]]
            else:
                minute = et_start + (timedelta(seconds=10) if w.kind == "quotes" else timedelta())
                path = _path(bars, calendar, w.symbol, minute)
                t0 = epoch_us(minute)
                if w.kind == "trades":
                    rows = [[t0 + i * 10_000_000 + 1, p, 100, ""] for i, p in enumerate(path)]
                else:
                    rows = [[t0 + i * 10_000_000, p - 0.01, p + 0.01, 5, 5]
                            for i, p in enumerate(path)]
                    if path:
                        rows.insert(0, [t0 - 5_000_000, path[0] - 0.01, path[0] + 0.01, 5, 5])
            ticks.put(w, rows)
            written += 1
    finally:
        bars.close()
        ticks.close()
    return written


def _day(calendar: dict[date, TradingDay], at: datetime) -> TradingDay | None:
    return calendar.get(at.astimezone(ET).date())


def _minute_bar(bars: BarStore, calendar: dict[date, TradingDay], symbol: str,
                minute: datetime) -> Sequence[float] | None:
    day = _day(calendar, minute)
    if day is None:
        return None
    found, _ = bars.minutes(day, [symbol])
    for b in found.get(symbol, []):
        if b.timestamp == minute:
            return (b.open, b.high, b.low, b.close)
    return None


def _path(bars: BarStore, calendar: dict[date, TradingDay], symbol: str,
          minute: datetime) -> list[float]:
    ohlc = _minute_bar(bars, calendar, symbol, minute)
    if ohlc is None:
        return []
    o, h, low, c = ohlc
    return [o, h, low, c] if c < o else [o, low, h, c]


def _bar_open(bars: BarStore, calendar: dict[date, TradingDay], symbol: str,
              at: datetime) -> float | None:
    ohlc = _minute_bar(bars, calendar, symbol, at)
    return None if ohlc is None else ohlc[0]

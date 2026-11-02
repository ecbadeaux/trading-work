"""Builders for synthetic market data used across tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from trading_bot.data.models import ET, Bar, TradingDay


def et(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def bar(
    day: date,
    hour: int,
    minute: int,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 1000,
    vwap: float | None = None,
) -> Bar:
    return Bar(et(day, hour, minute), o, h, low, c, v, vwap)


def session(day: date) -> TradingDay:
    return TradingDay(day, et(day, 9, 30), et(day, 16, 0))


def weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out

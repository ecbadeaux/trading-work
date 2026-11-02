"""Plain market-data records shared by strategies and the backtester.

These deliberately do not depend on alpaca-py, so strategy logic can be tested and replayed
without the SDK. Conversion from alpaca-py models happens in ``trading_bot.data.historical``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Bar:
    """An intraday bar. ``timestamp`` is the bar's start time (timezone-aware)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A daily bar in raw (unadjusted) prices.

    ``adj_close`` is the split-adjusted close when known. Comparing it with ``close`` reveals
    splits, which would otherwise corrupt ATR and volume averages computed from raw data.
    """

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float | None = None


@dataclass(frozen=True, slots=True)
class TradingDay:
    """One regular trading session. ``open``/``close`` are timezone-aware (US/Eastern)."""

    day: date
    open: datetime
    close: datetime

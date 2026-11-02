from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest
from helpers import bar, session
from test_engine import DAYS, TEST_DAY, build_source, config

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import run_backtest
from trading_bot.data.models import Bar, DailyBar, TradingDay
from trading_bot.data.store import BarStore, StoreSource
from trading_bot.strategies.orb import OrbParams

D = date(2024, 3, 5)


def rows(day: TradingDay, bars: Sequence[Bar]) -> list[list[object]]:
    return [
        [int((b.timestamp - day.open).total_seconds()), b.open, b.high, b.low, b.close, b.volume,
         b.vwap]
        for b in bars
    ]


def test_calendar_and_daily_round_trip(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars.sqlite")
    days = [session(d) for d in DAYS[:3]]
    store.put_calendar(days, DAYS[0] - timedelta(days=3), DAYS[2])
    assert store.calendar(DAYS[0], DAYS[2]) == days
    assert store.calendar_range() == (DAYS[0] - timedelta(days=3), DAYS[2])

    bars = [DailyBar(d, 10, 11, 9, 10.5, 1e6, 5.25) for d in DAYS[:3]]
    store.put_daily("AAA", bars, DAYS[0], DAYS[2])
    assert store.daily(["AAA", "ZZZ"], DAYS[1], DAYS[2]) == {"AAA": bars[1:]}
    assert store.daily_coverage() == {"AAA": (DAYS[0], DAYS[2])}

    reopened = BarStore(tmp_path / "bars.sqlite", head_minutes=5)
    assert reopened.head_minutes == 30  # the stored setting wins


def test_minutes_head_rest_and_empty_markers(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars.sqlite", head_minutes=30)
    day = session(D)
    morning = [bar(D, 9, 30 + i, 10, 11, 9, 10, 100, vwap=10.0) for i in range(3)]
    afternoon = [bar(D, 14, 0, 12, 13, 11, 12, 200)]
    store.put_minutes(day, {"AAA": rows(day, morning + afternoon), "NONE": []})

    assert store.minute_symbols(D) == {"AAA", "NONE"}
    head, stored = store.minutes(day, ["AAA", "NONE", "ZZZ"], until_minutes=2)
    assert head == {"AAA": morning[:2]} and stored == {"AAA", "NONE"}
    full, _ = store.minutes(day, ["AAA"])
    assert full["AAA"] == morning + afternoon
    long_range, _ = store.minutes(day, ["AAA"], until_minutes=300)  # beyond the head
    assert long_range["AAA"] == morning + afternoon
    assert store.stats()["minute_symbol_days"] == 2


class Fallback:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.requests = 7

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        self.calls.append(("calendar", []))
        return [session(start)]

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]:
        self.calls.append(("daily", list(symbols)))
        return {s: [] for s in symbols}

    def opening_bars(
        self, symbols: Sequence[str], day: TradingDay, minutes: int
    ) -> dict[str, list[Bar]]:
        self.calls.append(("opening", list(symbols)))
        return {s: [bar(day.day, 9, 30, 1, 1, 1, 1)] for s in symbols}

    def session_bars(self, symbols: Sequence[str], day: TradingDay) -> dict[str, list[Bar]]:
        self.calls.append(("session", list(symbols)))
        return {}


def test_store_source_falls_back_for_missing_data(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars.sqlite")
    day = session(D)
    store.put_calendar([day], D, D)
    store.put_daily("AAA", [DailyBar(D, 1, 1, 1, 1, 1)], D, D)
    store.put_minutes(day, {"AAA": rows(day, [bar(D, 9, 30, 2, 2, 2, 2)])})
    fallback = Fallback()
    src = StoreSource(store, fallback)

    assert src.trading_days(D, D) == [day] and fallback.calls == []
    src.trading_days(D - timedelta(days=30), D)  # outside the stored range
    assert fallback.calls[-1] == ("calendar", [])

    assert set(src.daily_bars(["AAA", "BBB"], D, D)) == {"AAA", "BBB"}
    assert fallback.calls[-1] == ("daily", ["BBB"])

    bars = src.opening_bars(["AAA", "BBB"], day, 5)
    assert bars["AAA"][0].open == 2 and bars["BBB"][0].open == 1
    assert fallback.calls[-1] == ("opening", ["BBB"])
    assert src.misses == {"calendar": 1, "daily_symbols": 1, "minute_symbol_days": 1}
    assert src.requests == 7

    offline = StoreSource(store)
    assert offline.opening_bars(["BBB"], day, 5) == {}
    assert offline.misses["minute_symbol_days"] == 1


UNIVERSE = ["AAA", "BBB", "CCC"]


def build_store(path: Path) -> BarStore:
    """A store holding everything ``test_engine.build_source()`` has."""
    src = build_source()
    store = BarStore(path)
    days = [session(d) for d in DAYS]
    store.put_calendar(days, DAYS[0] - timedelta(days=60), DAYS[-1])
    for sym, bars in src.daily.items():
        store.put_daily(sym, bars, DAYS[0] - timedelta(days=60), DAYS[-1])
    for day in days:
        per_symbol: dict[str, list[list[object]]] = {}
        for sym in UNIVERSE:
            merged = {b.timestamp: b for b in src.opening.get((day.day, sym), [])}
            merged.update({b.timestamp: b for b in src.session.get((day.day, sym), [])})
            per_symbol[sym] = rows(day, [merged[t] for t in sorted(merged)])
        store.put_minutes(day, per_symbol)
    store.set_meta("universe", {"symbols": UNIVERSE, "as_of": DAYS[-1].isoformat()})
    return store


def test_backtest_from_store_matches_the_source(tmp_path: Path) -> None:
    src = build_source()
    universe = UNIVERSE
    store = build_store(tmp_path / "bars.sqlite")

    expected = run_backtest(src, universe, OrbParams(), CostModel(), config())
    actual = run_backtest(StoreSource(store), universe, OrbParams(), CostModel(), config())
    assert expected.trades  # the comparison is not vacuous
    assert [(t.symbol, t.entry_price, t.exit_price, t.qty) for t in actual.trades] == [
        (t.symbol, t.entry_price, t.exit_price, t.qty) for t in expected.trades
    ]
    assert actual.final_equity == pytest.approx(expected.final_equity)
    assert TEST_DAY in {d.day for d in actual.days}

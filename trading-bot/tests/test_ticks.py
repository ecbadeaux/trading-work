"""Tick data: parsing, the tick store, the fill rules and tick fills in the simulator."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import bar, et
from tick_helpers import FakeTicks, quote, trade, us

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.simulator import Trade, simulate_trade
from trading_bot.backtest.ticks import elect, market_fill, quote_at
from trading_bot.data.models import Bar
from trading_bot.data.ticks import (
    MinuteTicks,
    StoreTicks,
    TickStore,
    Window,
    epoch_us,
    minute_windows,
    moment_window,
    parse_ts_us,
    quote_rows,
    trade_rows,
)
from trading_bot.strategies.orb import OpeningRange, OrbCandidate, OrbParams

D = date(2024, 3, 5)

# -- data.ticks ---------------------------------------------------------------------------------


def test_parse_timestamps_keeps_microseconds() -> None:
    base = epoch_us(datetime(2021, 5, 21, 13, 38, tzinfo=UTC))
    assert parse_ts_us("2021-05-21T13:38:00.217451776Z") == base + 217451
    assert parse_ts_us("2021-05-21T13:38:00Z") == base
    assert parse_ts_us("2021-05-21T13:38:00.5Z") == base + 500000
    assert parse_ts_us("2021-05-21T09:38:00-04:00") == base


def test_trade_and_quote_rows() -> None:
    raw = [
        {"t": "2024-03-05T14:35:02Z", "p": 10.5, "s": 50, "c": ["@", "I"]},
        {"t": "2024-03-05T14:35:01Z", "p": 10.4, "s": 100, "c": [" "]},
        {"t": "2024-03-05T14:35:03Z", "p": 99.0, "s": 100, "c": [" "], "u": "canceled"},
        {"t": "2024-03-05T14:35:04Z", "p": 10.6, "s": 100, "c": [" ", "T"], "u": "corrected"},
    ]
    rows = trade_rows(raw)
    assert [r[1:] for r in rows] == [[10.4, 100, ""], [10.5, 50, "I"], [10.6, 100, "T"]]
    quotes = quote_rows([
        {"t": "2024-03-05T14:35:02Z", "bp": 10.4, "ap": 10.5, "bs": 1, "as": 2},
        {"t": "2024-03-05T14:35:01Z", "bp": 10.3, "ap": 10.4, "bs": 3, "as": 4},
    ])
    assert [q[1:] for q in quotes] == [[10.3, 10.4, 3, 4], [10.4, 10.5, 1, 2]]


def test_windows_and_store_round_trip(tmp_path: Path) -> None:
    minute = et(D, 9, 36)
    tw, qw = minute_windows("AAA", minute)
    assert (tw.start, tw.end) == (minute, minute + timedelta(minutes=1))
    assert (qw.start, qw.end) == (minute - timedelta(seconds=10), tw.end)
    mw = moment_window("AAA", et(D, 15, 50))
    assert mw.end - mw.start == timedelta(seconds=15)
    assert tw.key() == ("AAA", "trades", "2024-03-05T14:36:00Z", "2024-03-05T14:37:00Z")

    store = TickStore(tmp_path / "ticks.sqlite")
    t0 = epoch_us(minute)
    store.put(tw, [[t0 + 5, 10.5, 100, ""], [t0 + 9, 10.6, 50, "I"]])
    store.put(qw, [])
    assert store.trades(tw) == [trade(t0 + 5, 10.5), trade(t0 + 9, 10.6, "I", 50)]
    assert store.quotes(qw) == [] and store.quotes(mw) is None
    assert store.stored() == {tw.key(), qw.key()}
    assert store.stats()["windows"] == 2 and store.stats()["empty"] == 1

    ticks = StoreTicks(store)
    got = ticks.minute("AAA", minute)
    assert got is not None and len(got.trades) == 2 and got.quotes == []
    assert ticks.minute("AAA", et(D, 9, 37)) is None and ticks.moment("AAA", et(D, 15, 50)) is None
    assert ticks.misses == {"minute": 1, "moment": 1}
    store.close()


# -- backtest.ticks -----------------------------------------------------------------------------


def test_quote_at_skips_invalid_quotes() -> None:
    quotes = [quote(10, 9.9, 10.1), quote(20, 0, 10.2), quote(30, 10.3, 10.2), quote(40, 10, 10.2)]
    assert quote_at(quotes, 5) is None
    assert quote_at(quotes, 35) == quotes[0]  # zero bid and crossed quotes are skipped
    assert quote_at(quotes, 40) == quotes[3]
    assert market_fill(quotes, 30, buy=True, latency_us=10) == (10.2, pytest.approx(0.2))
    assert market_fill(quotes, 30, buy=False, latency_us=10) == (10.0, pytest.approx(0.2))
    assert market_fill(quotes, 0, buy=True, latency_us=5) is None


def test_elect_needs_an_eligible_trade_inside_the_quote() -> None:
    quotes = [quote(0, 100.0, 100.1), quote(50, 101.0, 101.1)]
    trades = [
        trade(10, 101.2, "T"),  # extended hours: never elects
        trade(20, 101.2),  # outside the quote in force (ask 100.1)
        trade(30, 100.05),  # below the level
        trade(60, 101.05, "I"),  # an odd lot inside the quote elects
        trade(70, 101.1),
    ]
    assert elect(trades, quotes, 101.0, rising=True, after=0) == 60
    assert elect(trades, quotes, 101.0, rising=True, after=65) == 70
    assert elect(trades, quotes, 100.0, rising=False, after=0) is None
    assert elect([trade(5, 101.5)], [], 101.0, rising=True, after=0) == 5  # no quote yet


# -- tick fills in the simulator ----------------------------------------------------------------

LONG = OrbCandidate("AAA", "long", 5.0, OpeningRange(100, 101, 99.5, 100.8, 5000), atr=2.0)
OR_BARS = [bar(D, 9, 30 + i, 100, 100.2, 99.9, 100, 1000) for i in range(5)]
CLOSE = bar(D, 15, 50, 103, 103.1, 102.9, 103.05)


def entry_tape(ticks: FakeTicks, *, dip_after_entry: bool = False) -> None:
    """09:35: quotes move up, a trade elects the buy stop at 101 at 09:35:12."""
    quotes = [quote(us(D, 9, 34, 58), 100.79, 100.81), quote(us(D, 9, 35, 10), 101.0, 101.04)]
    trades = [
        trade(us(D, 9, 35, 2), 100.75),  # a dip before the entry: below the stop, harmless
        trade(us(D, 9, 35, 3), 101.5, "T"),  # not an electing print
        trade(us(D, 9, 35, 12), 101.02),
    ]
    if dip_after_entry:
        quotes.append(quote(us(D, 9, 35, 29), 100.76, 100.79))
        trades.append(trade(us(D, 9, 35, 30), 100.78))
    ticks.minutes[("AAA", et(D, 9, 35))] = MinuteTicks(trades, quotes)
    ticks.moments[("AAA", et(D, 15, 50))] = [quote(us(D, 15, 49, 59), 102.97, 102.99)]


def run(bars: list[Bar], ticks: FakeTicks | None, costs: CostModel | None = None,
        **kw: Any) -> Trade:
    t = simulate_trade(
        D, LONG, OR_BARS + bars, or_end=et(D, 9, 35), exit_at=et(D, 15, 50), qty=100,
        params=OrbParams(), costs=costs or CostModel.zero(), ticks=ticks, **kw,
    )
    assert t is not None
    return t


AMBIGUOUS = bar(D, 9, 35, 100.8, 101.3, 100.7, 101.2)  # reaches both the trigger and the stop


def test_entry_and_exit_fill_from_the_tape() -> None:
    ticks = FakeTicks()
    entry_tape(ticks)
    t = run([bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE], ticks)
    assert (t.entry_price, t.stop_price, t.exit_price) == (101, pytest.approx(100.8), 103)
    assert t.entry_fill == 101.04 and t.exit_fill == 102.97  # the ask, then the bid
    assert t.fill_source == "ticks" and t.entry_spread == pytest.approx(0.04)
    assert t.tick_notes == ()
    assert t.cost_r == pytest.approx((0.04 + 0.03) / 0.2)

    slipped = run([bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE], ticks,
                  CostModel(spread_bps=50, slippage_bps=10))
    assert slipped.entry_fill == pytest.approx(101.04 * 1.001)  # only slippage is added
    assert slipped.exit_fill == pytest.approx(102.97 * 0.999)


def test_the_tape_settles_the_entry_minute() -> None:
    bars = [AMBIGUOUS, CLOSE]
    # Bars alone: the worst case stops the trade in the entry minute.
    assert run(bars, None).exit_reason == "stop"
    ticks = FakeTicks()
    entry_tape(ticks)  # the dip to 100.75 came before the entry
    held = run(bars, ticks)
    assert held.exit_reason == "close" and held.fill_source == "ticks"

    entry_tape(ticks, dip_after_entry=True)
    stopped = run(bars, ticks)
    assert stopped.exit_reason == "stop" and stopped.exit_time == et(D, 9, 35)
    assert stopped.exit_price == pytest.approx(100.8) and stopped.exit_fill == 100.76


def test_later_stops_use_the_tape_when_it_confirms_them() -> None:
    ticks = FakeTicks()
    entry_tape(ticks)
    later = bar(D, 9, 40, 100.9, 100.95, 100.5, 100.6)
    bars = [bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), later, CLOSE]
    ticks.minutes[("AAA", et(D, 9, 40))] = MinuteTicks(
        [trade(us(D, 9, 40, 1), 100.85), trade(us(D, 9, 40, 7), 100.79)],
        [quote(us(D, 9, 39, 55), 100.84, 100.86), quote(us(D, 9, 40, 6), 100.7, 100.8)],
    )
    t = run(bars, ticks)
    assert t.exit_reason == "stop" and t.exit_fill == 100.7 and t.fill_source == "ticks"

    # The bar's low came from prints that don't elect: the stop still counts (conservative),
    # at the bar model's price.
    ticks.minutes[("AAA", et(D, 9, 40))] = MinuteTicks(
        [trade(us(D, 9, 40, 7), 100.5, "Z")], [quote(us(D, 9, 39, 55), 100.84, 100.86)]
    )
    t = run(bars, ticks)
    assert t.exit_reason == "stop" and t.tick_notes == ("stop_not_elected",)
    assert t.fill_source == "mixed" and t.exit_fill == pytest.approx(100.8)


def test_missing_ticks_fall_back_to_the_bar_model() -> None:
    bars = [AMBIGUOUS, CLOSE]
    costs = CostModel()
    t = run(bars, FakeTicks(), costs)
    plain = run(bars, None, costs)
    assert t.fill_source == "bars" and t.tick_notes == ("missing_entry",)
    assert (t.exit_reason, t.entry_fill, t.exit_fill) == (
        plain.exit_reason, plain.entry_fill, plain.exit_fill
    )

    ticks = FakeTicks()
    entry_tape(ticks)
    del ticks.moments[("AAA", et(D, 15, 50))]
    t = run([bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE], ticks)
    assert t.fill_source == "mixed" and t.tick_notes == ("missing_exit",)

    ticks.minutes[("AAA", et(D, 9, 35))] = MinuteTicks([trade(us(D, 9, 35, 2), 100.9)], [])
    t = run([bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE], ticks)
    assert "entry_not_elected" in t.tick_notes and t.entry_fill == 101


def test_market_entries_fill_at_the_quote() -> None:
    ticks = FakeTicks()
    entry_tape(ticks)
    t = run([bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE], ticks)
    m = simulate_trade(
        D, LONG, [*OR_BARS, bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE],
        or_end=et(D, 9, 35), exit_at=et(D, 15, 50), qty=100,
        params=OrbParams(entry_mode="market"), costs=CostModel.zero(), ticks=ticks,
    )
    assert m is not None and m.entry_price == 100.8 and m.entry_fill == 100.81
    assert t.entry_fill == 101.04


def test_windows_cover_both_paths() -> None:
    from trading_bot.backtest.tickplan import windows_for

    entry, exit_ = et(D, 9, 35), et(D, 10, 2)
    stop = windows_for("AAA", entry, exit_, "stop")
    assert [w.kind for w in stop] == ["trades", "quotes", "trades", "quotes"]
    assert stop[2].start == exit_
    close = windows_for("AAA", entry, et(D, 15, 50), "close")
    assert close[-1] == moment_window("AAA", et(D, 15, 50)) and len(close) == 3
    assert len(windows_for("AAA", entry, exit_, "target")) == 2


def test_request_groups_batch_same_time_windows() -> None:
    from trading_bot.backtest.tickplan import request_groups

    at = et(D, 15, 50)
    windows = [moment_window(s, at) for s in ("AAA", "BBB", "CCC", "OLD@20240101")]
    windows.append(minute_windows("AAA", et(D, 9, 35))[0])

    def lookup(key: str) -> tuple[str, str | None]:
        return (key.split("@")[0], "-") if "@" in key else (key, None)

    groups = request_groups(windows, lookup, chunk=2)
    shapes = [(g[0], g[1].astimezone(UTC).time().isoformat(), g[3], sorted(g[4])) for g in groups]
    assert shapes == [
        ("trades", "14:35:00", None, ["AAA"]),
        ("quotes", "20:49:50", None, ["AAA", "BBB"]),
        ("quotes", "20:49:50", None, ["CCC"]),
        ("quotes", "20:49:50", "-", ["OLD"]),
    ]
    assert groups[-1][4] == {"OLD": "OLD@20240101"}


class FakeTickClient:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        return self.pages[path].pop(0)


def test_fetch_stores_every_window_once(tmp_path: Path) -> None:
    from trading_bot.backtest import tickplan
    from trading_bot.data.download import BarFetcher

    minute = et(D, 9, 35)
    tw, qw = minute_windows("AAA", minute)
    other = minute_windows("BBB", minute)[0]
    client = FakeTickClient({
        "/stocks/trades": [
            {"trades": {"AAA": [{"t": "2024-03-05T14:35:01.5Z", "p": 101, "s": 1, "c": [" "]}]},
             "next_page_token": "x"},
            {"trades": {"AAA": [{"t": "2024-03-05T14:36:00Z", "p": 102, "s": 1, "c": [" "]}]},
             "next_page_token": None},
        ],
        "/stocks/quotes": [
            {"quotes": {"AAA": [{"t": "2024-03-05T14:34:55Z", "bp": 100, "ap": 101, "bs": 1,
                                 "as": 1}]}, "next_page_token": None},
        ],
    })
    fetcher = BarFetcher(
        client, pages_per_minute=6000, sleep=lambda s: None  # type: ignore[arg-type]
    )
    store = TickStore(tmp_path / "ticks.sqlite")
    todo = tickplan.missing(store, [tw, qw, other])
    assert tickplan.fetch(fetcher, store, todo, lambda k: (k, None)) == 2
    paths = [(path, params["symbols"]) for path, params in client.calls]
    assert paths == [  # the quote window starts 10 s earlier, so it goes first
        ("/stocks/quotes", "AAA"), ("/stocks/trades", "AAA,BBB"), ("/stocks/trades", "AAA,BBB"),
    ]
    assert "asof" not in client.calls[1][1] and client.calls[2][1]["page_token"] == "x"
    stored = store.trades(tw)
    assert stored is not None and [t.price for t in stored] == [101]  # 14:36:00 is the next minute
    assert store.trades(other) == [] and len(store.quotes(qw) or []) == 1
    assert tickplan.missing(store, [tw, qw, other]) == []
    text = tickplan.describe([tw, qw, other], [], 0, 180)
    assert "3 needed, 3 stored, 0 to fetch" in text
    with pytest.raises(ValueError):
        fetcher.ticks("bars", ["AAA"], minute, minute)


def test_window_order_is_stable() -> None:
    a = Window("AAA", "trades", et(D, 9, 35), et(D, 9, 36))
    b = Window("AAA", "quotes", et(D, 9, 35), et(D, 9, 36))
    assert sorted([a, b]) == [b, a]

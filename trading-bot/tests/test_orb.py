from __future__ import annotations

import random
from datetime import date

import pytest
from helpers import bar

from trading_bot.data.models import DailyBar
from trading_bot.strategies.orb import (
    DailyStats,
    OpeningRange,
    OrbCandidate,
    OrbParams,
    average_true_range,
    daily_stats,
    gap,
    opening_range,
    position_size,
    relative_volume,
    select_candidates,
    stop_distance,
)

D = date(2024, 3, 5)
GOOD_STATS = DailyStats(atr=2.0, avg_volume=2_000_000)


def daily(
    day: int, o: float, h: float, low: float, c: float, v: float = 1e6, adj: float | None = None
) -> DailyBar:
    return DailyBar(date(2024, 1, day), o, h, low, c, v, adj)


def test_opening_range_aggregates_and_direction() -> None:
    bars = [
        bar(D, 9, 30, 100, 101, 99.5, 100.5, 10),
        bar(D, 9, 31, 100.5, 102, 100, 101.5, 20),
    ]
    rng = opening_range(bars)
    assert rng == OpeningRange(open=100, high=102, low=99.5, close=101.5, volume=30)
    assert rng.direction == "long"
    assert OpeningRange(100, 101, 99, 99.5, 1).direction == "short"
    assert OpeningRange(100, 101, 99, 100, 1).direction is None
    assert opening_range([]) is None


def test_average_true_range_uses_previous_close() -> None:
    bars = [daily(2, 10, 11, 9, 10), daily(3, 12, 13, 11.5, 12), daily(4, 12, 12.5, 11, 11.5)]
    # TR day 3 = max(1.5, |13-10|, |11.5-10|) = 3 ; TR day 4 = max(1.5, 0.5, 1.0) = 1.5
    assert average_true_range(bars) == pytest.approx(2.25)
    assert average_true_range(bars[:1]) is None


def test_daily_stats_requires_full_history_and_rejects_splits() -> None:
    history = [daily(d, 10, 11, 9, 10, v=1000) for d in range(2, 6)]  # 4 bars -> 3-day window
    stats = daily_stats(history, lookback_days=3)
    assert stats == DailyStats(atr=2.0, avg_volume=1000, prev_close=10)
    assert daily_stats(history[:3], lookback_days=3) is None

    split = [daily(2, 20, 22, 18, 20, adj=10)]
    split += [daily(d, 10, 11, 9, 10, adj=10) for d in (3, 4, 5)]
    assert daily_stats(split, lookback_days=3) is None


def test_relative_volume() -> None:
    assert relative_volume(300, [100, 100, 100]) == 3
    assert relative_volume(300, []) is None
    assert relative_volume(300, [0, 0]) is None


def _inputs(**entries: tuple[OpeningRange, DailyStats, float]):
    return entries


def test_select_candidates_filters_ranks_and_caps() -> None:
    up = OpeningRange(50, 51, 49.5, 50.8, 1)
    down = OpeningRange(50, 50.5, 49, 49.2, 1)
    doji = OpeningRange(50, 51, 49, 50, 1)
    inputs = _inputs(
        AAA=(up, GOOD_STATS, 3.0),
        BBB=(down, GOOD_STATS, 5.0),
        CCC=(up, GOOD_STATS, 0.9),  # below min relvol
        DDD=(doji, GOOD_STATS, 9.0),  # doji
        EEE=(OpeningRange(4, 4.2, 3.9, 4.1, 1), GOOD_STATS, 8.0),  # price too low
        FFF=(up, DailyStats(atr=0.4, avg_volume=2e6), 8.0),  # ATR too low
        GGG=(up, DailyStats(atr=2.0, avg_volume=5e5), 8.0),  # avg volume too low
        HHH=(up, GOOD_STATS, 3.0),  # ties with AAA, sorts after it
    )
    picked = select_candidates(inputs, OrbParams(top_n=2))
    assert [(c.symbol, c.direction) for c in picked] == [("BBB", "short"), ("AAA", "long")]
    assert picked[0].trigger == 49  # short triggers at the range low
    assert picked[1].trigger == 51


def test_select_candidates_placebo_modes() -> None:
    up = OpeningRange(50, 51, 49.5, 50.8, 1)
    inputs = {f"S{i:02d}": (up, GOOD_STATS, 2.0 + i) for i in range(20)}

    inverse = select_candidates(inputs, OrbParams(direction_mode="inverse"))
    assert {c.direction for c in inverse} == {"short"}

    params = OrbParams(direction_mode="random")
    first = select_candidates(inputs, params, random.Random(7))
    again = select_candidates(inputs, params, random.Random(7))
    assert [c.direction for c in first] == [c.direction for c in again]
    assert {c.direction for c in first} == {"long", "short"}


def test_stop_distance_and_position_size() -> None:
    c = OrbCandidate("AAA", "long", 2.0, OpeningRange(50, 51, 49.5, 50.8, 1), atr=2.0)
    assert stop_distance(c, OrbParams()) == pytest.approx(0.2)
    assert stop_distance(c, OrbParams(stop_mode="range")) == pytest.approx(1.5)

    # Risk-bound: 1% of 25k / 0.5 = 500 shares; leverage cap = 25k*4/20/10 = 500 -> 500.
    assert position_size(25_000, 10, 0.5, 0.01, 4, 20) == 500
    # Leverage-bound: risk says 1250, cap says 25k*4/20/50.6 = 98.8 -> 98.
    assert position_size(25_000, 50.6, 0.2, 0.01, 4, 20) == 98
    assert position_size(25_000, 50, 0, 0.01, 4, 20) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"or_minutes": 0},
        {"top_n": 0},
        {"stop_atr_fraction": 0},
        {"target_r": -1},
        {"exit_minutes_before_close": -1},
        {"exit_minutes_before_close": 10, "last_entry_minutes_before_close": 5},
        {"breakeven_r": 0},
        {"min_gap": -0.01},
        {"min_range_atr": 0.5, "max_range_atr": 0.5},
        {"entry_window_minutes": 0},
    ],
)
def test_params_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OrbParams(**kwargs)  # type: ignore[arg-type]


def test_gap_and_setup_filters() -> None:
    stats = DailyStats(atr=2.0, avg_volume=2e6, prev_close=48.0)  # open 50 -> +4.2% gap
    up = OpeningRange(50, 51, 49.5, 50.8, 1)  # width 1.5 = 3% of the open, 0.75 ATR
    down = OpeningRange(50, 50.5, 49, 49.2, 1)
    assert gap(stats, 50) == pytest.approx(50 / 48 - 1)
    assert gap(DailyStats(atr=2.0, avg_volume=2e6), 50) is None

    def picked(rng_bar: OpeningRange, s: DailyStats = stats, **kw: object) -> bool:
        params = OrbParams(**kw)  # type: ignore[arg-type]
        return bool(select_candidates({"AAA": (rng_bar, s, 3.0)}, params))

    assert picked(up, min_gap=0.04) and not picked(up, min_gap=0.05)
    assert not picked(up, DailyStats(atr=2.0, avg_volume=2e6), min_gap=0.01)  # no prior close
    assert picked(up, gap_aligned=True) and not picked(down, gap_aligned=True)
    assert picked(up, min_atr_pct=0.04) and not picked(up, min_atr_pct=0.05)
    assert picked(up, min_range_pct=0.03) and not picked(up, min_range_pct=0.031)
    assert picked(up, min_range_atr=0.75) and not picked(up, min_range_atr=0.8)
    assert picked(up, max_range_atr=0.75) and not picked(up, max_range_atr=0.7)

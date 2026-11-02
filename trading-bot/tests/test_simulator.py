from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from helpers import bar, et

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.simulator import Trade, running_vwap, simulate_trade
from trading_bot.data.models import Bar
from trading_bot.strategies.orb import OpeningRange, OrbCandidate, OrbParams

D = date(2024, 3, 5)
LONG = OrbCandidate("AAA", "long", 5.0, OpeningRange(100, 101, 99.5, 100.8, 5000), atr=2.0)
SHORT = OrbCandidate("AAA", "short", 5.0, OpeningRange(100, 100.4, 99.5, 99.6, 5000), atr=2.0)
OR_BARS = [bar(D, 9, 30 + i, 100, 100.2, 99.9, 100, 1000, vwap=100) for i in range(5)]
CLOSE_BAR = bar(D, 15, 50, 103, 103.1, 102.9, 103.05)


def run(
    candidate: OrbCandidate, bars: list[Bar], costs: CostModel | None = None, **overrides: Any
) -> Trade | None:
    params = OrbParams(**{k: overrides.pop(k) for k in list(overrides) if hasattr(OrbParams, k)})
    return simulate_trade(
        D,
        candidate,
        OR_BARS + bars,
        or_end=et(D, 9, 35),
        exit_at=et(D, 15, 50),
        qty=overrides.pop("qty", 100),
        params=params,
        costs=costs or CostModel.zero(),
        **overrides,
    )


def test_stop_entry_held_to_flatten_time() -> None:
    t = run(
        LONG,
        [
            bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2),
            bar(D, 9, 36, 101.2, 102, 101.1, 101.9),
            bar(D, 15, 49, 102.9, 103, 102.8, 102.95),
            CLOSE_BAR,
        ],
    )
    assert t is not None
    assert (t.entry_price, t.stop_price, t.exit_price, t.exit_reason) == (
        101,
        pytest.approx(100.8),
        103,
        "close",
    )
    assert t.entry_time == et(D, 9, 35) and t.exit_time == et(D, 15, 50)
    assert t.gross_r == pytest.approx(10) and t.net_r == pytest.approx(10)


def test_entry_gap_fills_at_open() -> None:
    t = run(LONG, [bar(D, 9, 35, 101.5, 101.8, 101.4, 101.7), CLOSE_BAR])
    assert t is not None and t.entry_price == 101.5
    assert t.stop_price == pytest.approx(101.3)


def test_stop_hit_and_gap_through_stop() -> None:
    entry = bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2)
    hit = run(LONG, [entry, bar(D, 9, 36, 101.2, 101.3, 100.7, 100.9), CLOSE_BAR])
    assert hit is not None and hit.exit_reason == "stop"
    assert hit.exit_price == pytest.approx(100.8) and hit.gross_r == pytest.approx(-1)

    gapped = run(LONG, [entry, bar(D, 9, 36, 100.5, 100.6, 100.4, 100.5), CLOSE_BAR])
    assert gapped is not None and gapped.exit_price == 100.5
    assert gapped.gross_r == pytest.approx(-2.5)


def test_worst_case_intrabar_stops_on_entry_bar() -> None:
    bars = [bar(D, 9, 35, 100.8, 101.3, 100.7, 101.0), CLOSE_BAR]
    worst = run(LONG, bars)
    assert worst is not None and worst.exit_reason == "stop"
    assert worst.exit_time == et(D, 9, 35) and worst.gross_r == pytest.approx(-1)

    best = run(LONG, bars, worst_case_intrabar=False)
    assert best is not None and best.exit_reason == "close" and best.exit_price == 103


def test_no_trigger_and_entry_cutoff() -> None:
    flat = [bar(D, 9, 35, 100.5, 100.9, 100.4, 100.6), bar(D, 15, 50, 100.5, 100.9, 100.4, 100.6)]
    assert run(LONG, flat) is None

    late = [bar(D, 9, 35, 100.5, 100.9, 100.4, 100.6), bar(D, 11, 0, 100.9, 101.5, 100.9, 101.4)]
    assert run(LONG, late, last_entry_at=et(D, 10, 30)) is None
    assert run(LONG, late) is not None


def test_profit_target_fills_at_limit_without_costs() -> None:
    bars = [
        bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2),
        bar(D, 9, 36, 101.2, 101.5, 101.1, 101.45),
        CLOSE_BAR,
    ]
    t = run(LONG, bars, CostModel(), target_r=2.0)
    assert t is not None and t.exit_reason == "target"
    assert t.exit_price == pytest.approx(101.4) and t.exit_fill == pytest.approx(101.4)
    assert t.gross_r == pytest.approx(2)


def test_short_trade_mirrors_long() -> None:
    bars = [bar(D, 9, 35, 99.55, 99.6, 99.3, 99.4), bar(D, 15, 50, 98.5, 98.6, 98.4, 98.5)]
    t = run(SHORT, bars)
    assert t is not None and t.direction == "short"
    assert t.entry_price == 99.5 and t.stop_price == pytest.approx(99.7)
    assert t.gross_r == pytest.approx(5)


def test_vwap_exit_on_next_bar_open() -> None:
    bars = [
        bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2, vwap=101.0),
        bar(D, 9, 36, 101.2, 101.3, 100.05, 100.1, vwap=100.5),  # closes below session VWAP
        bar(D, 9, 37, 100.15, 100.3, 100.1, 100.2),
        CLOSE_BAR,
    ]
    t = run(LONG, bars, vwap_exit=True, stop_atr_fraction=0.5)
    assert t is not None and t.exit_reason == "vwap"
    assert t.exit_price == 100.15 and t.exit_time == et(D, 9, 37)
    assert t.gross_r == pytest.approx(-0.85)


def test_market_entry_and_data_ending_early() -> None:
    bars = [bar(D, 9, 35, 100.8, 101.0, 100.7, 100.9), bar(D, 12, 0, 101, 101.2, 101, 101.1)]
    t = run(LONG, bars, entry_mode="market")
    assert t is not None
    assert t.entry_price == 100.8 and t.stop_price == pytest.approx(100.6)
    assert t.exit_reason == "close" and t.exit_price == 101.1 and t.exit_time == et(D, 12, 0)


def test_costs_reduce_net_result() -> None:
    bars = [bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2), CLOSE_BAR]
    t = run(LONG, bars, CostModel())
    assert t is not None
    # Entry is a stop fill: half of 4 bps spread + (1 + 2) bps slippage on 101.
    assert t.entry_fill == pytest.approx(101 + 0.0202 + 0.0303)
    # Exit is a market fill: half of 4 bps spread + 1 bp slippage on 103.
    assert t.exit_fill == pytest.approx(103 - 0.0206 - 0.0103)
    fees = 0.0000278 * 100 * t.exit_fill + 0.000166 * 100
    assert t.fees == pytest.approx(fees)
    assert t.net_pnl == pytest.approx((t.exit_fill - t.entry_fill) * 100 - fees)
    assert t.cost_r == pytest.approx(t.gross_r - t.net_r) and t.cost_r > 0


def test_zero_qty_returns_none() -> None:
    assert run(LONG, [bar(D, 9, 35, 100.8, 101.3, 100.85, 101.2)], qty=0) is None


def test_running_vwap_skips_zero_volume() -> None:
    bars = [
        bar(D, 9, 30, 10, 10, 10, 10, v=0),
        bar(D, 9, 31, 10, 12, 8, 10, v=100),  # typical price 10
        bar(D, 9, 32, 11, 11, 11, 11, v=100, vwap=13),
    ]
    assert running_vwap(bars) == [None, 10, 11.5]


def test_close_confirmed_entry_waits_for_a_close_beyond_the_range() -> None:
    bars = [
        bar(D, 9, 35, 100.9, 101.3, 100.8, 100.95),  # pokes above 101, closes back inside
        bar(D, 9, 36, 100.95, 101.4, 100.9, 101.3),  # closes above the range
        bar(D, 9, 37, 101.35, 101.6, 101.3, 101.5),  # entry at this open
        CLOSE_BAR,
    ]
    t = run(LONG, bars, entry_mode="close")
    assert t is not None
    assert t.entry_time == et(D, 9, 37) and t.entry_price == 101.35
    assert t.stop_price == pytest.approx(101.35 - 0.2)
    assert run(LONG, bars[:2], entry_mode="close") is None  # no bar left to enter on


def test_breakeven_moves_stop_to_entry_from_the_next_bar() -> None:
    bars = [
        bar(D, 9, 35, 100.9, 101.5, 100.85, 101.4),  # entry at 101; reaches +2R (101.4)
        bar(D, 9, 36, 101.4, 101.45, 100.9, 101.0),  # back through 101 -> breakeven exit
        CLOSE_BAR,
    ]
    t = run(LONG, bars, breakeven_r=2.0)
    assert t is not None and t.exit_reason == "breakeven" and t.exit_price == 101
    assert t.gross_pnl == 0
    # Without breakeven the original stop (100.8) is never touched; held to the close.
    plain = run(LONG, bars)
    assert plain is not None and plain.exit_reason == "close"
    # Not reached: +3R needs 101.6.
    unreached = run(LONG, bars, breakeven_r=3.0)
    assert unreached is not None and unreached.exit_reason == "close"

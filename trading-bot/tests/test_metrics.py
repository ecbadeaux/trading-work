from __future__ import annotations

from datetime import date

import pytest
from helpers import et

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import BacktestConfig, BacktestResult, DayResult
from trading_bot.backtest.metrics import (
    combine_equal_weight,
    curve_stats,
    max_drawdown,
    summarize,
)
from trading_bot.backtest.simulator import Trade
from trading_bot.strategies.orb import Direction, OrbParams

D1, D2, D3 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)


def trade(day: date, direction: Direction, entry: float, exit_: float, risk: float) -> Trade:
    return Trade(
        day=day,
        symbol="AAA",
        direction=direction,
        relvol=2.0,
        qty=10,
        risk_per_share=risk,
        entry_time=et(day, 9, 40),
        entry_price=entry,
        entry_fill=entry,
        stop_price=entry - risk if direction == "long" else entry + risk,
        exit_time=et(day, 15, 50),
        exit_price=exit_,
        exit_fill=exit_,
        exit_reason="close",
        fees=0.0,
    )


def result_with(days: list[DayResult], trades: list[Trade]) -> BacktestResult:
    return BacktestResult(
        params=OrbParams(),
        costs=CostModel.zero(),
        config=BacktestConfig(start=days[0].day, end=days[-1].day, initial_equity=1000),
        trades=trades,
        days=days,
    )


def test_max_drawdown() -> None:
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert max_drawdown([100, 110, 120]) == 0


def test_summarize_trade_and_day_stats() -> None:
    trades = [trade(D1, "long", 100, 102, 1.0), trade(D2, "short", 50, 51, 0.5)]
    days = [
        DayResult(D1, 1000, 20, 20, 1),
        DayResult(D2, 1020, -10, -10, 1),
        DayResult(D3, 1010, 0, 0, 0),
    ]
    s = summarize(result_with(days, trades))
    assert s.trades == 2 and s.long_trades == 1 and s.short_trades == 1
    assert s.total_return == pytest.approx(0.01)
    assert s.win_rate == 0.5 and s.hit_ratio_days == 0.5
    assert s.avg_gross_r == pytest.approx(0) and s.avg_cost_r == pytest.approx(0)
    assert s.profit_factor == pytest.approx(2)
    assert (s.best_r, s.worst_r) == (pytest.approx(2), pytest.approx(-2))
    assert s.max_drawdown == pytest.approx(1 - 1010 / 1020)
    assert s.sharpe is not None and s.benchmark_total_return is None


def test_benchmark_alpha_beta() -> None:
    bench = {date(2024, 3, 1): 100.0}
    bench[D1] = 101.0  # +1%
    bench[D2] = bench[D1] * 0.995  # -0.5%
    bench[D3] = bench[D2] * 1.002  # +0.2%
    days = []
    equity = 1000.0
    prev = bench[date(2024, 3, 1)]
    for d in (D1, D2, D3):
        r = 2 * (bench[d] / prev - 1)  # strategy = exactly 2x the benchmark
        days.append(DayResult(d, equity, r * equity, r * equity, 1))
        equity *= 1 + r
        prev = bench[d]
    s = summarize(result_with(days, []), bench)
    assert s.beta == pytest.approx(2)
    assert s.alpha == pytest.approx(0, abs=1e-9)
    assert s.benchmark_total_return == pytest.approx(bench[D3] / 100 - 1)


def test_empty_result() -> None:
    r = BacktestResult(
        params=OrbParams(), costs=CostModel.zero(), config=BacktestConfig(start=D1, end=D2)
    )
    s = summarize(r)
    assert s.trades == 0 and s.days == 0 and s.total_return == 0
    assert s.sharpe is None and s.win_rate is None and s.avg_net_r is None


def test_combine_equal_weight_averages_sleeves() -> None:
    a = result_with([DayResult(D1, 1000, 100, 100, 1), DayResult(D2, 1100, -50, -50, 1)], [])
    b = result_with([DayResult(D1, 1000, -100, -100, 1)], [])  # stopped after D1
    curve = combine_equal_weight([a, b])
    assert curve == [(D1, 1000), (D2, (1050 + 900) / 2)]
    stats = curve_stats(1000, curve)
    assert stats["total_return"] == pytest.approx(975 / 1000 - 1)
    assert stats["max_drawdown"] == pytest.approx(0.025)
    assert combine_equal_weight([]) == []
    assert curve_stats(1000, [])["total_return"] == 0.0

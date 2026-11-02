from __future__ import annotations

import math
from datetime import date

import pytest
from helpers import et

from trading_bot.backtest.simulator import Trade
from trading_bot.backtest.stats import (
    Agg,
    DayAgg,
    best_mean,
    book_from_trades,
    bootstrap_ratio_ci,
    clusters,
    daily_series,
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    pbo_cscv,
    pooled,
    probabilistic_sharpe,
    ratio_stats,
    sharpe,
    split_blocks,
)
from trading_bot.strategies.orb import Direction

D1, D2, D3 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 4, 1)


def trade(day: date, direction: Direction, gross_r: float, cost_r: float) -> Trade:
    """A one-share trade risking $1, so dollar P&L equals R."""
    sign = 1 if direction == "long" else -1
    exit_price = 10 + sign * gross_r
    return Trade(
        day=day, symbol="AAA", direction=direction, relvol=2.0, qty=1, risk_per_share=1.0,
        entry_time=et(day, 9, 35), entry_price=10.0, entry_fill=10.0, stop_price=10 - sign,
        exit_time=et(day, 15, 50), exit_price=exit_price, exit_fill=exit_price - sign * cost_r,
        exit_reason="close", fees=0.0,
    )


def test_book_from_trades_keeps_days_and_sides_apart() -> None:
    book = book_from_trades([
        trade(D1, "long", 2.0, 0.1), trade(D1, "short", -1.0, 0.1), trade(D2, "long", 0.5, 0.2),
    ])
    assert set(book) == {D1, D2}
    long = book[D1].long
    assert (long.trades, long.wins) == (1, 1)
    assert (long.gross_r, long.cost_r) == (pytest.approx(2.0), pytest.approx(0.1))
    assert book[D1].short.trades == 1 and book[D1].short.wins == 0
    total = pooled(book)
    assert total.trades == 3 and total.wins == 2
    assert total.net_r == pytest.approx(1.5 - 0.4)
    assert total.mean_net(2.0) == pytest.approx((1.5 - 0.8) / 3)  # costs doubled
    assert pooled(book, "short").net_r == pytest.approx(-1.1)
    assert Agg().mean_net() is None and DayAgg().side().trades == 0


def test_clusters_by_day_and_month() -> None:
    book = book_from_trades([
        trade(D1, "long", 1.0, 0.0), trade(D1, "long", 1.0, 0.0), trade(D2, "short", -1.0, 0.0),
        trade(D3, "long", 3.0, 1.0),
    ])
    assert sorted(clusters(book)) == sorted([(2.0, 2), (-1.0, 1), (2.0, 1)])
    assert sorted(clusters(book, by="month")) == sorted([(1.0, 3), (2.0, 1)])
    assert clusters(book, side="short") == [(-1.0, 1)]
    assert sorted(clusters(book, cost_multiplier=3)) == sorted([(2.0, 2), (-1.0, 1), (0.0, 1)])
    assert daily_series(book, [D1, date(2024, 3, 6), D3]) == [2.0, 0.0, 2.0]


def test_ratio_stats_matches_plain_se_and_widens_for_correlated_days() -> None:
    plain = ratio_stats([(1.0, 1), (-1.0, 1), (3.0, 1)])
    assert plain.mean == pytest.approx(1.0)
    assert plain.se == pytest.approx(2 / math.sqrt(3))  # sample sd 2 over sqrt(3)
    assert plain.t == pytest.approx(math.sqrt(3) / 2)
    assert plain.low == pytest.approx(1 - 1.959964 * 2 / math.sqrt(3))

    # Same four trades: independent, or two days of two identical trades.
    independent = ratio_stats([(1.0, 1), (1.0, 1), (-1.0, 1), (-1.0, 1)])
    clustered = ratio_stats([(2.0, 2), (-2.0, 2)])
    assert independent.mean == clustered.mean == 0
    assert clustered.se is not None and independent.se is not None
    assert clustered.se > independent.se

    assert ratio_stats([]).mean is None
    single = ratio_stats([(1.0, 4)])
    assert single.mean == 0.25 and single.se is None


def test_bootstrap_interval() -> None:
    groups = [(float(i % 5) - 1.5, 1) for i in range(200)]
    ci = bootstrap_ratio_ci(groups, n_boot=500, seed=1)
    assert ci is not None
    mean = ratio_stats(groups).mean
    assert mean is not None and ci[0] < mean < ci[1]
    assert bootstrap_ratio_ci(groups, n_boot=500, seed=1) == ci  # deterministic
    assert bootstrap_ratio_ci(groups[:1]) is None


def test_sharpe_and_moments() -> None:
    assert sharpe([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert sharpe([1.0, 1.0]) is None and sharpe([1.0]) is None
    skew, kurt = moments([-1.0, 0.0, 1.0])
    assert skew == pytest.approx(0) and kurt == pytest.approx(1.5)
    assert moments([1.0, 1.0, 10.0])[0] > 0  # right tail


def test_probabilistic_and_deflated_sharpe() -> None:
    assert probabilistic_sharpe(0.0, 100, 0.0, 3.0) == pytest.approx(0.5)
    low = probabilistic_sharpe(0.1, 50, 0.0, 3.0)
    high = probabilistic_sharpe(0.1, 500, 0.0, 3.0)
    assert low is not None and high is not None and 0.5 < low < high
    assert probabilistic_sharpe(0.1, 1, 0.0, 3.0) is None

    # The paper's approximation: about 2.53 standard deviations for 100 trials.
    assert expected_max_sharpe(100, 1.0) == pytest.approx(2.53, abs=0.01)
    assert expected_max_sharpe(1, 1.0) == 0.0
    dsr, sr0 = deflated_sharpe(0.1, 500, 0.0, 3.0, n_trials=50, var_sr=1 / 499)
    assert sr0 > 0 and dsr is not None and dsr < high


def test_split_blocks() -> None:
    days = [date(2024, 1, d) for d in range(1, 11)]
    blocks = split_blocks(days, 4)
    assert [len(b) for b in blocks] == [3, 3, 2, 2]
    assert [d for b in blocks for d in b] == days
    with pytest.raises(ValueError):
        split_blocks(days[:3], 4)


def block(net_per_trade: list[float]) -> list[Agg]:
    return [Agg(10, 0, 10 * x, 0.0) for x in net_per_trade]


def test_pbo_zero_when_one_variant_always_wins() -> None:
    blocks = [block([1.0, -1.0, -0.5]) for _ in range(6)]
    result = pbo_cscv(blocks)
    assert result.pbo == 0 and result.prob_oos_loss == 0 and result.mean_oos == pytest.approx(1)
    assert result.splits == math.comb(6, 3)


def test_pbo_one_when_each_variant_shines_in_a_single_block() -> None:
    # Variant v is great in block v only: whatever looks best in sample is a loser out of sample.
    n = 6
    blocks = [block([5.0 if v == s else -1.0 for v in range(n)]) for s in range(n)]
    result = pbo_cscv(blocks)
    assert result.pbo == 1 and result.prob_oos_loss == 1


def test_pbo_selection_rule_and_errors() -> None:
    blocks = [block([1.0, 2.0]) for _ in range(4)]
    blocks[0][1] = Agg(0, 0, 0.0, 0.0)  # variant 1 (the better one) has no trades in block 0
    assert pbo_cscv(blocks).pbo == 0
    # With a 20-trade minimum, variant 1 can't be picked when block 0 is in sample (half the
    # splits); variant 0 is picked instead and ranks below variant 1 out of sample.
    assert pbo_cscv(blocks, best_mean(min_trades=20)).pbo == pytest.approx(0.5)
    assert best_mean(5)([1.0, 9.0], [5, 4]) == 0  # variant 1 is below the trade minimum
    with pytest.raises(ValueError):
        pbo_cscv(blocks[:3])
    with pytest.raises(ValueError):
        pbo_cscv([block([1.0])] * 2)
    with pytest.raises(ValueError):
        pbo_cscv(blocks, lambda nets, counts: None)

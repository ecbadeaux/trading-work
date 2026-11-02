from __future__ import annotations

import pytest

from trading_bot.backtest.costs import CostModel


def test_spread_floor_and_bps() -> None:
    costs = CostModel(spread_bps=4, min_spread=0.01, slippage_bps=0, stop_slippage_bps=0)
    assert costs.adverse_move(10, "market") == pytest.approx(0.005)  # floor: 1 cent / 2
    assert costs.adverse_move(100, "market") == pytest.approx(0.02)  # 4 bps of 100 / 2


def test_stop_fills_pay_extra_and_limits_pay_nothing() -> None:
    costs = CostModel(spread_bps=0, min_spread=0, slippage_bps=1, stop_slippage_bps=2)
    assert costs.adverse_move(100, "market") == pytest.approx(0.01)
    assert costs.adverse_move(100, "stop") == pytest.approx(0.03)
    assert costs.adverse_move(100, "limit") == 0
    assert costs.fill_price(100, "buy", "stop") == pytest.approx(100.03)
    assert costs.fill_price(100, "sell", "stop") == pytest.approx(99.97)


def test_fees_only_on_sells_and_taf_capped() -> None:
    costs = CostModel(commission_per_share=0.001)
    assert costs.fees("buy", 100, 50) == pytest.approx(0.1)
    sell = costs.fees("sell", 100, 50)
    assert sell == pytest.approx(0.1 + 0.0000278 * 5000 + 0.000166 * 100)
    assert costs.fees("sell", 1_000_000, 1) - costs.fees("buy", 1_000_000, 1) == pytest.approx(
        0.0000278 * 1_000_000 + 8.30
    )


def test_zero_model_costs_nothing() -> None:
    z = CostModel.zero()
    assert z.fill_price(123.45, "buy", "stop") == 123.45
    assert z.fees("sell", 1000, 100) == 0

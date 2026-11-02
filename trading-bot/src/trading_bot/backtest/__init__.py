"""Backtesting. Entry points: ``trading-bot-backtest`` (``cli``), ``trading-bot-walkforward``
(``walkforward``) and ``trading-bot-ledger`` (``ledger``)."""

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    DataSource,
    run_backtest,
    run_backtests,
)
from trading_bot.backtest.metrics import Summary, summarize
from trading_bot.backtest.simulator import Trade, simulate_trade

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "CostModel",
    "DataSource",
    "Summary",
    "Trade",
    "run_backtest",
    "run_backtests",
    "simulate_trade",
    "summarize",
]

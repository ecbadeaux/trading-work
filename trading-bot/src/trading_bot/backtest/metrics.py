"""Performance statistics for a backtest result."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from trading_bot.backtest.engine import BacktestResult

TRADING_DAYS = 252


@dataclass(frozen=True)
class Summary:
    start: date | None
    end: date | None
    days: int
    trades: int
    long_trades: int
    short_trades: int
    initial_equity: float
    final_equity: float
    total_return: float
    cagr: float | None
    volatility: float | None
    sharpe: float | None
    max_drawdown: float
    hit_ratio_days: float | None
    """Share of days with trades whose net P&L was positive."""
    win_rate: float | None
    """Share of trades with positive net P&L."""
    avg_gross_r: float | None
    avg_net_r: float | None
    avg_cost_r: float | None
    """Average cost per trade in R. Compare with ``avg_gross_r`` to judge viability."""
    profit_factor: float | None
    best_r: float | None
    worst_r: float | None
    benchmark_total_return: float | None = None
    alpha: float | None = None
    """Annualized intercept of daily strategy returns regressed on benchmark returns."""
    beta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat() if self.start else None
        d["end"] = self.end.isoformat() if self.end else None
        return d


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.25 = -25%)."""
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1 - value / peak)
    return worst


def combine_equal_weight(results: Sequence[BacktestResult]) -> list[tuple[date, float]]:
    """Equity of a portfolio that splits capital equally across ``results`` (no rebalancing).

    Each result is one sleeve run on the full initial equity, so the combined equity on a day is
    the average of the sleeves' equities. A sleeve that stopped early keeps its last equity.
    Returns end-of-day ``(day, equity)`` pairs.
    """
    if not results:
        return []
    curves = [{d.day: d.equity_end for d in r.days} for r in results]
    last = [r.config.initial_equity for r in results]
    combined: list[tuple[date, float]] = []
    for day in sorted(set().union(*curves)):
        for k, curve in enumerate(curves):
            last[k] = curve.get(day, last[k])
        combined.append((day, statistics.fmean(last)))
    return combined


def curve_stats(initial: float, curve: Sequence[tuple[date, float]]) -> dict[str, float | None]:
    """Total return, CAGR, volatility, Sharpe and max drawdown of an end-of-day equity curve."""
    if not curve:
        return {"total_return": 0.0, "cagr": None, "volatility": None, "sharpe": None,
                "max_drawdown": 0.0}
    equity = [initial, *(e for _, e in curve)]
    returns = [b / a - 1 for a, b in zip(equity, equity[1:], strict=False) if a > 0]
    std = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    final, initial = equity[-1], equity[0]
    return {
        "total_return": final / initial - 1,
        "cagr": (final / initial) ** (TRADING_DAYS / len(returns)) - 1 if final > 0 else None,
        "volatility": std * math.sqrt(TRADING_DAYS) if std > 0 else None,
        "sharpe": statistics.fmean(returns) / std * math.sqrt(TRADING_DAYS) if std > 0 else None,
        "max_drawdown": max_drawdown(equity),
    }


def summarize(
    result: BacktestResult, benchmark_closes: Mapping[date, float] | None = None
) -> Summary:
    days = result.days
    trades = result.trades
    initial = result.config.initial_equity
    final = result.final_equity
    returns = [d.net_return for d in days]

    std = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    traded_days = [d for d in days if d.trades]
    net_rs = [t.net_r for t in trades]
    wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in trades if t.net_pnl < 0)

    bench_total: float | None = None
    alpha: float | None = None
    beta: float | None = None
    if benchmark_closes:
        bench_total, alpha, beta = _benchmark(result, benchmark_closes)

    return Summary(
        start=days[0].day if days else None,
        end=days[-1].day if days else None,
        days=len(days),
        trades=len(trades),
        long_trades=sum(1 for t in trades if t.direction == "long"),
        short_trades=sum(1 for t in trades if t.direction == "short"),
        initial_equity=initial,
        final_equity=final,
        total_return=final / initial - 1,
        cagr=(final / initial) ** (TRADING_DAYS / len(days)) - 1 if days and final > 0 else None,
        volatility=std * math.sqrt(TRADING_DAYS) if std > 0 else None,
        sharpe=statistics.fmean(returns) / std * math.sqrt(TRADING_DAYS) if std > 0 else None,
        max_drawdown=max_drawdown([initial, *(d.equity_end for d in days)]),
        hit_ratio_days=(
            sum(1 for d in traded_days if d.net_pnl > 0) / len(traded_days) if traded_days else None
        ),
        win_rate=sum(1 for t in trades if t.net_pnl > 0) / len(trades) if trades else None,
        avg_gross_r=statistics.fmean(t.gross_r for t in trades) if trades else None,
        avg_net_r=statistics.fmean(net_rs) if trades else None,
        avg_cost_r=statistics.fmean(t.cost_r for t in trades) if trades else None,
        profit_factor=wins / losses if losses > 0 else None,
        best_r=max(net_rs) if trades else None,
        worst_r=min(net_rs) if trades else None,
        benchmark_total_return=bench_total,
        alpha=alpha,
        beta=beta,
    )


def _benchmark(
    result: BacktestResult, closes: Mapping[date, float]
) -> tuple[float | None, float | None, float | None]:
    ordered = sorted(closes)
    prev_close = {cur: closes[prev] for prev, cur in zip(ordered, ordered[1:], strict=False)}
    pairs = [
        (d.net_return, closes[d.day] / prev_close[d.day] - 1)
        for d in result.days
        if d.day in closes and d.day in prev_close
    ]
    if not pairs:
        return None, None, None
    first = next(d.day for d in result.days if d.day in prev_close)
    last = next(d.day for d in reversed(result.days) if d.day in closes)
    total = closes[last] / prev_close[first] - 1
    if len(pairs) < 2:
        return total, None, None
    strat = [p[0] for p in pairs]
    bench = [p[1] for p in pairs]
    var_b = statistics.variance(bench)
    if var_b == 0:
        return total, None, None
    beta = statistics.covariance(strat, bench) / var_b
    alpha = (statistics.fmean(strat) - beta * statistics.fmean(bench)) * TRADING_DAYS
    return total, alpha, beta

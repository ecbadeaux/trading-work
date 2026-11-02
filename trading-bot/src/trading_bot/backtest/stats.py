"""Research statistics: clustered confidence intervals, Sharpe-ratio tests, overfitting checks.

Everything works on trade results in R summed per day (``Agg``/``DayAgg``), so nothing depends
on account size. Standard library only.

- ``ratio_stats``: mean net R per trade with a standard error clustered by day (or month).
  Trades on the same day move together, so treating them as independent overstates confidence.
- ``bootstrap_ratio_ci``: the same mean with a percentile interval from resampling whole days.
- ``probabilistic_sharpe`` and ``deflated_sharpe``: the chance the true Sharpe ratio is above a
  benchmark, and above the best of N unskilled trials (Bailey & López de Prado, 2012 and 2014).
- ``pbo_cscv``: probability of backtest overfitting by combinatorially symmetric
  cross-validation (Bailey, Borwein, López de Prado & Zhu, 2015).
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trading_bot.backtest.simulator import Trade

Side = Literal["long", "short"]
ClusterBy = Literal["day", "month"]
EULER_GAMMA = 0.5772156649015329
_NORMAL = statistics.NormalDist()


@dataclass(frozen=True)
class Agg:
    """Trades summed over a set (a day, a block, a year). R values are per-trade R summed."""

    trades: int = 0
    wins: int = 0
    """Trades with positive net P&L (at the actual costs)."""
    gross_r: float = 0.0
    cost_r: float = 0.0

    @property
    def net_r(self) -> float:
        return self.gross_r - self.cost_r

    def net(self, cost_multiplier: float = 1.0) -> float:
        """Summed net R if every cost were ``cost_multiplier`` times larger."""
        return self.gross_r - cost_multiplier * self.cost_r

    def mean_net(self, cost_multiplier: float = 1.0) -> float | None:
        return self.net(cost_multiplier) / self.trades if self.trades else None

    def __add__(self, other: Agg) -> Agg:
        return Agg(
            self.trades + other.trades,
            self.wins + other.wins,
            self.gross_r + other.gross_r,
            self.cost_r + other.cost_r,
        )

    @staticmethod
    def total(items: Iterable[Agg]) -> Agg:
        out = Agg()
        for item in items:
            out = out + item
        return out


@dataclass(frozen=True)
class DayAgg:
    """One day's trades, long and short kept apart."""

    long: Agg = Agg()
    short: Agg = Agg()

    def side(self, side: Side | None = None) -> Agg:
        if side == "long":
            return self.long
        if side == "short":
            return self.short
        return self.long + self.short

    def __add__(self, other: DayAgg) -> DayAgg:
        return DayAgg(self.long + other.long, self.short + other.short)


Book = dict[date, DayAgg]
"""A strategy's results by day (days without trades are absent)."""


def book_from_trades(trades: Iterable[Trade]) -> Book:
    book: Book = {}
    for t in trades:
        one = Agg(1, int(t.net_pnl > 0), t.gross_r, t.cost_r)
        entry = DayAgg(long=one) if t.direction == "long" else DayAgg(short=one)
        book[t.day] = book[t.day] + entry if t.day in book else entry
    return book


def pooled(book: Mapping[date, DayAgg], side: Side | None = None) -> Agg:
    return Agg.total(d.side(side) for d in book.values())


def clusters(
    book: Mapping[date, DayAgg],
    *,
    side: Side | None = None,
    cost_multiplier: float = 1.0,
    by: ClusterBy = "day",
) -> list[tuple[float, int]]:
    """``(summed net R, trades)`` per day or per calendar month, for clusters with trades."""
    grouped: dict[Hashable, tuple[float, int]] = defaultdict(lambda: (0.0, 0))
    for day, agg in book.items():
        a = agg.side(side)
        if not a.trades:
            continue
        key: Hashable = day if by == "day" else (day.year, day.month)
        s, n = grouped[key]
        grouped[key] = (s + a.net(cost_multiplier), n + a.trades)
    return list(grouped.values())


@dataclass(frozen=True)
class RatioStats:
    """Mean net R per trade with a cluster-robust standard error and 95% interval."""

    mean: float | None
    se: float | None
    t: float | None
    low: float | None
    high: float | None
    trades: int
    clusters: int


def ratio_stats(groups: Sequence[tuple[float, int]], z: float = 1.959964) -> RatioStats:
    """Mean of trade R (``sum / count`` pooled over ``groups``), SE clustered by group.

    Var(mean) = G / (G - 1) * sum((S_g - mean * N_g)^2) / N^2, the usual cluster-robust
    variance of a ratio estimator. With one trade per group it reduces to the plain SE.
    """
    total_n = sum(n for _, n in groups)
    if total_n == 0:
        return RatioStats(None, None, None, None, None, 0, 0)
    mean = sum(s for s, _ in groups) / total_n
    g = len(groups)
    if g < 2:
        return RatioStats(mean, None, None, None, None, total_n, g)
    resid = sum((s - mean * n) ** 2 for s, n in groups)
    se = math.sqrt(g / (g - 1) * resid) / total_n
    t = mean / se if se > 0 else None
    return RatioStats(mean, se, t, mean - z * se, mean + z * se, total_n, g)


def bootstrap_ratio_ci(
    groups: Sequence[tuple[float, int]],
    n_boot: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile interval for mean R per trade, resampling whole groups with replacement."""
    if len(groups) < 2 or n_boot <= 0:
        return None
    rng = random.Random(seed)
    sums = [s for s, _ in groups]
    counts = [n for _, n in groups]
    idx = range(len(groups))
    means: list[float] = []
    for _ in range(n_boot):
        pick = rng.choices(idx, k=len(groups))
        n = sum(counts[i] for i in pick)
        if n:
            means.append(sum(sums[i] for i in pick) / n)
    if not means:
        return None
    means.sort()
    tail = (1 - level) / 2
    lo = means[min(len(means) - 1, int(tail * len(means)))]
    hi = means[min(len(means) - 1, int((1 - tail) * len(means)))]
    return lo, hi


def daily_series(
    book: Mapping[date, DayAgg], calendar: Iterable[date], cost_multiplier: float = 1.0
) -> list[float]:
    """Summed net R for every day in ``calendar`` (0 on days without trades)."""
    return [book[d].side().net(cost_multiplier) if d in book else 0.0 for d in calendar]


def sharpe(xs: Sequence[float]) -> float | None:
    """Per-period Sharpe ratio (mean / sample standard deviation); not annualized."""
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / sd if sd > 0 else None


def moments(xs: Sequence[float]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis; a normal distribution gives (0, 3)."""
    n = len(xs)
    if n < 2:
        return 0.0, 3.0
    mu = statistics.fmean(xs)
    m2 = sum((x - mu) ** 2 for x in xs) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = sum((x - mu) ** 3 for x in xs) / n
    m4 = sum((x - mu) ** 4 for x in xs) / n
    return m3 / m2**1.5, m4 / m2**2


def probabilistic_sharpe(
    sr: float, n_obs: int, skew: float, kurt: float, benchmark: float = 0.0
) -> float | None:
    """Probability that the true per-period Sharpe exceeds ``benchmark`` (Bailey & López de
    Prado 2012). ``kurt`` is non-excess (3 for a normal)."""
    if n_obs < 2:
        return None
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return None
    return _NORMAL.cdf((sr - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected best per-period Sharpe among ``n_trials`` unskilled strategies whose Sharpe
    estimates have variance ``var_sr`` (the false-strategy theorem)."""
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    a = _NORMAL.inv_cdf(1 - 1 / n_trials)
    b = _NORMAL.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(
    sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, var_sr: float
) -> tuple[float | None, float]:
    """``(DSR, SR0)``: the probabilistic Sharpe against the expected best of ``n_trials``
    (Bailey & López de Prado 2014). DSR >= 0.95 is the usual bar."""
    sr0 = expected_max_sharpe(n_trials, var_sr)
    return probabilistic_sharpe(sr, n_obs, skew, kurt, sr0), sr0


def split_blocks(calendar: Sequence[date], n_blocks: int) -> list[list[date]]:
    """Cut a sorted calendar into ``n_blocks`` contiguous, nearly equal blocks."""
    if n_blocks <= 0 or len(calendar) < n_blocks:
        raise ValueError("need at least one day per block")
    size, extra = divmod(len(calendar), n_blocks)
    out: list[list[date]] = []
    start = 0
    for b in range(n_blocks):
        end = start + size + (1 if b < extra else 0)
        out.append(list(calendar[start:end]))
        start = end
    return out


@dataclass(frozen=True)
class PboResult:
    pbo: float
    """Share of splits where the in-sample winner ranks at or below the out-of-sample median."""
    prob_oos_loss: float
    """Share of splits where the in-sample winner loses money out of sample."""
    mean_oos: float
    """Average out-of-sample mean net R per trade of the in-sample winner."""
    splits: int
    logits: tuple[float, ...]


Chooser = Callable[[Sequence[float], Sequence[int]], int | None]
"""Selection rule: (summed net R, trades) per variant -> the index picked, or None."""


def best_mean(min_trades: int = 1) -> Chooser:
    """Pick the highest mean net R per trade among variants with at least ``min_trades``."""

    def choose(nets: Sequence[float], counts: Sequence[int]) -> int | None:
        best: int | None = None
        best_mean = -math.inf
        for v, (s, n) in enumerate(zip(nets, counts, strict=True)):
            if n >= max(min_trades, 1) and s / n > best_mean:
                best, best_mean = v, s / n
        return best

    return choose


def pbo_cscv(blocks: Sequence[Sequence[Agg]], choose: Chooser | None = None) -> PboResult:
    """Probability of backtest overfitting for a selection rule (default: ``best_mean()``).

    ``blocks[s][v]`` is variant ``v``'s trades in time block ``s`` (an even number of blocks,
    usually 16). Every half of the blocks is used once as in-sample, its complement as
    out-of-sample; the rule picks on the in-sample half and the pick is ranked out of sample by
    mean net R per trade.
    """
    choose = choose or best_mean()
    n_blocks = len(blocks)
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("need an even number of blocks (at least 2)")
    n_var = len(blocks[0])
    if n_var < 2 or any(len(row) != n_var for row in blocks):
        raise ValueError("need at least two variants, the same in every block")
    nets = [[a.net_r for a in row] for row in blocks]
    counts = [[a.trades for a in row] for row in blocks]
    total_net = [sum(nets[s][v] for s in range(n_blocks)) for v in range(n_var)]
    total_n = [sum(counts[s][v] for s in range(n_blocks)) for v in range(n_var)]

    def perf(net: Sequence[float], n: Sequence[int]) -> list[float]:
        return [net[v] / n[v] if n[v] > 0 else -math.inf for v in range(n_var)]

    logits: list[float] = []
    oos_best: list[float] = []
    half = n_blocks // 2
    # Each split and its complement are both evaluated, so only combinations holding block 0
    # need enumerating.
    for combo in itertools.combinations(range(1, n_blocks), half - 1):
        chosen = (0, *combo)
        a_net = [sum(nets[s][v] for s in chosen) for v in range(n_var)]
        a_n = [sum(counts[s][v] for s in chosen) for v in range(n_var)]
        b_net = [total_net[v] - a_net[v] for v in range(n_var)]
        b_n = [total_n[v] - a_n[v] for v in range(n_var)]
        for is_net, is_n, oos_net, oos_n in ((a_net, a_n, b_net, b_n), (b_net, b_n, a_net, a_n)):
            best = choose(is_net, is_n)
            if best is None:
                continue
            oos_perf = perf(oos_net, oos_n)
            mine = oos_perf[best]
            below = sum(1 for v in range(n_var) if oos_perf[v] < mine)
            ties = sum(1 for v in range(n_var) if v != best and oos_perf[v] == mine)
            omega = (below + 1 + ties / 2) / (n_var + 1)
            logits.append(math.log(omega / (1 - omega)))
            if mine != -math.inf:
                oos_best.append(mine)
    if not logits:
        raise ValueError("the selection rule picked nothing in any split")
    return PboResult(
        pbo=sum(1 for x in logits if x <= 0) / len(logits),
        prob_oos_loss=sum(1 for x in oos_best if x < 0) / len(oos_best) if oos_best else 1.0,
        mean_oos=statistics.fmean(oos_best) if oos_best else math.nan,
        splits=len(logits),
        logits=tuple(logits),
    )

"""Day-by-day ORB backtest over a symbol universe.

For each trading day:

1. Daily filters (price, 14-day average volume, 14-day ATR) come from prior sessions only.
2. At the end of the opening range, relative volume (RelVol) is today's opening-range volume
   over its 14-day average, and the top N candidates are chosen (``select_candidates``).
3. Each candidate is sized from start-of-day equity and simulated on minute bars.
4. Equity is updated with the day's net P&L.

To keep data requests manageable, opening-range bars are fetched only for symbols that pass the
daily filters on some day whose RelVol window needs them.

``run_backtests`` runs several parameter sets (variants) in one pass over the data: daily bars,
daily filters and opening-range bars are loaded once and shared, which is what makes parameter
grids and walk-forward runs affordable. ``run_backtest`` is the one-variant case.

``BacktestConfig.fills = "ticks"`` prices fills from trades and quotes (a ``TickSource``, see
``simulator.py``) instead of the cost model's spread.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Literal, Protocol

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.simulator import Trade, simulate_trade
from trading_bot.data.models import Bar, DailyBar, TradingDay
from trading_bot.data.ticks import TickSource
from trading_bot.strategies.orb import (
    DailyStats,
    OpeningRange,
    OrbCandidate,
    OrbParams,
    daily_stats,
    opening_range,
    position_size,
    relative_volume,
    select_candidates,
    stop_distance,
)

log = logging.getLogger(__name__)

# Daily prefilter slack on price: the filter proper uses the opening-range open, which can differ
# slightly from the daily bar's official open.
_PRICE_PREFILTER_SLACK = 0.95


class DataSource(Protocol):
    """What the engine needs from a market data provider."""

    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]: ...

    def opening_bars(
        self, symbols: Sequence[str], day: TradingDay, minutes: int
    ) -> dict[str, list[Bar]]: ...

    def session_bars(self, symbols: Sequence[str], day: TradingDay) -> dict[str, list[Bar]]: ...


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_equity: float = 25_000.0
    risk_per_trade: float = 0.01
    """Fraction of equity lost if a stop is hit (before the leverage cap)."""
    max_leverage: float = 4.0
    """Gross exposure cap as a multiple of equity, split equally across ``top_n`` slots."""
    worst_case_intrabar: bool = True
    seed: int = 0
    """Seed for ``direction_mode="random"``."""
    fills: Literal["bars", "ticks"] = "bars"
    """``bars``: the cost model on minute-bar prices. ``ticks``: real quotes and trades."""
    tick_latency_ms: int = 100
    """With tick fills: delay from a decision to the quote a market order fills against."""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")
        if not 0 < self.risk_per_trade < 1:
            raise ValueError("risk_per_trade must be between 0 and 1")
        if self.max_leverage <= 0:
            raise ValueError("max_leverage must be > 0")
        if self.fills not in ("bars", "ticks"):
            raise ValueError("fills must be 'bars' or 'ticks'")
        if self.tick_latency_ms < 0:
            raise ValueError("tick_latency_ms must be >= 0")


@dataclass(frozen=True)
class DayResult:
    day: date
    equity_start: float
    gross_pnl: float
    net_pnl: float
    trades: int

    @property
    def equity_end(self) -> float:
        return self.equity_start + self.net_pnl

    @property
    def net_return(self) -> float:
        return self.net_pnl / self.equity_start


@dataclass
class BacktestResult:
    params: OrbParams
    costs: CostModel
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    days: list[DayResult] = field(default_factory=list)
    diagnostics: Counter[str] = field(default_factory=Counter)

    @property
    def final_equity(self) -> float:
        return self.days[-1].equity_end if self.days else self.config.initial_equity


ProgressFn = Callable[[int, int, date], None]


def history_start(start: date, lookback_days: int) -> date:
    """Calendar date early enough to cover ``lookback_days + 1`` sessions before ``start``."""
    return start - timedelta(days=int((lookback_days + 1) * 7 / 5) + 14)


def run_backtest(
    source: DataSource,
    universe: Sequence[str],
    params: OrbParams,
    costs: CostModel,
    config: BacktestConfig,
    progress: ProgressFn | None = None,
    ticks: TickSource | None = None,
) -> BacktestResult:
    return run_backtests(source, universe, [params], costs, config, progress, ticks=ticks)[0]


def eligibility_key(params: OrbParams) -> tuple[int, float, float, float]:
    """The parameters behind the daily prefilter. Variants run together must share them."""
    return (params.lookback_days, params.min_price, params.min_avg_volume, params.min_atr)


def run_backtests(
    source: DataSource,
    universe: Sequence[str],
    variants: Sequence[OrbParams],
    costs: CostModel,
    config: BacktestConfig,
    progress: ProgressFn | None = None,
    seeds: Sequence[int] | None = None,
    ticks: TickSource | None = None,
) -> list[BacktestResult]:
    """Backtest several variants in one pass over the data; one result per variant, in order.

    Each variant keeps its own equity, random stream and diagnostics, so every result equals a
    separate ``run_backtest`` call. Opening-range bars are fetched once per day for the longest
    ``or_minutes`` among the variants. ``seeds`` (one per variant) overrides ``config.seed``,
    e.g. for several random-direction placebos. ``ticks`` is required with
    ``config.fills == "ticks"`` and ignored otherwise.
    """
    if not variants:
        return []
    if config.fills == "ticks" and ticks is None:
        raise ValueError("tick fills need a tick source")
    tape = ticks if config.fills == "ticks" else None
    key = eligibility_key(variants[0])
    if any(eligibility_key(v) != key for v in variants):
        raise ValueError("variants must share lookback_days, min_price, min_avg_volume, min_atr")
    seeds = [config.seed] * len(variants) if seeds is None else list(seeds)
    if len(seeds) != len(variants):
        raise ValueError("need one seed per variant")
    results = [
        BacktestResult(params=v, costs=costs, config=replace(config, seed=s))
        for v, s in zip(variants, seeds, strict=True)
    ]
    lookback = variants[0].lookback_days

    days = source.trading_days(history_start(config.start, lookback), config.end)
    first_test = next((i for i, d in enumerate(days) if d.day >= config.start), None)
    if first_test is None:
        log.warning("No trading days between %s and %s", config.start, config.end)
        return results
    if first_test < lookback + 1:
        raise ValueError(
            f"need {lookback + 1} sessions of history before {config.start}; got {first_test}"
        )

    log.info("Loading daily bars for %d symbols", len(universe))
    daily = source.daily_bars(universe, days[0].day, config.end)
    shared: Counter[str] = Counter()
    eligible = daily_eligibility(days, daily, variants[0], first_test, shared)
    for r in results:
        r.diagnostics.update(shared)
    needed = needed_by_day(eligible, lookback)

    # Pass 2: walk the days.
    lengths = sorted({v.or_minutes for v in variants})
    rngs = [random.Random(s) for s in seeds]
    or_history: dict[int, dict[int, dict[str, OpeningRange]]] = {m: {} for m in lengths}
    equity = [config.initial_equity] * len(variants)
    running = [True] * len(variants)
    first_needed = min(needed) if needed else first_test
    total = len(days) - first_needed
    for i in range(first_needed, len(days)):
        day = days[i]
        if progress:
            progress(i - first_needed + 1, total, day.day)
        symbols = sorted(needed.get(i, ()))
        raw_or = source.opening_bars(symbols, day, lengths[-1]) if symbols else {}
        for m in lengths:
            cut = day.open + timedelta(minutes=m)
            history = or_history[m]
            history[i] = {
                sym: rng_bar
                for sym, bars in raw_or.items()
                if (rng_bar := opening_range([b for b in bars if b.timestamp < cut])) is not None
            }
            history.pop(i - lookback - 1, None)
        if i < first_test:
            continue

        inputs = {m: _candidate_inputs(i, eligible[i], or_history[m], lookback) for m in lengths}
        picks: list[list[OrbCandidate]] = []
        for k, v in enumerate(variants):
            candidates = select_candidates(inputs[v.or_minutes], v, rngs[k]) if running[k] else []
            if running[k]:
                results[k].diagnostics["days"] += 1
                results[k].diagnostics["candidates"] += len(candidates)
            picks.append(candidates)
        wanted = sorted({c.symbol for candidates in picks for c in candidates})
        sessions = source.session_bars(wanted, day) if wanted else {}

        for k, v in enumerate(variants):
            if not running[k]:
                continue
            result = results[k]
            day_trades = _trade_day(
                day, picks[k], sessions, v, costs, config, equity[k], result.diagnostics, tape
            )
            gross = sum(t.gross_pnl for t in day_trades)
            net = sum(t.net_pnl for t in day_trades)
            result.days.append(DayResult(day.day, equity[k], gross, net, len(day_trades)))
            result.trades.extend(day_trades)
            equity[k] += net
            if equity[k] <= 0:
                log.warning("Equity exhausted on %s; stopping", day.day)
                result.diagnostics["equity_exhausted"] += 1
                running[k] = False
        if not any(running):
            break

    return results


def _trade_day(
    day: TradingDay,
    candidates: Sequence[OrbCandidate],
    sessions: Mapping[str, Sequence[Bar]],
    params: OrbParams,
    costs: CostModel,
    config: BacktestConfig,
    equity: float,
    diag: Counter[str],
    ticks: TickSource | None = None,
) -> list[Trade]:
    """Size and simulate one variant's candidates for one day."""
    if not candidates:
        return []
    or_end = day.open + timedelta(minutes=params.or_minutes)
    exit_at = day.close - timedelta(minutes=params.exit_minutes_before_close)
    cutoffs = []
    if params.last_entry_minutes_before_close is not None:
        cutoffs.append(day.close - timedelta(minutes=params.last_entry_minutes_before_close))
    if params.entry_window_minutes is not None:
        cutoffs.append(or_end + timedelta(minutes=params.entry_window_minutes))
    last_entry_at = min(cutoffs) if cutoffs else None

    trades: list[Trade] = []
    for c in candidates:
        qty = position_size(
            equity,
            c.trigger,
            stop_distance(c, params),
            config.risk_per_trade,
            config.max_leverage,
            params.top_n,
        )
        if qty <= 0:
            diag["skipped_zero_qty"] += 1
            continue
        trade = simulate_trade(
            day.day,
            c,
            sessions.get(c.symbol, ()),
            or_end=or_end,
            exit_at=exit_at,
            qty=qty,
            params=params,
            costs=costs,
            last_entry_at=last_entry_at,
            worst_case_intrabar=config.worst_case_intrabar,
            ticks=ticks,
            tick_latency_ms=config.tick_latency_ms,
        )
        if trade is None:
            diag["not_triggered"] += 1
            continue
        diag[f"exit_{trade.exit_reason}"] += 1
        if ticks is not None:
            diag[f"fills_{trade.fill_source}"] += 1
            for note in trade.tick_notes:
                diag[f"tick_{note}"] += 1
        trades.append(trade)
    return trades


def daily_eligibility(
    days: Sequence[TradingDay],
    daily: Mapping[str, Sequence[DailyBar]],
    params: OrbParams,
    first_test: int,
    diag: Counter[str] | None = None,
) -> dict[int, dict[str, DailyStats]]:
    """Pass 1: for each test day index, the symbols passing the daily filters, with their stats.

    Uses the ``lookback_days + 1`` sessions before the day only. ``days[first_test:]`` are the
    test days; earlier entries supply history. The price filter is applied loosely here (the
    exact check uses the opening-range open in ``select_candidates``).
    """
    lookback = params.lookback_days
    by_day = {sym: {b.day: b for b in bars} for sym, bars in daily.items()}
    eligible: dict[int, dict[str, DailyStats]] = {}
    for i in range(first_test, len(days)):
        prior = [d.day for d in days[i - lookback - 1 : i]]
        today = days[i].day
        todays: dict[str, DailyStats] = {}
        for sym, bars_by_day in by_day.items():
            if today not in bars_by_day:
                continue
            history = [bars_by_day[d] for d in prior if d in bars_by_day]
            if len(history) < lookback + 1:
                continue
            # A split inside the window (including today) makes raw prices and volumes
            # incomparable, so skip the symbol until the window is clean again.
            if _split_suspected(history, bars_by_day[today]):
                if diag is not None:
                    diag["split_guard_skips"] += 1
                continue
            stats = daily_stats(history, lookback)
            if stats is None:
                continue
            open_price = bars_by_day[today].open
            if (
                stats.avg_volume >= params.min_avg_volume
                and stats.atr > params.min_atr
                and open_price > params.min_price * _PRICE_PREFILTER_SLACK
            ):
                todays[sym] = stats
        eligible[i] = todays
    return eligible


def needed_by_day(
    eligible: Mapping[int, Mapping[str, DailyStats]], lookback: int
) -> dict[int, set[str]]:
    """Symbols whose opening range is needed on day j: eligible on j, or on a later test day
    whose RelVol lookback window includes j."""
    needed: dict[int, set[str]] = {}
    for i, todays in eligible.items():
        for j in range(i - lookback, i + 1):
            needed.setdefault(j, set()).update(todays)
    return needed


def _candidate_inputs(
    i: int,
    eligible_today: Mapping[str, DailyStats],
    or_history: Mapping[int, Mapping[str, OpeningRange]],
    lookback: int,
) -> dict[str, tuple[OpeningRange, DailyStats, float]]:
    todays_or = or_history.get(i, {})
    inputs: dict[str, tuple[OpeningRange, DailyStats, float]] = {}
    for sym, stats in eligible_today.items():
        rng_bar = todays_or.get(sym)
        if rng_bar is None:
            continue
        prior = [
            or_history[j][sym].volume if sym in or_history.get(j, {}) else 0.0
            for j in range(i - lookback, i)
        ]
        relvol = relative_volume(rng_bar.volume, prior)
        if relvol is not None:
            inputs[sym] = (rng_bar, stats, relvol)
    return inputs


def _split_suspected(history: Sequence[DailyBar], today: DailyBar) -> bool:
    factors = [b.close / b.adj_close for b in [*history, today] if b.adj_close]
    return len(factors) >= 2 and max(factors) / min(factors) > 1.01

"""Opening range breakout (ORB) on Stocks in Play: the pure decision logic.

Rules and the research behind every default are in ``docs/research/orb.md``. Everything here is
a pure function of data known at decision time, so the backtester and live trading can share it.
Nothing in this module talks to Alpaca.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from trading_bot.data.models import Bar, DailyBar

Direction = Literal["long", "short"]
DirectionMode = Literal["opening", "random", "inverse"]
StopMode = Literal["atr", "range"]
EntryMode = Literal["stop", "close", "market"]


@dataclass(frozen=True)
class OrbParams:
    """Strategy parameters. Defaults follow Zarattini, Barbon & Aziz (2024)."""

    or_minutes: int = 5
    """Length of the opening range in minutes, starting at the open."""
    lookback_days: int = 14
    """Days used for ATR, average daily volume and average opening-range volume."""
    min_price: float = 5.0
    min_avg_volume: float = 1_000_000
    min_atr: float = 0.50
    min_relvol: float = 1.0
    """Minimum relative volume (1.0 = the opening range traded at least its usual volume)."""
    top_n: int = 20
    """Trade at most this many symbols per day, highest relative volume first."""
    min_gap: float = 0.0
    """Minimum overnight gap, ``|open / prior close - 1|`` (0.02 = 2%). 0 disables."""
    gap_aligned: bool = False
    """Trade only when the gap and the opening candle point the same way."""
    min_atr_pct: float = 0.0
    """Minimum ATR as a fraction of the open (0.01 = 1%). 0 disables."""
    min_range_pct: float = 0.0
    """Minimum opening-range width (high - low) as a fraction of the open. 0 disables."""
    min_range_atr: float = 0.0
    """Minimum opening-range width in ATRs. 0 disables."""
    max_range_atr: float | None = None
    """Maximum opening-range width in ATRs. ``None`` disables."""
    entry_mode: EntryMode = "stop"
    """``stop``: stop order at the range high/low. ``close``: enter at the next bar's open after a
    bar closes beyond the range. ``market``: enter at the first bar's open after the range."""
    entry_window_minutes: int | None = None
    """No new entries later than this many minutes after the range ends. ``None``: no limit."""
    stop_mode: StopMode = "atr"
    """``atr``: stop ``stop_atr_fraction`` x ATR from entry. ``range``: far side of the range."""
    stop_atr_fraction: float = 0.10
    target_r: float | None = None
    """Profit target in R. ``None`` (the default) holds until the flatten time."""
    breakeven_r: float | None = None
    """Move the stop to the entry price once a bar reaches this many R of profit (from the next
    bar). ``None`` disables."""
    vwap_exit: bool = False
    """Exit at the next bar's open after a bar closes on the wrong side of session VWAP."""
    exit_minutes_before_close: int = 10
    """Flatten time. The papers hold to the close (0); our session manager flattens at 15:50."""
    last_entry_minutes_before_close: int | None = None
    """No new entries after this many minutes before the close. ``None``: until the flatten."""
    direction_mode: DirectionMode = "opening"
    """``opening`` trades with the opening candle. ``random``/``inverse`` are placebo checks."""

    def __post_init__(self) -> None:
        if self.or_minutes <= 0:
            raise ValueError("or_minutes must be > 0")
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be > 0")
        if self.top_n <= 0:
            raise ValueError("top_n must be > 0")
        if self.stop_mode == "atr" and self.stop_atr_fraction <= 0:
            raise ValueError("stop_atr_fraction must be > 0")
        if self.target_r is not None and self.target_r <= 0:
            raise ValueError("target_r must be > 0 when set")
        if self.breakeven_r is not None and self.breakeven_r <= 0:
            raise ValueError("breakeven_r must be > 0 when set")
        if min(self.min_gap, self.min_atr_pct, self.min_range_pct, self.min_range_atr) < 0:
            raise ValueError("min_gap, min_atr_pct, min_range_pct and min_range_atr must be >= 0")
        if self.max_range_atr is not None and self.max_range_atr <= self.min_range_atr:
            raise ValueError("max_range_atr must be > min_range_atr")
        if self.entry_window_minutes is not None and self.entry_window_minutes <= 0:
            raise ValueError("entry_window_minutes must be > 0 when set")
        if self.exit_minutes_before_close < 0:
            raise ValueError("exit_minutes_before_close must be >= 0")
        if (
            self.last_entry_minutes_before_close is not None
            and self.last_entry_minutes_before_close < self.exit_minutes_before_close
        ):
            raise ValueError("last_entry_minutes_before_close must be >= exit_minutes_before_close")


@dataclass(frozen=True)
class OpeningRange:
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def direction(self) -> Direction | None:
        """Direction of the opening candle; ``None`` for a doji (close == open)."""
        if self.close > self.open:
            return "long"
        if self.close < self.open:
            return "short"
        return None


@dataclass(frozen=True)
class DailyStats:
    """Per-symbol statistics computed from completed prior days only (no lookahead)."""

    atr: float
    avg_volume: float
    prev_close: float | None = None
    """Raw close of the last completed session, for the gap filter."""


@dataclass(frozen=True)
class OrbCandidate:
    """A symbol selected for today, with everything needed to place the entry order."""

    symbol: str
    direction: Direction
    relvol: float
    opening_range: OpeningRange
    atr: float

    @property
    def trigger(self) -> float:
        """Entry stop price: the range high for longs, the range low for shorts."""
        return self.opening_range.high if self.direction == "long" else self.opening_range.low


def opening_range(bars: Sequence[Bar]) -> OpeningRange | None:
    """Aggregate the opening-range bars (in time order). ``None`` if there are none."""
    if not bars:
        return None
    return OpeningRange(
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
    )


def average_true_range(daily: Sequence[DailyBar]) -> float | None:
    """Simple average of the true range over ``daily[1:]``.

    ``daily`` must be consecutive sessions in time order. The first bar only supplies the previous
    close, so N + 1 bars give an N-day ATR.
    """
    if len(daily) < 2:
        return None
    ranges = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(daily, daily[1:], strict=False)
    ]
    return sum(ranges) / len(ranges)


def daily_stats(history: Sequence[DailyBar], lookback_days: int) -> DailyStats | None:
    """ATR and average daily volume from the ``lookback_days + 1`` sessions before today.

    Returns ``None`` when history is incomplete or a split happened inside the window (raw
    prices on either side of a split are not comparable).
    """
    if len(history) < lookback_days + 1:
        return None
    window = history[-(lookback_days + 1) :]
    if _split_in_window(window):
        return None
    atr = average_true_range(window)
    if atr is None:
        return None
    avg_volume = sum(b.volume for b in window[1:]) / lookback_days
    return DailyStats(atr=atr, avg_volume=avg_volume, prev_close=window[-1].close)


def _split_in_window(window: Sequence[DailyBar]) -> bool:
    factors = [b.close / b.adj_close for b in window if b.adj_close]
    if len(factors) < 2:
        return False
    return max(factors) / min(factors) > 1.01


def passes_daily_filters(stats: DailyStats, open_price: float, params: OrbParams) -> bool:
    return (
        open_price > params.min_price
        and stats.avg_volume >= params.min_avg_volume
        and stats.atr > params.min_atr
        and stats.atr >= params.min_atr_pct * open_price
    )


def gap(stats: DailyStats, open_price: float) -> float | None:
    """Overnight gap as a fraction of the prior close (+0.03 = opened 3% higher)."""
    if not stats.prev_close:
        return None
    return open_price / stats.prev_close - 1


def passes_setup_filters(rng_bar: OpeningRange, stats: DailyStats, params: OrbParams) -> bool:
    """Gap and opening-range width filters. All are off by default."""
    direction = rng_bar.direction
    if params.min_gap > 0 or params.gap_aligned:
        g = gap(stats, rng_bar.open)
        if g is None or abs(g) < params.min_gap:
            return False
        if params.gap_aligned and (g > 0) != (direction == "long"):
            return False
    width = rng_bar.high - rng_bar.low
    if width < params.min_range_pct * rng_bar.open:
        return False
    if width < params.min_range_atr * stats.atr:
        return False
    return params.max_range_atr is None or width <= params.max_range_atr * stats.atr


def relative_volume(or_volume: float, prior_or_volumes: Sequence[float]) -> float | None:
    """Today's opening-range volume divided by its average over prior days."""
    if not prior_or_volumes:
        return None
    avg = sum(prior_or_volumes) / len(prior_or_volumes)
    if avg <= 0:
        return None
    return or_volume / avg


def select_candidates(
    inputs: Mapping[str, tuple[OpeningRange, DailyStats, float]],
    params: OrbParams,
    rng: random.Random | None = None,
) -> list[OrbCandidate]:
    """Pick today's trades.

    ``inputs`` maps symbol -> (opening range, daily stats, relative volume). Applies the daily
    and setup filters, drops dojis and low relative volume, then keeps the ``top_n`` by relative
    volume (ties broken by symbol so results are deterministic).
    """
    eligible: list[OrbCandidate] = []
    for symbol, (rng_bar, stats, relvol) in inputs.items():
        direction = rng_bar.direction
        if direction is None or relvol < params.min_relvol:
            continue
        if not passes_daily_filters(stats, rng_bar.open, params):
            continue
        if not passes_setup_filters(rng_bar, stats, params):
            continue
        eligible.append(OrbCandidate(symbol, direction, relvol, rng_bar, stats.atr))

    eligible.sort(key=lambda c: (-c.relvol, c.symbol))
    selected = eligible[: params.top_n]

    if params.direction_mode == "opening":
        return selected
    if params.direction_mode == "inverse":
        return [_with_direction(c, _flip(c.direction)) for c in selected]
    chooser = rng or random.Random(0)
    return [
        _with_direction(c, "long" if chooser.random() < 0.5 else "short") for c in selected
    ]


def _flip(direction: Direction) -> Direction:
    return "short" if direction == "long" else "long"


def _with_direction(c: OrbCandidate, direction: Direction) -> OrbCandidate:
    return OrbCandidate(c.symbol, direction, c.relvol, c.opening_range, c.atr)


def stop_distance(candidate: OrbCandidate, params: OrbParams) -> float:
    """Planned distance from entry to stop, in dollars per share. This is 1R."""
    if params.stop_mode == "atr":
        return candidate.atr * params.stop_atr_fraction
    return candidate.opening_range.high - candidate.opening_range.low


def position_size(
    equity: float,
    entry_price: float,
    risk_per_share: float,
    risk_fraction: float,
    max_leverage: float,
    max_positions: int,
) -> int:
    """Whole shares so a stop-out loses ``risk_fraction`` of equity, capped by leverage.

    The cap gives each of ``max_positions`` an equal share of ``equity * max_leverage``, so gross
    exposure never exceeds ``max_leverage`` even if every slot fills.
    """
    if equity <= 0 or entry_price <= 0 or risk_per_share <= 0 or max_positions <= 0:
        return 0
    by_risk = equity * risk_fraction / risk_per_share
    by_leverage = equity * max_leverage / max_positions / entry_price
    return int(min(by_risk, by_leverage))

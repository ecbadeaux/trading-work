"""Simulate one ORB trade on minute bars.

Fill assumptions (all deliberately conservative):

- Stop entry: fills at the trigger, or at the bar's open if the bar gapped past it.
- Stop-loss: fills at the stop, or at the bar's open if the bar gapped past it.
- If the bar that triggered the entry also reaches the stop, we assume the stop was hit
  (``worst_case_intrabar``). Minute bars cannot show which came first.
- If a later bar touches both the stop and the target, the stop wins.
- Market exits (flatten time, VWAP exit) fill at the next bar's open.
- Close-confirmed entries (``entry_mode="close"``) fill at the open of the bar after the close.
- A breakeven move (``breakeven_r``) takes effect from the bar after the one that reached it.

Costs are applied on top of these reference prices, so gross and net results can both be
reported.

With ``ticks`` (tick fills, ``backtest/ticks.py``), the reference prices and the stop stay the
same, but the tape decides what happened inside a minute and what each fill cost:

- the entry fills at the far side of the real quote just after the electing trade;
- the entry minute's trades show whether the stop elected after the entry, so the
  ``worst_case_intrabar`` guess is not needed;
- stop exits fill at the real quote after their electing trade, market exits at the real quote
  at the exit time.

Only ``CostModel.slippage_bps`` and fees are added to tick fills. Any fill whose window is
missing falls back to the bar model; ``Trade.fill_source`` and ``Trade.tick_notes`` say so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trading_bot.backtest.costs import CostModel, FillKind, Side
from trading_bot.backtest.ticks import elect, market_fill
from trading_bot.data.models import Bar
from trading_bot.data.ticks import MinuteTicks, TickSource, epoch_us
from trading_bot.strategies.orb import Direction, OrbCandidate, OrbParams, stop_distance

ExitReason = Literal["stop", "breakeven", "target", "vwap", "close"]


@dataclass(frozen=True)
class Trade:
    day: date
    symbol: str
    direction: Direction
    relvol: float
    qty: int
    risk_per_share: float
    """Planned entry-to-stop distance: 1R per share."""
    entry_time: datetime
    entry_price: float
    """Reference entry price before costs."""
    entry_fill: float
    """Entry price after spread and slippage."""
    stop_price: float
    exit_time: datetime
    exit_price: float
    exit_fill: float
    exit_reason: ExitReason
    fees: float
    fill_source: str = "bars"
    """``bars`` (modelled costs), ``ticks`` (both fills from the tape) or ``mixed``."""
    entry_spread: float | None = None
    """Quoted spread (dollars) at the entry fill, with tick fills."""
    tick_notes: tuple[str, ...] = ()
    """Why a tick fill fell back to the bar model (e.g. ``missing_entry``)."""

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def gross_pnl(self) -> float:
        return self.sign * (self.exit_price - self.entry_price) * self.qty

    @property
    def net_pnl(self) -> float:
        return self.sign * (self.exit_fill - self.entry_fill) * self.qty - self.fees

    @property
    def cost(self) -> float:
        return self.gross_pnl - self.net_pnl

    @property
    def risk_dollars(self) -> float:
        return self.risk_per_share * self.qty

    @property
    def gross_r(self) -> float:
        return self.gross_pnl / self.risk_dollars

    @property
    def net_r(self) -> float:
        return self.net_pnl / self.risk_dollars

    @property
    def cost_r(self) -> float:
        return self.cost / self.risk_dollars


def running_vwap(bars: Sequence[Bar]) -> list[float | None]:
    """Session VWAP as of each bar's close. Uses each bar's own VWAP, or its typical price."""
    out: list[float | None] = []
    pv = vol = 0.0
    for b in bars:
        if b.volume > 0:
            price = b.vwap if b.vwap is not None else (b.high + b.low + b.close) / 3
            pv += price * b.volume
            vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


def simulate_trade(
    day: date,
    candidate: OrbCandidate,
    bars: Sequence[Bar],
    *,
    or_end: datetime,
    exit_at: datetime,
    qty: int,
    params: OrbParams,
    costs: CostModel,
    last_entry_at: datetime | None = None,
    worst_case_intrabar: bool = True,
    ticks: TickSource | None = None,
    tick_latency_ms: int = 100,
) -> Trade | None:
    """Simulate ``candidate`` over the session's minute bars (time order, from the open).

    Returns ``None`` if the entry never triggers before the cutoff, or if ``qty`` is zero.
    ``ticks`` switches on tick fills (see the module docstring).
    """
    risk = stop_distance(candidate, params)
    if qty <= 0 or risk <= 0 or not bars:
        return None
    long = candidate.direction == "long"
    sign = 1 if long else -1
    entry_cutoff = min(exit_at, last_entry_at) if last_entry_at else exit_at

    # Entry
    entry_idx: int | None = None
    entry_price = 0.0
    entry_kind: FillKind = "stop"
    for idx, bar in enumerate(bars):
        if bar.timestamp < or_end:
            continue
        if bar.timestamp >= entry_cutoff:
            return None
        if params.entry_mode == "market":
            entry_idx, entry_price, entry_kind = idx, bar.open, "market"
            break
        trigger = candidate.trigger
        if params.entry_mode == "close":
            if (bar.close > trigger) if long else (bar.close < trigger):
                if idx + 1 >= len(bars) or bars[idx + 1].timestamp >= entry_cutoff:
                    return None
                entry_idx, entry_price, entry_kind = idx + 1, bars[idx + 1].open, "market"
                break
            continue
        if long and bar.high >= trigger:
            entry_idx, entry_price = idx, max(trigger, bar.open)
            break
        if not long and bar.low <= trigger:
            entry_idx, entry_price = idx, min(trigger, bar.open)
            break
    if entry_idx is None:
        return None

    tape = _Tape(ticks, candidate.symbol, tick_latency_ms) if ticks is not None else None
    if tape is not None:
        tape.enter(bars[entry_idx], candidate.trigger, entry_kind, long)

    stop = entry_price - sign * risk
    target = entry_price + sign * params.target_r * risk if params.target_r else None
    breakeven_at = (
        entry_price + sign * params.breakeven_r * risk if params.breakeven_r else None
    )
    at_breakeven = False
    vwaps = running_vwap(bars) if params.vwap_exit else []

    # Management
    exit_price: float | None = None
    exit_time = bars[-1].timestamp
    exit_reason: ExitReason = "close"
    exit_kind: FillKind = "market"
    pending_vwap_exit = False
    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        if bar.timestamp >= exit_at:
            exit_price, exit_time, exit_reason = bar.open, bar.timestamp, "close"
            break
        if pending_vwap_exit:
            exit_price, exit_time, exit_reason = bar.open, bar.timestamp, "vwap"
            break

        entered_inside_bar = idx == entry_idx and entry_price != bar.open
        if idx == entry_idx and tape is not None and tape.entry_t is not None:
            # The tape settles the entry bar: a stop or target after the entry, or neither.
            hit = tape.after_entry(stop, target, long)
            if hit == "stop":
                exit_price, exit_time, exit_kind = stop, bar.timestamp, "stop"
                exit_reason = "breakeven" if at_breakeven else "stop"
                break
            if hit == "target" and target is not None:
                exit_price, exit_time, exit_reason, exit_kind = (
                    target, bar.timestamp, "target", "limit"
                )
                break
        elif not entered_inside_bar or worst_case_intrabar:
            stop_hit = bar.low <= stop if long else bar.high >= stop
            target_hit = (
                target is not None
                and not entered_inside_bar
                and (bar.high >= target if long else bar.low <= target)
            )
            if stop_hit:
                gapped = (bar.open < stop) if long else (bar.open > stop)
                exit_price = bar.open if gapped and not entered_inside_bar else stop
                exit_time, exit_kind = bar.timestamp, "stop"
                exit_reason = "breakeven" if at_breakeven else "stop"
                if tape is not None and idx != entry_idx:
                    tape.stop_exit(bar, stop, long)
                break
            if target_hit and target is not None:
                gapped = (bar.open > target) if long else (bar.open < target)
                exit_price = bar.open if gapped else target
                exit_time, exit_reason, exit_kind = bar.timestamp, "target", "limit"
                break

        if breakeven_at is not None and not at_breakeven:
            if (bar.high >= breakeven_at) if long else (bar.low <= breakeven_at):
                stop, at_breakeven = entry_price, True

        if params.vwap_exit:
            vwap = vwaps[idx]
            if vwap is not None:
                crossed = bar.close < vwap if long else bar.close > vwap
                pending_vwap_exit = pending_vwap_exit or crossed

    data_ended = exit_price is None
    if exit_price is None:  # data ended before the flatten time
        exit_price, exit_time = bars[-1].close, bars[-1].timestamp
    if tape is not None and exit_kind == "market" and not data_ended:
        tape.market_exit(exit_time, long)

    entry_side: Side = "buy" if long else "sell"
    exit_side: Side = "sell" if long else "buy"
    tick_entry = tape.entry_fill if tape is not None else None
    tick_exit = tape.exit_fill if tape is not None and exit_kind != "limit" else None
    entry_fill = (
        costs.fill_price(entry_price, entry_side, entry_kind)
        if tick_entry is None
        else _slip(tick_entry, entry_side, costs)
    )
    exit_fill = (
        costs.fill_price(exit_price, exit_side, exit_kind)
        if tick_exit is None
        else _slip(tick_exit, exit_side, costs)
    )
    fees = costs.fees(entry_side, qty, entry_fill) + costs.fees(exit_side, qty, exit_fill)

    fill_source = "bars"
    if tape is not None:
        # A limit fill (profit target) is exact either way, so it doesn't count.
        sources = [tick_entry is not None]
        if exit_kind != "limit":
            sources.append(tick_exit is not None)
        fill_source = "ticks" if all(sources) else "mixed" if any(sources) else "bars"
    return Trade(
        day=day,
        symbol=candidate.symbol,
        direction=candidate.direction,
        relvol=candidate.relvol,
        qty=qty,
        risk_per_share=risk,
        entry_time=bars[entry_idx].timestamp,
        entry_price=entry_price,
        entry_fill=entry_fill,
        stop_price=stop,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_fill=exit_fill,
        exit_reason=exit_reason,
        fees=fees,
        fill_source=fill_source,
        entry_spread=None if tape is None else tape.entry_spread,
        tick_notes=() if tape is None else tuple(tape.notes),
    )


def _slip(price: float, side: Side, costs: CostModel) -> float:
    move = price * costs.slippage_bps / 1e4
    return price + move if side == "buy" else price - move


class _Tape:
    """Tick fills for one trade: remembers the entry minute and collects fallback notes."""

    def __init__(self, ticks: TickSource, symbol: str, latency_ms: int) -> None:
        self.ticks = ticks
        self.symbol = symbol
        self.latency = latency_ms * 1000
        self.entry_t: int | None = None
        self.entry_fill: float | None = None
        self.entry_spread: float | None = None
        self.exit_fill: float | None = None
        self.notes: list[str] = []
        self._minute: MinuteTicks | None = None

    def enter(self, bar: Bar, trigger: float, kind: FillKind, long: bool) -> None:
        minute = self.ticks.minute(self.symbol, bar.timestamp)
        if minute is None:
            self.notes.append("missing_entry")
            return
        start = epoch_us(bar.timestamp)
        if kind == "stop":
            t = elect(minute.trades, minute.quotes, trigger, rising=long, after=start)
            if t is None:
                self.notes.append("entry_not_elected")
                return
        else:  # a market order at the bar's open
            t = start
        fill = market_fill(minute.quotes, t, buy=long, latency_us=self.latency)
        if fill is None:
            self.notes.append("no_entry_quote")
            return
        self.entry_t, self._minute = t, minute
        self.entry_fill, self.entry_spread = fill

    def after_entry(self, stop: float, target: float | None, long: bool) -> str | None:
        """``"stop"`` or ``"target"`` if the tape reaches one after the entry in its minute."""
        assert self._minute is not None and self.entry_t is not None
        trades, quotes = self._minute.trades, self._minute.quotes
        t_stop = elect(trades, quotes, stop, rising=not long, after=self.entry_t)
        t_target = (
            None if target is None
            else elect(trades, quotes, target, rising=long, after=self.entry_t)
        )
        if t_stop is not None and (t_target is None or t_stop <= t_target):
            fill = market_fill(quotes, t_stop, buy=not long, latency_us=self.latency)
            if fill is None:
                self.notes.append("no_stop_quote")
            else:
                self.exit_fill = fill[0]
            return "stop"
        return None if t_target is None else "target"

    def stop_exit(self, bar: Bar, stop: float, long: bool) -> None:
        """A stop that the minute bars say was hit after the entry minute."""
        minute = self.ticks.minute(self.symbol, bar.timestamp)
        if minute is None:
            self.notes.append("missing_stop")
            return
        t = elect(minute.trades, minute.quotes, stop, rising=not long,
                  after=epoch_us(bar.timestamp))
        if t is None:
            # The bar's extreme came from prints that don't elect stops; we still assume the
            # stop filled (conservative) at the bar model's price.
            self.notes.append("stop_not_elected")
            return
        fill = market_fill(minute.quotes, t, buy=not long, latency_us=self.latency)
        if fill is None:
            self.notes.append("no_stop_quote")
        else:
            self.exit_fill = fill[0]

    def market_exit(self, at: datetime, long: bool) -> None:
        quotes = self.ticks.moment(self.symbol, at)
        if quotes is None:
            self.notes.append("missing_exit")
            return
        fill = market_fill(quotes, epoch_us(at), buy=not long, latency_us=self.latency)
        if fill is None:
            self.notes.append("no_exit_quote")
        else:
            self.exit_fill = fill[0]

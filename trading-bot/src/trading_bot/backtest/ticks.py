"""Fills from the tape: when a stop elects and what a market order pays.

The rules follow Alpaca's order handling (``us/docs/orders-at-alpaca.md``):

- A stop "will only elect if there is a trade on the consolidated tape at or above [below] your
  stop price that is not outside of the NBBO". ``elect`` finds that trade: the first one at or
  after a given time, at or through the level, inside the quote in force when it printed, and
  not carrying a condition that keeps it off the regular tape (``EXCLUDED``).
- An elected stop becomes a market order, and market orders fill at the far side of the NBBO.
  ``market_price`` takes the quote in force ``latency`` after the decision: the ask to buy, the
  bid to sell. Quote sizes are ignored (our orders are assumed to fit at the top of the book).

The simulator (``simulator.py``) adds ``CostModel.slippage_bps`` and fees on top. The modelled
spread (``spread_bps``, ``min_spread``) and the extra stop slippage are not used: the quote
already contains them.
"""

from __future__ import annotations

from collections.abc import Sequence

from trading_bot.data.ticks import QuoteTick, TradeTick

EXCLUDED = frozenset("4679ABCDGHLMNPQRTUVWZ")
"""Trade conditions that don't elect stops: derivatively priced (4), closing prints (6), QCT
(7), corrected closes (9), acquisition/bunched/cash/distribution (A, B, C, D, G), price
variation (H), late or out of sequence (L, Z), official open/close summaries (M, Q), next day
(N), prior reference price (P), seller (R), extended hours (T, U), contingent (V), average price
(W). Both tapes' meanings are listed by ``/v2/stocks/meta/conditions/trade``. Regular sales,
odd lots (I), ISOs (F), opening prints (O) and the rest are kept; the NBBO check removes stray
prints."""


def valid(q: QuoteTick) -> bool:
    return q.bid > 0 and q.ask > 0 and q.ask >= q.bid


def quote_at(quotes: Sequence[QuoteTick], t: int) -> QuoteTick | None:
    """The last valid quote at or before ``t`` (epoch µs), or ``None``."""
    found = None
    for q in quotes:
        if q.t > t:
            break
        if valid(q):
            found = q
    return found


def elect(
    trades: Sequence[TradeTick],
    quotes: Sequence[QuoteTick],
    level: float,
    *,
    rising: bool,
    after: int,
) -> int | None:
    """Time (epoch µs) of the first eligible trade at or after ``after`` that reaches ``level``.

    ``rising``: a buy stop (price >= level); otherwise a sell stop (price <= level). A trade
    outside the quote in force is skipped; with no quote yet, the trade counts.
    """
    qi = 0
    current: QuoteTick | None = None
    for tr in trades:
        while qi < len(quotes) and quotes[qi].t <= tr.t:
            if valid(quotes[qi]):
                current = quotes[qi]
            qi += 1
        if tr.t < after:
            continue
        if any(c in EXCLUDED for c in tr.conditions):
            continue
        if not (tr.price >= level if rising else tr.price <= level):
            continue
        if current is not None and not current.bid - 1e-9 <= tr.price <= current.ask + 1e-9:
            continue
        return tr.t
    return None


def market_fill(
    quotes: Sequence[QuoteTick], t: int, *, buy: bool, latency_us: int
) -> tuple[float, float] | None:
    """``(price, spread)`` for a market order decided at ``t`` (epoch µs): the far side of the
    quote in force ``latency_us`` later. ``None`` if no valid quote is known by then."""
    q = quote_at(quotes, t + latency_us)
    if q is None:
        return None
    return (q.ask if buy else q.bid), q.ask - q.bid

"""Transaction cost model.

Whether ORB survives depends on cost per trade measured in R, so costs are explicit and reported
separately (see docs/research/orb.md, "Where the consensus breaks: costs").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]
FillKind = Literal["market", "stop", "limit"]


@dataclass(frozen=True)
class CostModel:
    """Costs applied to every simulated fill.

    - Market and stop fills cross half the spread and pay ``slippage_bps``. Stop-triggered fills
      pay ``stop_slippage_bps`` on top, because they fire while price is moving fast.
    - Limit fills (profit targets) fill at their price.
    - Fees: optional per-share commission (Alpaca charges none), plus SEC and FINRA TAF fees on
      sells. Fee rates change yearly; the defaults are approximate.
    """

    spread_bps: float = 4.0
    """Full quoted spread as basis points of price."""
    min_spread: float = 0.01
    """Floor on the full spread in dollars (one tick)."""
    slippage_bps: float = 1.0
    stop_slippage_bps: float = 2.0
    commission_per_share: float = 0.0
    sec_fee_rate: float = 0.0000278
    """SEC Section 31 fee as a fraction of sell notional."""
    taf_per_share: float = 0.000166
    """FINRA Trading Activity Fee per share sold."""
    taf_max: float = 8.30

    @classmethod
    def zero(cls) -> CostModel:
        """No costs at all. Matches the papers' no-slippage assumption."""
        return cls(
            spread_bps=0.0,
            min_spread=0.0,
            slippage_bps=0.0,
            stop_slippage_bps=0.0,
            commission_per_share=0.0,
            sec_fee_rate=0.0,
            taf_per_share=0.0,
            taf_max=0.0,
        )

    def adverse_move(self, price: float, kind: FillKind) -> float:
        """Dollars per share lost against the reference price on this fill."""
        if kind == "limit":
            return 0.0
        half_spread = max(self.min_spread, price * self.spread_bps / 1e4) / 2
        slip_bps = self.slippage_bps + (self.stop_slippage_bps if kind == "stop" else 0.0)
        return half_spread + price * slip_bps / 1e4

    def fill_price(self, price: float, side: Side, kind: FillKind) -> float:
        move = self.adverse_move(price, kind)
        return price + move if side == "buy" else price - move

    def fees(self, side: Side, qty: int, price: float) -> float:
        total = self.commission_per_share * qty
        if side == "sell":
            total += self.sec_fee_rate * qty * price
            total += min(self.taf_per_share * qty, self.taf_max)
        return total

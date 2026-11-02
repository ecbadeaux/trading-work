"""Order submission."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from trading_bot.strategies.base import Signal

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.models import Order

log = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, trading: TradingClient, dry_run: bool = False) -> None:
        self.trading = trading
        self.dry_run = dry_run

    def submit(self, signal: Signal) -> Any | None:
        """Submit a market DAY order for ``signal``. Returns the order, or None in dry-run."""
        if self.dry_run:
            log.info("[dry-run] would %s %s %s (%s)",
                     signal.side, signal.qty, signal.symbol, signal.reason)
            return None

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=signal.symbol,
            qty=float(signal.qty),
            side=OrderSide.BUY if signal.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = cast("Order", self.trading.submit_order(request))
        log.info("Submitted order %s: %s %s %s (%s)",
                 order.id, signal.side, signal.qty, signal.symbol, signal.reason)
        return order

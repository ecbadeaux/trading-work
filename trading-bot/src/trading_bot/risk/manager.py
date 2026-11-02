"""Risk manager: every signal must pass ``approve`` before an order is sent."""

from __future__ import annotations

from decimal import Decimal

from trading_bot.strategies.base import Signal


class RiskManager:
    def __init__(self, max_order_qty: Decimal = Decimal("100")) -> None:
        self.max_order_qty = max_order_qty

    def approve(self, signal: Signal) -> bool:
        return Decimal("0") < signal.qty <= self.max_order_qty

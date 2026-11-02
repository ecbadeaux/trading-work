"""Strategy interface and the Signal type strategies emit."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trading_bot.broker import AlpacaClients


@dataclass(frozen=True)
class Signal:
    """A strategy's intent to trade. Turned into an order by the OrderManager."""

    symbol: str
    side: Literal["buy", "sell"]
    qty: Decimal
    reason: str = ""


class Strategy(ABC):
    name: str = "unnamed"

    def on_start(self) -> None:  # noqa: B027 - optional hook, intentionally a no-op
        """Called once after connecting to Alpaca."""

    @abstractmethod
    def on_tick(self, clients: AlpacaClients) -> Iterable[Signal]:
        """Called every main-loop iteration. Return zero or more signals."""

    def on_stop(self) -> None:  # noqa: B027 - optional hook, intentionally a no-op
        """Called once on shutdown."""

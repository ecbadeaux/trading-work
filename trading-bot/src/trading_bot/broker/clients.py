"""Factory for the alpaca-py clients used by the bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from trading_bot.config import Settings

if TYPE_CHECKING:
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.trading.client import TradingClient


@dataclass
class AlpacaClients:
    trading: TradingClient
    stock_data: StockHistoricalDataClient
    crypto_data: CryptoHistoricalDataClient


def create_clients(settings: Settings) -> AlpacaClients:
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    return AlpacaClients(
        trading=TradingClient(settings.api_key, settings.secret_key, paper=settings.paper),
        stock_data=StockHistoricalDataClient(settings.api_key, settings.secret_key),
        crypto_data=CryptoHistoricalDataClient(settings.api_key, settings.secret_key),
    )

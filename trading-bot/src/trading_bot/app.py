"""Application: owns the main loop and wires the components together."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, cast

from trading_bot.broker import AlpacaClients, create_clients
from trading_bot.config import Settings
from trading_bot.execution import OrderManager
from trading_bot.risk import RiskManager
from trading_bot.strategies import Strategy

if TYPE_CHECKING:
    from alpaca.trading.models import Clock, TradeAccount

log = logging.getLogger(__name__)


class Application:
    def __init__(
        self,
        settings: Settings,
        clients: AlpacaClients | None = None,
        strategies: list[Strategy] | None = None,
    ) -> None:
        self.settings = settings
        self._stop = threading.Event()
        self._clients = clients
        self.strategies: list[Strategy] = strategies or []
        self.risk: RiskManager | None = None
        self.orders: OrderManager | None = None

    @property
    def clients(self) -> AlpacaClients:
        if self._clients is None:
            self._clients = create_clients(self.settings)
        return self._clients

    def startup(self) -> None:
        account = cast("TradeAccount", self.clients.trading.get_account())
        log.info(
            "Connected to Alpaca account %s (status=%s, equity=%s, buying_power=%s)",
            account.account_number,
            account.status,
            account.equity,
            account.buying_power,
        )
        self.risk = RiskManager()
        self.orders = OrderManager(self.clients.trading, dry_run=self.settings.dry_run)
        for strategy in self.strategies:
            strategy.on_start()

    def run(self, once: bool = False) -> None:
        self.startup()
        while not self._stop.is_set():
            self.tick()
            if once:
                break
            self._stop.wait(self.settings.loop_interval)

    def tick(self) -> None:
        """One iteration of the main loop."""
        clock = cast("Clock", self.clients.trading.get_clock())
        log.info("Market open=%s next_open=%s next_close=%s",
                 clock.is_open, clock.next_open, clock.next_close)

        assert self.risk is not None and self.orders is not None
        for strategy in self.strategies:
            try:
                for signal in strategy.on_tick(self.clients):
                    if self.risk.approve(signal):
                        self.orders.submit(signal)
                    else:
                        log.warning("Risk manager rejected %s", signal)
            except Exception:
                log.exception("Strategy %s failed during tick", strategy.name)

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        for strategy in self.strategies:
            try:
                strategy.on_stop()
            except Exception:
                log.exception("Strategy %s failed during shutdown", strategy.name)

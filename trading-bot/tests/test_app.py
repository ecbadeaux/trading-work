from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from trading_bot.app import Application
from trading_bot.config import Settings
from trading_bot.strategies import Signal, Strategy


class FakeStrategy(Strategy):
    name = "fake"

    def __init__(self, signals: list[Signal]) -> None:
        self.signals = signals
        self.started = self.stopped = False

    def on_start(self) -> None:
        self.started = True

    def on_tick(self, clients):  # type: ignore[no-untyped-def]
        return self.signals

    def on_stop(self) -> None:
        self.stopped = True


def make_clients() -> SimpleNamespace:
    trading = MagicMock()
    trading.get_clock.return_value = SimpleNamespace(is_open=True, next_open=None, next_close=None)
    return SimpleNamespace(trading=trading, stock_data=MagicMock(), crypto_data=MagicMock())


def test_run_once_dry_run_does_not_submit() -> None:
    clients = make_clients()
    strategy = FakeStrategy([Signal("AAPL", "buy", Decimal("1"), "test")])
    app = Application(
        Settings(api_key="k", secret_key="s", dry_run=True), clients=clients, strategies=[strategy]
    )

    app.run(once=True)
    app.shutdown()

    assert strategy.started and strategy.stopped
    clients.trading.get_account.assert_called_once()
    clients.trading.submit_order.assert_not_called()


def test_risk_rejects_oversized_signal() -> None:
    clients = make_clients()
    strategy = FakeStrategy([Signal("AAPL", "buy", Decimal("1000000"))])
    app = Application(Settings(api_key="k", secret_key="s"), clients=clients, strategies=[strategy])

    app.run(once=True)

    clients.trading.submit_order.assert_not_called()

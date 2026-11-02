from __future__ import annotations

import pytest

_ENV_VARS = [
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "ALPACA_PAPER",
    "TRADING_BOT_ALLOW_LIVE",
    "TRADING_BOT_DRY_RUN",
    "TRADING_BOT_LOOP_INTERVAL",
    "TRADING_BOT_LOG_LEVEL",
    "TRADING_BOT_LOG_DIR",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Isolate tests from the developer's real env vars and .env file."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

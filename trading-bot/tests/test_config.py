from __future__ import annotations

import pytest

from trading_bot.config import ConfigError, load_settings


def test_missing_keys_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings()


def test_defaults_to_paper(keys: None) -> None:
    settings = load_settings()
    assert settings.paper is True
    assert settings.dry_run is False
    assert settings.loop_interval == 60.0


def test_live_requires_explicit_allow(keys: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("TRADING_BOT_ALLOW_LIVE", "yes")
    assert load_settings().paper is False


def test_invalid_interval(keys: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_BOT_LOOP_INTERVAL", "0")
    with pytest.raises(ConfigError):
        load_settings()


def test_repr_hides_secrets(keys: None) -> None:
    assert "test-secret" not in repr(load_settings())

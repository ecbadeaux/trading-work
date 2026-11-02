"""Settings loaded from environment variables (optionally via a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    paper: bool = True
    dry_run: bool = False
    loop_interval: float = 60.0
    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    def with_overrides(self, **changes: Any) -> Settings:
        return replace(self, **changes)

    def __repr__(self) -> str:  # never leak secrets into logs
        return (
            f"Settings(paper={self.paper}, dry_run={self.dry_run}, "
            f"loop_interval={self.loop_interval}, log_level={self.log_level!r}, "
            f"log_dir={str(self.log_dir)!r}, api_key='***', secret_key='***')"
        )


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (got {raw!r})")


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """Build Settings from the environment.

    If ``env_file`` is given it must exist; otherwise ``./.env`` is loaded when present.
    Real environment variables take precedence over values in the file.
    """
    from dotenv import load_dotenv

    if env_file is not None:
        if not Path(env_file).is_file():
            raise ConfigError(f"env file not found: {env_file}")
        load_dotenv(env_file, override=False)
    elif Path(".env").is_file():
        load_dotenv(".env", override=False)

    api_key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise ConfigError("APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set")

    paper = _parse_bool("ALPACA_PAPER", True)
    if not paper and not _parse_bool("TRADING_BOT_ALLOW_LIVE", False):
        raise ConfigError(
            "ALPACA_PAPER=false requires TRADING_BOT_ALLOW_LIVE=yes to trade real money"
        )

    try:
        loop_interval = float(os.environ.get("TRADING_BOT_LOOP_INTERVAL", "60"))
    except ValueError as exc:
        raise ConfigError("TRADING_BOT_LOOP_INTERVAL must be a number") from exc
    if loop_interval <= 0:
        raise ConfigError("TRADING_BOT_LOOP_INTERVAL must be > 0")

    log_level = os.environ.get("TRADING_BOT_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"TRADING_BOT_LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}")

    return Settings(
        api_key=api_key,
        secret_key=secret_key,
        paper=paper,
        dry_run=_parse_bool("TRADING_BOT_DRY_RUN", False),
        loop_interval=loop_interval,
        log_level=log_level,
        log_dir=Path(os.environ.get("TRADING_BOT_LOG_DIR", "logs")),
    )

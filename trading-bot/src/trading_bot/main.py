"""Process entry point: parse CLI args, load config, set up logging, run the app."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence
from types import FrameType

from trading_bot import __version__
from trading_bot.app import Application
from trading_bot.config import ConfigError, Settings, load_settings
from trading_bot.logging_setup import setup_logging

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="trading-bot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--once", action="store_true", help="Run a single loop iteration, then exit."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run strategies but never submit orders.",
    )
    parser.add_argument(
        "--log-level", default=None, help="Override TRADING_BOT_LOG_LEVEL (e.g. DEBUG)."
    )
    parser.add_argument(
        "--env-file", default=None, help="Path to a .env file (default: ./.env if present)."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        settings: Settings = load_settings(env_file=args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        settings = settings.with_overrides(log_level=args.log_level.upper())
    if args.dry_run:
        settings = settings.with_overrides(dry_run=True)

    setup_logging(settings.log_level, settings.log_dir)
    log.info(
        "trading-bot %s starting (mode=%s, dry_run=%s)",
        __version__,
        "PAPER" if settings.paper else "LIVE",
        settings.dry_run,
    )

    app = Application(settings)

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("Received %s, shutting down…", signal.Signals(signum).name)
        app.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        app.run(once=args.once)
    except Exception:
        log.exception("Fatal error in main loop")
        return 1
    finally:
        app.shutdown()

    log.info("trading-bot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

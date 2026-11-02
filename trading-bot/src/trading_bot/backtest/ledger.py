"""Run ledger: one JSON line per backtested parameter set, so we know how many we have tried.

Multiple-testing corrections (the Deflated Sharpe Ratio, Harvey & Liu's haircut) need the number
of strategies tried, and that number is easy to understate. ``trading-bot-backtest`` and
``trading-bot-walkforward`` append to the ledger automatically; ``trading-bot-ledger`` prints a
summary and can backfill runs made before the ledger existed.

A *trial* is a distinct strategy key: the strategy parameters, costs and fill assumption. The
period, account size and placebo runs (``direction_mode`` other than ``opening``) don't make a
new trial. Analyses done outside these tools (ad hoc scripts) are not recorded, so the count is
a lower bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.backtest.costs import CostModel
from trading_bot.strategies.orb import OrbParams

log = logging.getLogger(__name__)

DEFAULT_LEDGER = Path("backtests/ledger.jsonl")


def strategy_key(
    params: OrbParams,
    costs: CostModel,
    worst_case_intrabar: bool,
    fills: str = "bars",
    tick_latency_ms: int | None = None,
) -> str:
    """Short stable hash of everything that defines a trial. Tick fills (``fills="ticks"``) are
    a different fill assumption, so they make a different key; bar fills keep the old keys."""
    content: dict[str, Any] = {
        "params": asdict(params),
        "costs": asdict(costs),
        "worst_case_intrabar": bool(worst_case_intrabar),
    }
    if fills != "bars":
        content["fills"] = fills
        content["tick_latency_ms"] = tick_latency_ms
    payload = json.dumps(content, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def make_record(
    *,
    tool: str,
    params: OrbParams,
    costs: CostModel,
    worst_case_intrabar: bool,
    start: str | None,
    end: str | None,
    trades: int,
    avg_net_r: float | None,
    label: str | None = None,
    out: str | None = None,
    fills: str = "bars",
    tick_latency_ms: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    ticks = {} if fills == "bars" else {"fills": fills, "tick_latency_ms": tick_latency_ms}
    return {
        "time": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "key": strategy_key(params, costs, worst_case_intrabar, fills, tick_latency_ms),
        "label": label,
        "start": start,
        "end": end,
        "trades": trades,
        "avg_net_r": avg_net_r,
        "out": out,
        "params": asdict(params),
        "costs": asdict(costs),
        "worst_case_intrabar": worst_case_intrabar,
        **ticks,
        **extra,
    }


def append(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    rows = [json.dumps(r, sort_keys=True) for r in records]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write("\n".join(rows) + "\n")
    return len(rows)


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("Skipping unreadable ledger line %d in %s", n, path)
    return out


def is_trial(record: Mapping[str, Any]) -> bool:
    params = record.get("params") or {}
    return bool(params.get("direction_mode", "opening") == "opening")


def trial_keys(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {r["key"] for r in records if is_trial(r) and "key" in r}


def backfill(root: Path, ledger: Path) -> int:
    """Add a record for every ``summary.json`` under ``root`` not already in the ledger."""
    known = {r.get("out") for r in load(ledger)}
    records = []
    for summary_path in sorted(root.rglob("summary.json")):
        out = str(summary_path.parent)
        if out in known:
            continue
        try:
            payload = json.loads(summary_path.read_text())
            params = OrbParams(**_known(OrbParams, payload["params"]))
            costs = CostModel(**_known(CostModel, payload["costs"]))
            config = payload["config"]
            summary = payload["summary"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Skipping %s: %s", summary_path, exc)
            continue
        # Old runs predate optional fields; constructing the objects fills today's defaults, so
        # they hash like new runs with the same settings.
        records.append(
            make_record(
                tool="backfill",
                params=params,
                costs=costs,
                worst_case_intrabar=bool(config.get("worst_case_intrabar", True)),
                start=config.get("start"),
                end=config.get("end"),
                trades=int(summary.get("trades") or 0),
                avg_net_r=summary.get("avg_net_r"),
                out=out,
                fills=str(config.get("fills", "bars")),
                tick_latency_ms=config.get("tick_latency_ms"),
            )
        )
    return append(ledger, records)


def _known(cls: type[Any], values: Mapping[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in values.items() if k in names}


def describe(records: Sequence[Mapping[str, Any]]) -> str:
    trials = trial_keys(records)
    tools = Counter(str(r.get("tool")) for r in records)
    by_tool = ", ".join(f"{k} {v}" for k, v in sorted(tools.items()))
    lines = [
        f"Runs recorded     {len(records)} ({by_tool})",
        f"Distinct trials   {len(trials)} (strategy parameters x costs x fill assumption; "
        "placebos excluded)",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="trading-bot-ledger",
        description="Summarize the backtest run ledger (how many strategies have been tried).",
    )
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    p.add_argument(
        "--backfill",
        type=Path,
        metavar="DIR",
        help="First add every summary.json under DIR that the ledger lacks (e.g. backtests)",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if args.backfill is not None:
        if not args.backfill.is_dir():
            print(f"Not a directory: {args.backfill}", file=sys.stderr)
            return 2
        added = backfill(args.backfill, args.ledger)
        print(f"Backfilled {added} runs from {args.backfill}")
    records = load(args.ledger)
    print(f"Ledger            {args.ledger}")
    print(describe(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

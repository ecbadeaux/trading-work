"""Write backtest outputs to disk and format a console summary."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trading_bot.backtest.engine import BacktestResult
from trading_bot.backtest.metrics import Summary
from trading_bot.backtest.simulator import Trade

TRADE_COLUMNS = [
    "day",
    "symbol",
    "direction",
    "relvol",
    "qty",
    "risk_per_share",
    "entry_time",
    "entry_price",
    "entry_fill",
    "stop_price",
    "exit_time",
    "exit_price",
    "exit_fill",
    "exit_reason",
    "fees",
    "gross_pnl",
    "net_pnl",
    "gross_r",
    "net_r",
    "cost_r",
    "fill_source",
    "entry_spread_bps",
]


def trade_row(t: Trade) -> list[str]:
    """One ``trades.csv`` row (``TRADE_COLUMNS`` order)."""
    return [
        t.day.isoformat(),
        t.symbol,
        t.direction,
        f"{t.relvol:.4f}",
        str(t.qty),
        f"{t.risk_per_share:.4f}",
        t.entry_time.isoformat(),
        f"{t.entry_price:.4f}",
        f"{t.entry_fill:.4f}",
        f"{t.stop_price:.4f}",
        t.exit_time.isoformat(),
        f"{t.exit_price:.4f}",
        f"{t.exit_fill:.4f}",
        t.exit_reason,
        f"{t.fees:.4f}",
        f"{t.gross_pnl:.2f}",
        f"{t.net_pnl:.2f}",
        f"{t.gross_r:.4f}",
        f"{t.net_r:.4f}",
        f"{t.cost_r:.4f}",
        t.fill_source,
        "" if t.entry_spread is None else f"{1e4 * t.entry_spread / t.entry_price:.2f}",
    ]


def fill_stats(trades: Sequence[Trade]) -> dict[str, Any]:
    """How trades were filled: counts by ``fill_source``, fallback reasons, and the quoted
    spread at entry (bps of price) where the tape priced it."""
    sources = Counter(t.fill_source for t in trades)
    notes = Counter(n for t in trades for n in t.tick_notes)
    spreads = sorted(
        1e4 * t.entry_spread / t.entry_price for t in trades if t.entry_spread is not None
    )

    def pct(q: float) -> float | None:
        return spreads[min(len(spreads) - 1, int(q * len(spreads)))] if spreads else None

    return {
        "trades": len(trades),
        "fill_sources": dict(sources),
        "tick_fallbacks": dict(notes),
        "entry_spread_bps": {"median": pct(0.5), "p75": pct(0.75), "p90": pct(0.9)},
    }


def format_fill_stats(stats: Mapping[str, Any]) -> str:
    n = stats["trades"] or 1
    ticks = stats["fill_sources"].get("ticks", 0)
    spread = stats["entry_spread_bps"]
    lines = [f"Fills from the tape: {ticks:,} of {stats['trades']:,} trades ({ticks / n:.0%})"]
    if spread["median"] is not None:
        lines.append(
            f"Quoted spread at entry: median {spread['median']:.1f} bps, "
            f"75th pct {spread['p75']:.1f}, 90th pct {spread['p90']:.1f}"
        )
    if stats["tick_fallbacks"]:
        lines.append(f"Bar-model fallbacks: {stats['tick_fallbacks']}")
    return "\n".join(lines)


def write_outputs(
    result: BacktestResult, summary: Summary, out_dir: Path, extra: dict[str, Any] | None = None
) -> None:
    """Write trades.csv, equity.csv and summary.json (summary, parameters, diagnostics)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "trades.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TRADE_COLUMNS)
        writer.writerows(trade_row(t) for t in result.trades)

    with (out_dir / "equity.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["day", "equity_start", "gross_pnl", "net_pnl", "trades", "equity_end"])
        for d in result.days:
            writer.writerow(
                [
                    d.day.isoformat(),
                    f"{d.equity_start:.2f}",
                    f"{d.gross_pnl:.2f}",
                    f"{d.net_pnl:.2f}",
                    d.trades,
                    f"{d.equity_end:.2f}",
                ]
            )

    payload = {
        "summary": summary.to_dict(),
        "params": asdict(result.params),
        "costs": asdict(result.costs),
        "config": {
            **asdict(result.config),
            "start": result.config.start.isoformat(),
            "end": result.config.end.isoformat(),
        },
        "diagnostics": dict(result.diagnostics),
        **(extra or {}),
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2) + "\n")


COMPARISON_COLUMNS = [
    "label", "trades", "win_rate", "avg_gross_r", "avg_cost_r", "avg_net_r", "total_return",
    "sharpe", "max_drawdown", "final_equity",
]


def write_comparison(
    rows: list[dict[str, Any]], combined: list[tuple[Any, float]], out_dir: Path
) -> None:
    """Write comparison.csv (one row per variant) and combined_equity.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "comparison.csv").open("w", newline="") as f:
        table = csv.DictWriter(f, fieldnames=COMPARISON_COLUMNS, extrasaction="ignore")
        table.writeheader()
        table.writerows(rows)
    with (out_dir / "combined_equity.csv").open("w", newline="") as f:
        curve = csv.writer(f)
        curve.writerow(["day", "equity"])
        curve.writerows([d.isoformat(), f"{e:.2f}"] for d, e in combined)


def format_comparison(rows: list[dict[str, Any]]) -> str:
    """Console table: one line per variant (rows use ``Summary`` field names plus ``label``)."""
    header = (
        f"{'Variant':<16}{'Trades':>7}{'Win':>7}{'Gross R':>9}{'Cost R':>8}{'Net R':>8}"
        f"{'Return':>9}{'Sharpe':>8}{'Max DD':>8}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        trades = "" if r.get("trades") is None else str(r["trades"])
        lines.append(
            f"{r['label']:<16}{trades:>7}{_pct(r.get('win_rate')):>7}"
            f"{_num(r.get('avg_gross_r'), 3):>9}{_num(r.get('avg_cost_r'), 3):>8}"
            f"{_num(r.get('avg_net_r'), 3):>8}{_pct(r.get('total_return')):>9}"
            f"{_num(r.get('sharpe')):>8}{_pct(r.get('max_drawdown')):>8}"
        )
    return "\n".join(lines)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def format_summary(summary: Summary, result: BacktestResult) -> str:
    s = summary
    lines = [
        f"Period            {s.start} .. {s.end} ({s.days} trading days)",
        f"Trades            {s.trades} ({s.long_trades} long / {s.short_trades} short)",
        f"Equity            {s.initial_equity:,.0f} -> {s.final_equity:,.0f}",
        f"Total return      {_pct(s.total_return)}   CAGR {_pct(s.cagr)}",
        f"Volatility        {_pct(s.volatility)}   Sharpe {_num(s.sharpe)}",
        f"Max drawdown      {_pct(s.max_drawdown)}",
        f"Win rate          {_pct(s.win_rate)} of trades, {_pct(s.hit_ratio_days)} of days",
        f"Avg R per trade   gross {_num(s.avg_gross_r, 3)}  cost {_num(s.avg_cost_r, 3)}  "
        f"net {_num(s.avg_net_r, 3)}",
        f"Profit factor     {_num(s.profit_factor)}   best {_num(s.best_r)}R  "
        f"worst {_num(s.worst_r)}R",
    ]
    if s.benchmark_total_return is not None:
        lines.append(
            f"Benchmark         {_pct(s.benchmark_total_return)}   alpha {_pct(s.alpha)}  "
            f"beta {_num(s.beta)}"
        )
    if result.diagnostics:
        diag = ", ".join(f"{k}={v}" for k, v in sorted(result.diagnostics.items()))
        lines.append(f"Diagnostics       {diag}")
    return "\n".join(lines)

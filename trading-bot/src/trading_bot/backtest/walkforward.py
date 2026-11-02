"""``trading-bot-walkforward``: a pre-registered walk-forward test of an ORB parameter grid.

The protocol is a TOML spec (``docs/research/walkforward-orb-v1.toml``): the grid, the years, a
fixed selection rule and the pass criteria, all written down before seeing results.

1. Every grid variant is backtested on every year, offline from the local store. Each year
   starts from the same equity (one ``run_backtests`` pass over the data per year).
2. For each test year the rule picks one variant using earlier years only. That variant's
   results in the test year form the out-of-sample record.
3. Checks on the out-of-sample record: day-clustered t and intervals, costs x N, optimistic
   fills, a random-direction placebo, long/short and market-volatility splits. Checks on the
   grid: the probability of backtest overfitting (PBO) and the Deflated Sharpe Ratio of the
   final pick, deflated by every trial in the ledger.

The final year is a one-shot holdout. It is never computed without ``--final``, and ``--final``
with a different spec than the first one is refused unless ``--reuse-holdout``.

Statistics are in R per trade (worst-case fills, after costs unless stated), so account size
and compounding don't matter.

A spec's ``[fills] mode = "ticks"`` prices every fill from real trades and quotes; the tick
store must hold the windows the grid needs (``trading-bot-ticks SPEC`` downloads them). The
random-direction placebo then runs with bar fills against the pick with bar fills, so its
comparison is like for like. ``universe = "store+delisted"`` adds the delisted stocks the
download found (``data/delisted.py``).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import logging
import math
import multiprocessing
import os
import statistics
import sys
import time
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trading_bot.backtest import ledger
from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    eligibility_key,
    history_start,
    run_backtests,
)
from trading_bot.backtest.report import TRADE_COLUMNS, trade_row
from trading_bot.backtest.stats import (
    Agg,
    Book,
    DayAgg,
    PboResult,
    RatioStats,
    book_from_trades,
    bootstrap_ratio_ci,
    clusters,
    daily_series,
    deflated_sharpe,
    moments,
    pbo_cscv,
    pooled,
    probabilistic_sharpe,
    ratio_stats,
    sharpe,
    split_blocks,
)
from trading_bot.data.store import UNIVERSES
from trading_bot.logging_setup import setup_logging
from trading_bot.strategies.orb import OrbParams

log = logging.getLogger(__name__)

CACHE_VERSION = 1
DEFAULT_TICKS = Path("data-store/ticks.sqlite")
BENCHMARK = "SPY"
VOL_WINDOW = 20
TRADING_DAYS = 252


class SpecError(ValueError):
    """The walk-forward spec is invalid."""


class StoreIncomplete(RuntimeError):
    """The store lacks data a year's backtest needs."""


class HoldoutUsed(RuntimeError):
    """The final holdout was already evaluated under another spec."""


# -- spec ------------------------------------------------------------------------------------

_PARAM_NAMES = {f.name for f in fields(OrbParams)}
_PORTFOLIO_NAMES = {"initial_equity", "risk_per_trade", "max_leverage", "worst_case_intrabar"}


@dataclass(frozen=True)
class Criteria:
    """Pass bars, all applied to the out-of-sample record unless noted."""

    min_t: float = 3.0
    """Day-clustered t-statistic of mean net R per trade."""
    min_positive_years: float = 0.7
    """Share of test years with positive mean net R."""
    max_pbo: float = 0.25
    """Probability of backtest overfitting of the selection rule over the grid."""
    min_dsr: float = 0.95
    """Deflated Sharpe Ratio of the final pick, given every trial in the ledger."""
    cost_multiplier: float = 2.0
    """Mean net R must stay positive with every cost multiplied by this."""
    final_min_t: float = 0.0
    """The holdout year's day-clustered t must exceed this (with ``--final``)."""


@dataclass(frozen=True)
class Axis:
    name: str
    labels: tuple[str, ...]
    settings: tuple[Mapping[str, Any], ...]
    """``OrbParams`` overrides for each value."""
    ordered: bool
    """Numeric axes have neighbours (used by ``score = "neighborhood"``)."""


@dataclass(frozen=True)
class Variant:
    index: int
    id: str
    label: str
    coords: tuple[int, ...]
    params: OrbParams


@dataclass(frozen=True)
class Spec:
    name: str
    sha: str
    """First 12 hex digits of the spec file's SHA-256."""
    notes: tuple[str, ...]
    """Caveats specific to this protocol, printed in the report."""
    universe: str
    first_year: int
    first_test_year: int
    last_test_year: int
    final_year: int | None
    window: str
    rolling_years: int
    min_trades: int
    score: str
    criteria: Criteria
    pbo_blocks: int
    bootstrap: int
    placebo_seeds: int
    seed: int
    costs: CostModel
    portfolio: Mapping[str, Any]
    fills: str
    """``bars`` (the cost model on minute bars) or ``ticks`` (real trades and quotes)."""
    tick_latency_ms: int
    axes: tuple[Axis, ...]
    variants: tuple[Variant, ...]
    neighbors: tuple[tuple[int, ...], ...]
    """For each variant index, its neighbours' indexes along ordered axes."""

    @property
    def test_years(self) -> list[int]:
        return list(range(self.first_test_year, self.last_test_year + 1))

    @property
    def dev_years(self) -> list[int]:
        return list(range(self.first_year, self.last_test_year + 1))

    def train_years(self, test_year: int) -> list[int]:
        start = self.first_year
        if self.window == "rolling":
            start = max(self.first_year, test_year - self.rolling_years)
        return list(range(start, min(test_year, self.last_test_year + 1)))

    def config(self, start: date, end: date, *, worst_case: bool | None = None) -> BacktestConfig:
        p = dict(self.portfolio)
        default_worst = bool(p.pop("worst_case_intrabar", True))
        return BacktestConfig(
            start=start,
            end=end,
            worst_case_intrabar=default_worst if worst_case is None else worst_case,
            seed=self.seed,
            fills="ticks" if self.fills == "ticks" else "bars",
            tick_latency_ms=self.tick_latency_ms,
            **p,
        )


def load_spec(path: Path) -> Spec:
    raw_bytes = path.read_bytes()
    try:
        raw = tomllib.loads(raw_bytes.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SpecError(f"{path}: {exc}") from exc
    _only(raw, {"name", "notes", "universe", "years", "selection", "criteria", "checks",
                "params", "costs", "portfolio", "fills", "grid"}, "the spec")

    name = raw.get("name")
    if not isinstance(name, str) or not name or "/" in name:
        raise SpecError("name must be a non-empty string without '/'")
    notes = raw.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise SpecError("notes must be a list of strings")
    universe = raw.get("universe", "store")
    if universe not in UNIVERSES:
        raise SpecError(
            "universe must be 'store' (stocks plus extras), 'stocks', or either with '+delisted'"
        )

    years = _table(raw, "years")
    _only(years, {"first", "first_test", "last_test", "final", "window", "rolling"}, "[years]")
    try:
        first, first_test, last_test = (int(years[k]) for k in ("first", "first_test", "last_test"))
    except KeyError as exc:
        raise SpecError(f"[years] needs {exc.args[0]}") from exc
    final = years.get("final")
    window = years.get("window", "anchored")
    rolling = int(years.get("rolling", 0))
    if not first < first_test <= last_test:
        raise SpecError("[years] needs first < first_test <= last_test")
    if final is not None and int(final) <= last_test:
        raise SpecError("[years] final must come after last_test")
    if window not in ("anchored", "rolling"):
        raise SpecError("[years] window must be 'anchored' or 'rolling'")
    if window == "rolling" and rolling < 1:
        raise SpecError("[years] rolling needs rolling = <years of training>")

    selection = _table(raw, "selection")
    _only(selection, {"metric", "min_trades", "score"}, "[selection]")
    if selection.get("metric", "net_r") != "net_r":
        raise SpecError("[selection] metric must be 'net_r' (mean net R per trade)")
    min_trades = int(selection.get("min_trades", 1))
    score = selection.get("score", "own")
    if min_trades < 1 or score not in ("own", "neighborhood"):
        raise SpecError("[selection] needs min_trades >= 1 and score 'own' or 'neighborhood'")

    crit = _table(raw, "criteria")
    _only(crit, {f.name for f in fields(Criteria)}, "[criteria]")
    criteria = Criteria(**{k: float(v) for k, v in crit.items()})

    checks = _table(raw, "checks")
    _only(checks, {"pbo_blocks", "bootstrap", "placebo_seeds", "seed"}, "[checks]")
    pbo_blocks = int(checks.get("pbo_blocks", 16))
    bootstrap = int(checks.get("bootstrap", 2000))
    placebo_seeds = int(checks.get("placebo_seeds", 5))
    if pbo_blocks < 2 or pbo_blocks % 2 or bootstrap < 0 or placebo_seeds < 1:
        raise SpecError(
            "[checks] needs an even pbo_blocks >= 2, bootstrap >= 0 and placebo_seeds >= 1"
        )

    fixed = _coerce(OrbParams, _table(raw, "params"), "[params]")
    cost_values = _coerce(CostModel, _table(raw, "costs"), "[costs]")
    portfolio = _table(raw, "portfolio")
    _only(portfolio, _PORTFOLIO_NAMES, "[portfolio]")
    fill_table = _table(raw, "fills")
    _only(fill_table, {"mode", "latency_ms"}, "[fills]")
    fills = fill_table.get("mode", "bars")
    latency = fill_table.get("latency_ms", 100)
    if fills not in ("bars", "ticks") or not isinstance(latency, int) or latency < 0:
        raise SpecError("[fills] needs mode 'bars' or 'ticks' and an integer latency_ms >= 0")

    axes = _parse_grid(_table(raw, "grid"), fixed)
    variants: list[Variant] = []
    width = len(str(math.prod(len(a.labels) for a in axes)))
    for index, coords in enumerate(itertools.product(*(range(len(a.labels)) for a in axes))):
        settings: dict[str, Any] = dict(fixed)
        for axis, c in zip(axes, coords, strict=True):
            settings.update(axis.settings[c])
        label = " ".join(f"{a.name}={a.labels[c]}" for a, c in zip(axes, coords, strict=True))
        try:
            params = OrbParams(**settings)
        except (TypeError, ValueError) as exc:
            raise SpecError(f"variant {label}: {exc}") from exc
        variants.append(Variant(index, f"v{index + 1:0{width}d}", label, coords, params))

    by_coords = {v.coords: v.index for v in variants}
    neighbors = []
    for v in variants:
        near = []
        for a, axis in enumerate(axes):
            if not axis.ordered:
                continue
            for step in (-1, 1):
                moved = list(v.coords)
                moved[a] += step
                if 0 <= moved[a] < len(axis.labels):
                    near.append(by_coords[tuple(moved)])
        neighbors.append(tuple(near))

    spec = Spec(
        name=name,
        sha=hashlib.sha256(raw_bytes).hexdigest()[:12],
        notes=tuple(notes),
        universe=universe,
        first_year=first,
        first_test_year=first_test,
        last_test_year=last_test,
        final_year=None if final is None else int(final),
        window=window,
        rolling_years=rolling,
        min_trades=min_trades,
        score=score,
        criteria=criteria,
        pbo_blocks=pbo_blocks,
        bootstrap=bootstrap,
        placebo_seeds=placebo_seeds,
        seed=int(checks.get("seed", 0)),
        costs=CostModel(**cost_values),
        portfolio=portfolio,
        fills=fills,
        tick_latency_ms=latency,
        axes=tuple(axes),
        variants=tuple(variants),
        neighbors=tuple(neighbors),
    )
    try:
        spec.config(date(2000, 1, 3), date(2000, 1, 3))
    except (TypeError, ValueError) as exc:
        raise SpecError(f"[portfolio]: {exc}") from exc
    return spec


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise SpecError(f"[{key}] must be a table")
    return value


def _coerce(cls: type[Any], values: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Check keys against ``cls`` fields and turn TOML ints into floats for float fields, so
    ``min_relvol = 2`` and ``--min-relvol 2`` describe (and hash as) the same strategy."""
    types = {f.name: str(f.type) for f in fields(cls)}
    _only(values, set(types), where)
    return {
        k: float(v) if "float" in types[k] and isinstance(v, int) and not isinstance(v, bool)
        else v
        for k, v in values.items()
    }


def _only(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise SpecError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _parse_grid(grid: Mapping[str, Any], fixed: Mapping[str, Any]) -> list[Axis]:
    if not grid:
        raise SpecError("[grid] needs at least one axis")
    axes: list[Axis] = []
    owner: dict[str, str] = {}
    for name, values in grid.items():
        if not isinstance(values, list) or not values:
            raise SpecError(f"grid axis {name!r} must be a non-empty list")
        if name in _PARAM_NAMES:
            if any(isinstance(v, dict) for v in values):
                raise SpecError(f"grid axis {name!r} takes plain values")
            labels = [_fmt(v) for v in values]
            settings: list[dict[str, Any]] = [
                _coerce(OrbParams, {name: v}, f"grid axis {name!r}") for v in values
            ]
            ordered = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
        else:
            if not all(isinstance(v, dict) and isinstance(v.get("label"), str) for v in values):
                raise SpecError(
                    f"grid axis {name!r} is not a strategy parameter, so each value must be a "
                    "table with a label, e.g. {{ label = \"no-quiet\", min_gap = 0.0066 }}"
                )
            labels = [v["label"] for v in values]
            settings = [
                _coerce(OrbParams, {k: x for k, x in v.items() if k != "label"},
                        f"grid axis {name!r}")
                for v in values
            ]
            ordered = False
        if len(set(labels)) != len(labels):
            raise SpecError(f"grid axis {name!r} has duplicate values")
        for param in {k for s in settings for k in s}:
            if param in fixed:
                raise SpecError(f"{param} is set in both [params] and the grid")
            if param in owner:
                raise SpecError(f"{param} is set by grid axes {owner[param]!r} and {name!r}")
            owner[param] = name
        axes.append(Axis(name, tuple(labels), tuple(settings), ordered))
    return axes


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# -- selection --------------------------------------------------------------------------------


def scores(spec: Spec, nets: Sequence[float], counts: Sequence[int]) -> list[float | None]:
    """Selection score per variant: mean net R per trade over the training data (``None`` with
    fewer than ``min_trades``), averaged with eligible neighbours for ``"neighborhood"``."""
    own = [s / n if n >= spec.min_trades else None for s, n in zip(nets, counts, strict=True)]
    if spec.score == "own":
        return own
    out: list[float | None] = []
    for v in spec.variants:
        mine = own[v.index]
        if mine is None:
            out.append(None)
            continue
        values = [mine, *(x for k in spec.neighbors[v.index] if (x := own[k]) is not None)]
        out.append(statistics.fmean(values))
    return out


def choose(spec: Spec, nets: Sequence[float], counts: Sequence[int]) -> int | None:
    """Index of the variant the rule picks (highest score; ties go to the earlier variant)."""
    best: int | None = None
    best_score = -math.inf
    for k, s in enumerate(scores(spec, nets, counts)):
        if s is not None and s > best_score:
            best, best_score = k, s
    return best


def merged(books: Sequence[Book]) -> Book:
    out: Book = {}
    for book in books:
        for day, agg in book.items():
            out[day] = out[day] + agg if day in out else agg
    return out


@dataclass(frozen=True)
class Step:
    year: int
    train_years: tuple[int, ...]
    variant: Variant | None
    train: Agg
    score: float | None
    eligible: int


def select_step(spec: Spec, books: Mapping[int, Mapping[str, Book]], year: int) -> Step:
    train = spec.train_years(year)
    totals = [Agg.total(pooled(books[y][v.id]) for y in train) for v in spec.variants]
    nets = [a.net_r for a in totals]
    counts = [a.trades for a in totals]
    s = scores(spec, nets, counts)
    k = choose(spec, nets, counts)
    return Step(
        year=year,
        train_years=tuple(train),
        variant=None if k is None else spec.variants[k],
        train=Agg() if k is None else totals[k],
        score=None if k is None else s[k],
        eligible=sum(1 for x in s if x is not None),
    )


# -- year runs (phase 1: the grid; phase 2: checks on the picks) -----------------------------


@dataclass(frozen=True)
class YearTask:
    store: str
    universe: tuple[str, ...]
    year: int
    start: date
    end: date
    ids: tuple[str, ...]
    variants: tuple[OrbParams, ...]
    seeds: tuple[int, ...]
    costs: CostModel
    config: BacktestConfig
    allow_missing: bool = False
    keep_trades: tuple[str, ...] = ()
    """Variant ids whose full trade rows are returned (for oos_trades.csv)."""
    ticks: str | None = None
    """Tick store path, for ``config.fills == "ticks"``."""

    def key(self) -> str:
        payload = {
            "version": CACHE_VERSION,
            "store": self.store,
            "universe": hashlib.sha256("\n".join(self.universe).encode()).hexdigest(),
            "year": self.year,
            "start": self.start,
            "end": self.end,
            "ids": self.ids,
            "variants": [asdict(p) for p in self.variants],
            "seeds": self.seeds,
            "costs": asdict(self.costs),
            "config": asdict(self.config),
            "keep_trades": self.keep_trades,
            "ticks": self.ticks,
        }
        text = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class YearResult:
    year: int
    calendar: list[date]
    books: dict[str, Book]
    diagnostics: dict[str, dict[str, int]]
    trades: dict[str, list[list[str]]]


def run_year(task: YearTask) -> YearResult:
    """Backtest every variant of ``task`` over its year, offline. Runs in worker processes."""
    from trading_bot.data.store import BarStore, StoreSource
    from trading_bot.data.ticks import StoreTicks, TickStore

    if task.config.fills == "ticks" and task.ticks is None:
        raise ValueError("tick fills need a tick store")
    store = BarStore(task.store)
    tick_store = TickStore(task.ticks) if task.config.fills == "ticks" and task.ticks else None
    try:
        source = StoreSource(store)
        tick_source = StoreTicks(tick_store) if tick_store is not None else None
        calendar = [d.day for d in source.trading_days(task.start, task.end)]
        groups: dict[tuple[int, float, float, float], list[int]] = {}
        for k, params in enumerate(task.variants):
            groups.setdefault(eligibility_key(params), []).append(k)
        results: dict[int, BacktestResult] = {}
        for members in groups.values():
            out = run_backtests(
                source,
                task.universe,
                [task.variants[k] for k in members],
                task.costs,
                task.config,
                seeds=[task.seeds[k] for k in members],
                ticks=tick_source,
            )
            results.update(zip(members, out, strict=True))
        if source.misses and not task.allow_missing:
            raise StoreIncomplete(
                f"{task.year}: the store lacks data this backtest needs ({dict(source.misses)}). "
                "Finish the download, or pass --allow-missing to accept biased results."
            )
        if tick_source is not None and tick_source.misses and not task.allow_missing:
            raise StoreIncomplete(
                f"{task.year}: the tick store lacks {sum(tick_source.misses.values()):,} windows "
                f"({dict(tick_source.misses)}). Run trading-bot-ticks with this spec, or pass "
                "--allow-missing (those fills then use the bar model)."
            )
        return YearResult(
            year=task.year,
            calendar=calendar,
            books={vid: book_from_trades(results[k].trades) for k, vid in enumerate(task.ids)},
            diagnostics={vid: dict(results[k].diagnostics) for k, vid in enumerate(task.ids)},
            trades={
                vid: [trade_row(t) for t in results[k].trades]
                for k, vid in enumerate(task.ids)
                if vid in task.keep_trades
            },
        )
    finally:
        store.close()
        if tick_store is not None:
            tick_store.close()


def _dump(result: YearResult) -> dict[str, Any]:
    def side(a: Agg) -> list[float]:
        return [a.trades, a.wins, a.gross_r, a.cost_r]

    return {
        "year": result.year,
        "calendar": [d.isoformat() for d in result.calendar],
        "books": {
            vid: {d.isoformat(): side(x.long) + side(x.short) for d, x in book.items()}
            for vid, book in result.books.items()
        },
        "diagnostics": result.diagnostics,
        "trades": result.trades,
    }


def _undump(payload: Mapping[str, Any]) -> YearResult:
    def book(raw: Mapping[str, Sequence[float]]) -> Book:
        return {
            date.fromisoformat(d): DayAgg(
                Agg(int(v[0]), int(v[1]), v[2], v[3]), Agg(int(v[4]), int(v[5]), v[6], v[7])
            )
            for d, v in raw.items()
        }

    return YearResult(
        year=int(payload["year"]),
        calendar=[date.fromisoformat(d) for d in payload["calendar"]],
        books={vid: book(raw) for vid, raw in payload["books"].items()},
        diagnostics=payload["diagnostics"],
        trades=payload["trades"],
    )


def _quiet_worker() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def run_tasks(
    tasks: Sequence[YearTask], cache_dir: Path | None, workers: int, what: str
) -> list[YearResult]:
    """Run (or load from ``cache_dir``) every task; results in task order."""
    results: list[YearResult | None] = [None] * len(tasks)
    todo: list[int] = []
    for i, task in enumerate(tasks):
        path = None if cache_dir is None else cache_dir / f"{task.year}-{task.key()}.json.gz"
        if path is not None and path.is_file():
            results[i] = _undump(json.loads(gzip.decompress(path.read_bytes())))
        else:
            todo.append(i)
    log.info("%s: %d year runs cached, %d to run", what, len(tasks) - len(todo), len(todo))
    started = time.monotonic()

    def finish(i: int, result: YearResult) -> None:
        results[i] = result
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / f"{tasks[i].year}-{tasks[i].key()}.json.gz"
            path.write_bytes(gzip.compress(json.dumps(_dump(result)).encode()))
        log.info("%s: %d done (%.0f s)", what, tasks[i].year, time.monotonic() - started)

    if workers <= 1 or len(todo) <= 1:
        for i in todo:
            finish(i, run_year(tasks[i]))
    else:
        context = multiprocessing.get_context("spawn")
        pool = ProcessPoolExecutor(
            max_workers=min(workers, len(todo)), mp_context=context, initializer=_quiet_worker
        )
        try:
            futures = {pool.submit(run_year, tasks[i]): i for i in todo}
            for future in as_completed(futures):
                finish(futures[future], future.result())
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
    return [r for r in results if r is not None]


def year_windows(
    years: Sequence[int], calendar_range: tuple[date, date], lookback: int
) -> dict[int, tuple[date, date]]:
    """Each year's test window, clipped to what the store can serve with history."""
    lo, hi = calendar_range
    probe = date(2000, 1, 3)
    earliest = lo + (probe - history_start(probe, lookback))
    out: dict[int, tuple[date, date]] = {}
    for y in years:
        start, end = max(date(y, 1, 1), earliest), min(date(y, 12, 31), hi)
        if start > end:
            raise SpecError(f"the store has no usable data for {y} (it covers {lo}..{hi})")
        out[y] = (start, end)
    return out


# -- evaluation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    value: float | None
    threshold: str
    passed: bool
    percent: bool = False


@dataclass
class YearRow:
    step: Step
    test: Agg
    stats: RatioStats
    optimistic: Agg
    """The pick rerun with optimistic intrabar fills (empty with tick fills)."""
    placebo: Agg
    """The pick rerun with random directions, all seeds pooled (bar fills)."""
    placebo_base: Agg
    """The pick with the placebo's fills: ``test`` with bar fills, a bar rerun with ticks."""


@dataclass
class Outcome:
    spec: Spec
    run_dir: Path
    report: str
    checks: list[Check]
    passed: bool


def realized_vol(closes: Mapping[date, float], window: int = VOL_WINDOW) -> dict[date, float]:
    """Each day's volatility of the ``window`` daily log returns before it (no lookahead)."""
    days = sorted(closes)
    rets = [math.log(closes[b] / closes[a]) for a, b in itertools.pairwise(days)]
    out: dict[date, float] = {}
    for i in range(window + 1, len(days)):
        out[days[i]] = statistics.stdev(rets[i - 1 - window : i - 1]) * math.sqrt(TRADING_DAYS)
    return out


def run_walkforward(
    spec: Spec,
    *,
    store_path: Path,
    out_root: Path,
    workers: int = 1,
    final: bool = False,
    reuse_holdout: bool = False,
    ledger_path: Path | None = ledger.DEFAULT_LEDGER,
    allow_missing: bool = False,
    fresh: bool = False,
    ticks_path: Path = DEFAULT_TICKS,
) -> Outcome:
    from trading_bot.data.store import BarStore

    root = out_root / spec.name
    marker = root / "holdout.json"
    if final:
        if spec.final_year is None:
            raise SpecError("the spec has no final year")
        if marker.is_file() and not reuse_holdout:
            used = json.loads(marker.read_text())
            if used.get("spec_sha") != spec.sha:
                raise HoldoutUsed(
                    f"the {used.get('final_year')} holdout was already evaluated with spec "
                    f"{used.get('spec_sha')} on {used.get('time')}; this spec is {spec.sha}. "
                    "A second look is no longer a holdout (--reuse-holdout to do it anyway)."
                )

    store = BarStore(store_path)
    try:
        cal_range = store.calendar_range()
        saved = store.get_meta("universe") or {}
        universe = tuple(store.universe(spec.universe))
        as_of = saved.get("as_of")
        delisted = (
            {"count": len(set(universe) & set(store.delisted())),
             **(store.get_meta("delisted_info") or {})}
            if spec.universe.endswith("+delisted") else None
        )
        if cal_range is None or not universe:
            raise StoreIncomplete(f"{store_path} has no calendar or universe yet")
        lookback = max(v.params.lookback_days for v in spec.variants)
        years = spec.dev_years + ([spec.final_year] if final and spec.final_year else [])
        windows = year_windows(years, cal_range, lookback)
        bench = store.daily([BENCHMARK], windows[years[0]][0], windows[years[-1]][1])
    finally:
        store.close()

    cache = None if fresh else root / "cache"
    started = datetime.now()
    tick_mode = spec.fills == "ticks"
    ticks = str(ticks_path) if tick_mode else None
    if tick_mode and not ticks_path.is_file():
        raise StoreIncomplete(f"tick store not found: {ticks_path} (run trading-bot-ticks)")
    grid_tasks = [
        YearTask(
            store=str(store_path),
            universe=universe,
            year=y,
            start=windows[y][0],
            end=windows[y][1],
            ids=tuple(v.id for v in spec.variants),
            variants=tuple(v.params for v in spec.variants),
            seeds=tuple(spec.seed for _ in spec.variants),
            costs=spec.costs,
            config=spec.config(windows[y][0], windows[y][1]),
            allow_missing=allow_missing,
            ticks=ticks,
        )
        for y in years
    ]
    by_year = {r.year: r for r in run_tasks(grid_tasks, cache, workers, "grid")}
    books = {y: r.books for y, r in by_year.items()}

    eval_years = spec.test_years + ([spec.final_year] if final and spec.final_year else [])
    steps = {y: select_step(spec, books, y) for y in eval_years}
    final_pick = select_step(spec, books, spec.last_test_year + 1)

    # Phase 2: the picks again with optimistic fills and random-direction placebos. With tick
    # fills the placebos (whose trades have no tick windows) run on bars, against the pick on
    # bars, and there is no optimistic bound (the tape settles the entry minute).
    placebo_seeds = [spec.seed + 1 + k for k in range(spec.placebo_seeds)]
    base_suffix = "~bars" if tick_mode else ""
    check_tasks: list[YearTask] = []
    for y, step in steps.items():
        if step.variant is None:
            continue
        v = step.variant
        random_dir = replace(v.params, direction_mode="random")
        start, end = windows[y]
        bar_config = replace(spec.config(start, end), fills="bars")
        placebo_task = YearTask(
            store=str(store_path), universe=universe, year=y, start=start, end=end,
            ids=(f"{v.id}{base_suffix}", *(f"{v.id}~random{s}" for s in placebo_seeds)),
            variants=(v.params, *(random_dir for _ in placebo_seeds)),
            seeds=(spec.seed, *placebo_seeds),
            costs=spec.costs, config=bar_config, allow_missing=allow_missing,
            keep_trades=() if tick_mode else (v.id,),
        )
        check_tasks.append(placebo_task)
        if tick_mode:
            check_tasks.append(replace(
                placebo_task, ids=(v.id,), variants=(v.params,), seeds=(spec.seed,),
                config=spec.config(start, end), keep_trades=(v.id,), ticks=ticks,
            ))
        else:
            check_tasks.append(replace(
                placebo_task, ids=(f"{v.id}~optimistic",), variants=(v.params,),
                seeds=(spec.seed,), config=spec.config(start, end, worst_case=False),
                keep_trades=(),
            ))
    checked: dict[tuple[int, str], Book] = {}
    oos_trades: list[list[str]] = []
    for result in run_tasks(check_tasks, cache, workers, "checks"):
        checked.update({(result.year, vid): book for vid, book in result.books.items()})
        for vid, trade_rows in result.trades.items():
            oos_trades.extend([*row, str(result.year), vid] for row in trade_rows)

    m = spec.criteria.cost_multiplier
    rows: dict[int, YearRow] = {}
    for y, step in steps.items():
        book = books[y][step.variant.id] if step.variant else {}
        vid = step.variant.id if step.variant else ""
        rows[y] = YearRow(
            step=step,
            test=pooled(book),
            stats=ratio_stats(clusters(book)),
            optimistic=pooled(checked.get((y, f"{vid}~optimistic"), {})),
            placebo=Agg.total(
                pooled(checked.get((y, f"{vid}~random{s}"), {})) for s in placebo_seeds
            ),
            placebo_base=pooled(checked.get((y, f"{vid}{base_suffix}"), {})),
        )

    oos_book = merged([books[y][s.variant.id] for y, s in steps.items()
                       if s.variant and y in spec.test_years])
    oos_calendar = [d for y in spec.test_years for d in by_year[y].calendar]
    dev_calendar = [d for y in spec.dev_years for d in by_year[y].calendar]
    dev_books = [merged([books[y][v.id] for y in spec.dev_years]) for v in spec.variants]

    # Overfitting checks over the grid (development years only).
    blocks = split_blocks(dev_calendar, spec.pbo_blocks)
    block_aggs = [
        [Agg.total(book[d].side() for d in block if d in book) for book in dev_books]
        for block in blocks
    ]
    pbo: PboResult | None = None
    if len(spec.variants) >= 2:
        pbo = pbo_cscv(block_aggs, lambda nets, counts: choose(spec, nets, counts))

    records = ledger.load(ledger_path) if ledger_path else []
    latency = spec.tick_latency_ms if tick_mode else None
    grid_keys = {
        ledger.strategy_key(v.params, spec.costs, _worst(spec), spec.fills, latency)
        for v in spec.variants
    }
    n_trials = len(ledger.trial_keys(records) | grid_keys)
    # Under the null (no trial has skill) a daily Sharpe estimate has variance ~1/(T-1). The
    # grid's own spread of Sharpes would overstate the noise: much of it is real (the 10%-ATR
    # stop variants lose consistently to costs), which would make the bar unreachable.
    var_sr = 1 / (len(dev_calendar) - 1)
    dsr = sr0 = pick_sr = None
    if final_pick.variant is not None:
        series = daily_series(dev_books[final_pick.variant.index], dev_calendar)
        pick_sr = sharpe(series)
        if pick_sr is not None:
            skew, kurt = moments(series)
            dsr, sr0 = deflated_sharpe(pick_sr, len(series), skew, kurt, n_trials, var_sr)

    # Out-of-sample record.
    test_rows = [rows[y] for y in spec.test_years]
    oos = ratio_stats(clusters(oos_book))
    oos_month = ratio_stats(clusters(oos_book, by="month"))
    oos_boot = bootstrap_ratio_ci(clusters(oos_book), spec.bootstrap, spec.seed)
    oos_agg = pooled(oos_book)
    oos_series = daily_series(oos_book, oos_calendar)
    oos_sr = sharpe(oos_series)
    oos_psr = None
    if oos_sr is not None:
        skew, kurt = moments(oos_series)
        oos_psr = probabilistic_sharpe(oos_sr, len(oos_series), skew, kurt)
    placebo_all = Agg.total(r.placebo for r in test_rows)
    optimistic_all = Agg.total(r.optimistic for r in test_rows)
    placebo_mean = placebo_all.mean_net()
    base_mean = Agg.total(r.placebo_base for r in test_rows).mean_net()
    edge = None if base_mean is None or placebo_mean is None else base_mean - placebo_mean
    positive = sum(1 for r in test_rows if (r.test.mean_net() or 0) > 0)

    c = spec.criteria
    checks = [
        Check("Out-of-sample t (day-clustered)", oos.t, f">= {c.min_t:g}",
              oos.t is not None and oos.t >= c.min_t),
        Check("Positive test years", positive / len(test_rows), f">= {c.min_positive_years:.0%}",
              positive / len(test_rows) >= c.min_positive_years, percent=True),
        Check(f"Net R per trade with costs x{m:g}", oos_agg.mean_net(m), "> 0",
              (oos_agg.mean_net(m) or 0) > 0),
        Check("Net R minus random-direction placebo" + (" (bar fills)" if tick_mode else ""),
              edge, "> 0", edge is not None and edge > 0),
        Check("Probability of backtest overfitting", None if pbo is None else pbo.pbo,
              f"<= {c.max_pbo:g}", pbo is not None and pbo.pbo <= c.max_pbo),
        Check("Deflated Sharpe of the final pick", dsr, f">= {c.min_dsr:g}",
              dsr is not None and dsr >= c.min_dsr),
    ]
    final_row = rows.get(spec.final_year) if final and spec.final_year else None
    if final_row is not None:
        checks.append(Check(f"Holdout {spec.final_year} t (day-clustered)", final_row.stats.t,
                            f"> {c.final_min_t:g}",
                            final_row.stats.t is not None and final_row.stats.t > c.final_min_t))
    passed = all(ch.passed for ch in checks)

    # Robustness splits of the out-of-sample record.
    sides = {s: ratio_stats(clusters(oos_book, side=s)) for s in ("long", "short")}
    closes = {b.day: b.adj_close or b.close for b in bench.get(BENCHMARK, [])}
    regimes = _regimes(oos_book, realized_vol(closes)) if closes else {}
    costs_table = {k: oos_agg.mean_net(k) for k in sorted({1.0, 1.5, m})}

    run_dir = root / started.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_csvs(run_dir, spec, rows, books, oos_trades, m)
    report = _render(
        spec=spec, store_path=store_path, as_of=as_of, windows=windows, n_universe=len(universe),
        rows=rows, final_row=final_row, final_pick=final_pick, checks=checks, passed=passed,
        oos=oos, oos_month=oos_month, oos_boot=oos_boot, oos_agg=oos_agg, oos_sr=oos_sr,
        oos_psr=oos_psr, placebo=placebo_all, optimistic=optimistic_all, sides=sides,
        regimes=regimes, costs_table=costs_table, pbo=pbo, dsr=dsr, sr0=sr0, pick_sr=pick_sr,
        n_trials=n_trials, dev_books=dev_books, delisted=delisted,
        fill_summary=_fill_summary(oos_trades) if tick_mode else None,
        tick_fallbacks=_tick_fallbacks(by_year.values()) if tick_mode else None,
    )
    (run_dir / "report.md").write_text(report)
    summary = {
        "spec": spec.name, "spec_sha": spec.sha, "store": str(store_path), "final": final,
        "passed": passed, "checks": [asdict(ch) for ch in checks],
        "oos": asdict(oos), "oos_trades": oos_agg.trades,
        "final_pick": None if final_pick.variant is None else final_pick.variant.label,
        "pbo": None if pbo is None else {k: v for k, v in asdict(pbo).items() if k != "logits"},
        "dsr": dsr, "sr0": sr0, "n_trials": n_trials,
        "selections": {str(y): (r.step.variant.label if r.step.variant else None)
                       for y, r in rows.items()},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    if ledger_path is not None:
        ledger.append(ledger_path, [
            ledger.make_record(
                tool="walkforward", params=v.params, costs=spec.costs,
                worst_case_intrabar=_worst(spec), start=str(dev_calendar[0]),
                end=str(dev_calendar[-1]), trades=pooled(dev_books[v.index]).trades,
                avg_net_r=pooled(dev_books[v.index]).mean_net(), label=v.label,
                out=str(run_dir), fills=spec.fills, tick_latency_ms=latency, spec=spec.name,
                spec_sha=spec.sha, universe=spec.universe,
            )
            for v in spec.variants
        ])
    if final and spec.final_year is not None and not marker.is_file():
        marker.write_text(json.dumps({
            "spec_sha": spec.sha, "final_year": spec.final_year,
            "time": datetime.now().isoformat(timespec="seconds"), "run": str(run_dir),
        }, indent=2) + "\n")
    return Outcome(spec, run_dir, report, checks, passed)


def _worst(spec: Spec) -> bool:
    return bool(spec.portfolio.get("worst_case_intrabar", True))


def _fill_summary(rows: Sequence[Sequence[str]]) -> dict[str, Any]:
    """Tick share and quoted entry spreads (bps) of the out-of-sample trade rows."""
    col = {name: i for i, name in enumerate(TRADE_COLUMNS)}
    sources = [r[col["fill_source"]] for r in rows]
    spreads = sorted(float(r[col["entry_spread_bps"]]) for r in rows if r[col["entry_spread_bps"]])

    def pct(q: float) -> float | None:
        return spreads[min(len(spreads) - 1, int(q * len(spreads)))] if spreads else None

    return {"trades": len(rows), "ticks": sources.count("ticks"),
            "median": pct(0.5), "p75": pct(0.75), "p90": pct(0.9)}


def _tick_fallbacks(results: Iterable[YearResult]) -> dict[str, int]:
    """Fallback counts (``tick_*`` diagnostics) over every variant and year."""
    total: dict[str, int] = {}
    for r in results:
        for diag in r.diagnostics.values():
            for k, v in diag.items():
                if k.startswith("tick_"):
                    total[k[5:]] = total.get(k[5:], 0) + v
    return dict(sorted(total.items()))


def _regimes(book: Book, vol: Mapping[date, float]) -> dict[str, RatioStats]:
    days = sorted(d for d in book if d in vol)
    if len(days) < 3:
        return {}
    levels = sorted(vol[d] for d in days)
    lo, hi = levels[len(levels) // 3], levels[2 * len(levels) // 3]
    bands = {
        f"calm (SPY vol < {lo:.0%})": (-math.inf, lo),
        "middle": (lo, hi),
        f"volatile (>= {hi:.0%})": (hi, math.inf),
    }
    return {
        name: ratio_stats(clusters({d: book[d] for d in days if a <= vol[d] < b}))
        for name, (a, b) in bands.items()
    }


# -- outputs ----------------------------------------------------------------------------------


def _write_csvs(
    run_dir: Path,
    spec: Spec,
    rows: Mapping[int, YearRow],
    books: Mapping[int, Mapping[str, Book]],
    oos_trades: Sequence[Sequence[str]],
    m: float,
) -> None:
    with (run_dir / "selections.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "train_years", "variant", "label", "train_trades", "train_net_r",
                    "score", "eligible", "test_trades", "test_net_r", "test_t",
                    f"test_net_r_costs_x{m:g}", "optimistic_net_r", "placebo_net_r"])
        for y, r in rows.items():
            s = r.step
            w.writerow([
                y, _span(s.train_years),
                s.variant.id if s.variant else "", s.variant.label if s.variant else "",
                s.train.trades, _f(s.train.mean_net()), _f(s.score), s.eligible,
                r.test.trades, _f(r.test.mean_net()), _f(r.stats.t, 2), _f(r.test.mean_net(m)),
                _f(r.optimistic.mean_net()), _f(r.placebo.mean_net()),
            ])
    years = sorted(books)
    with (run_dir / "grid.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "label", *(f"{y}_{k}" for y in years for k in ("trades", "net_r"))])
        for v in spec.variants:
            cells: list[Any] = []
            for y in years:
                a = pooled(books[y][v.id])
                cells += [a.trades, _f(a.mean_net())]
            w.writerow([v.id, v.label, *cells])
    with (run_dir / "oos_trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([*TRADE_COLUMNS, "test_year", "variant"])
        w.writerows(oos_trades)


def _span(years: Sequence[int]) -> str:
    if not years:
        return "-"
    return str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"


def _f(x: float | None, digits: int = 4) -> str:
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def _r(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.3f}"


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _t(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _annual(sr: float | None) -> str:
    return "n/a" if sr is None else f"{sr * math.sqrt(TRADING_DAYS):.2f}"


def _ci(s: RatioStats) -> str:
    return "n/a" if s.low is None or s.high is None else f"{s.low:+.3f} .. {s.high:+.3f}"


def _render(**k: Any) -> str:
    spec: Spec = k["spec"]
    rows: Mapping[int, YearRow] = k["rows"]
    windows: Mapping[int, tuple[date, date]] = k["windows"]
    oos: RatioStats = k["oos"]
    oos_agg: Agg = k["oos_agg"]
    c = spec.criteria
    out: list[str] = []
    add = out.append

    verdict = "PASSED" if k["passed"] else "FAILED"
    add(f"# Walk-forward {spec.name}: {verdict}\n")
    first_day = windows[min(windows)][0]
    last_day = windows[max(windows)][1]
    add(f"Spec `{spec.name}` (sha {spec.sha}), {len(spec.variants)} variants, "
        f"{spec.window} training from {spec.first_year}, selection by mean net R per trade "
        f"({spec.score} score, at least {spec.min_trades} training trades). Store "
        f"`{k['store_path']}`, {first_day}..{last_day}, {k['n_universe']} symbols "
        f"(universe `{spec.universe}`). All R figures are per trade, after costs, "
        + (f"with fills from real trades and quotes ({spec.tick_latency_ms} ms latency)"
           if spec.fills == "ticks" else "worst-case intrabar fills")
        + ", unless stated.\n")

    add("## Pass criteria\n")
    add("| Check | Value | Bar | |")
    add("|---|---|---|---|")
    for ch in k["checks"]:
        value = _p(ch.value) if ch.percent else "n/a" if ch.value is None else f"{ch.value:.3f}"
        add(f"| {ch.name} | {value} | {ch.threshold} | {'pass' if ch.passed else 'FAIL'} |")
    add("")

    add("## Walk-forward selections\n")
    add(f"| Test year | Trained on | Pick | Train net R (trades) | Test trades | Test net R | t "
        f"| Costs x{c.cost_multiplier:g} | Optimistic | Placebo |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for y, r in rows.items():
        s = r.step
        trained = _span(s.train_years)
        picked = f"{s.variant.id} {s.variant.label}" if s.variant else "none (cash)"
        mark = " (holdout)" if y == spec.final_year else ""
        add(f"| {y}{mark} | {trained} | {picked} | {_r(s.train.mean_net())} ({s.train.trades}) "
            f"| {r.test.trades} | {_r(r.test.mean_net())} | {_t(r.stats.t)} "
            f"| {_r(r.test.mean_net(c.cost_multiplier))} "
            f"| {'n/a' if spec.fills == 'ticks' else _r(r.optimistic.mean_net())} "
            f"| {_r(r.placebo.mean_net())} |")
    add("")

    boot = k["oos_boot"]
    add(f"## Out-of-sample record, {_span(spec.test_years)}\n")
    n = oos_agg.trades
    add(f"- Trades: {n:,}; win rate {_p(oos_agg.wins / n if n else None)}; "
        f"gross {_r(oos_agg.gross_r / n if n else None)}, "
        f"cost {_r(oos_agg.cost_r / n if n else None)}, net {_r(oos.mean)} R per trade.")
    add(f"- Day-clustered: t = {_t(oos.t)}, 95% CI {_ci(oos)} ({oos.clusters} days). "
        f"Month-clustered: t = {_t(k['oos_month'].t)}. Day bootstrap 95% CI: "
        f"{'n/a' if boot is None else f'{boot[0]:+.3f} .. {boot[1]:+.3f}'}.")
    add(f"- Daily R Sharpe: {_annual(k['oos_sr'])} annualized; probabilistic Sharpe (vs 0) "
        f"{_t(k['oos_psr'])}.")
    optimistic = "" if spec.fills == "ticks" else (
        f" Optimistic fills: {_r(k['optimistic'].mean_net())}."
    )
    placebo_fills = " on bar fills" if spec.fills == "ticks" else ""
    add("- Costs: " + ", ".join(f"x{m:g} {_r(v)}" for m, v in k["costs_table"].items())
        + f".{optimistic} Random-direction placebo ({spec.placebo_seeds} seeds"
        f"{placebo_fills}): {_r(k['placebo'].mean_net())}.")
    fs = k["fill_summary"]
    if fs is not None:
        spread = ("n/a" if fs["median"] is None else
                  f"median {fs['median']:.1f} bps, 75th pct {fs['p75']:.1f}, 90th {fs['p90']:.1f}")
        add(f"- Fills: {fs['ticks']:,} of {fs['trades']:,} trades fully from the tape; quoted "
            f"spread at entry {spread}.")
    sides = k["sides"]
    add(f"- Long: {_r(sides['long'].mean)} (t {_t(sides['long'].t)}, {sides['long'].trades} "
        f"trades). Short: {_r(sides['short'].mean)} (t {_t(sides['short'].t)}, "
        f"{sides['short'].trades} trades).")
    if k["regimes"]:
        add("- By market volatility (SPY 20-day realized, terciles of days traded): " + "; ".join(
            f"{name} {_r(s.mean)} (t {_t(s.t)})" for name, s in k["regimes"].items()) + ".")
    add("")

    final_row: YearRow | None = k["final_row"]
    if final_row is not None:
        hs = final_row.stats
        add(f"## Holdout {spec.final_year}\n")
        add(f"Pick {final_row.step.variant.label if final_row.step.variant else 'none'}: "
            f"{final_row.test.trades} trades, net {_r(hs.mean)} R per trade, t {_t(hs.t)}, "
            f"95% CI {_ci(hs)}.\n")

    pbo: PboResult | None = k["pbo"]
    add("## Overfitting checks (development years)\n")
    if pbo is not None:
        add(f"- PBO {pbo.pbo:.2f} over {pbo.splits:,} splits of {spec.pbo_blocks} blocks: how "
            "often the rule's in-sample pick lands at or below the median out of sample. The "
            f"pick loses money out of sample in {pbo.prob_oos_loss:.0%} of splits (average "
            f"{_r(pbo.mean_oos)}).")
    pick: Step = k["final_pick"]
    pick_sr = k["pick_sr"]
    sr0 = k["sr0"]
    add(f"- Final pick (trained on {_span(pick.train_years)}): "
        f"{pick.variant.id + ' ' + pick.variant.label if pick.variant else 'none'}. Daily Sharpe "
        f"{_annual(pick_sr)} annualized; expected best of {k['n_trials']} unskilled trials "
        f"{_annual(sr0)} (null Sharpe variance 1/(T-1)); Deflated Sharpe "
        f"{_t(k['dsr'])}. Trials = distinct strategies in the ledger plus this grid (a lower "
        "bound).")
    if pick.variant is not None:
        near = [spec.variants[i] for i in spec.neighbors[pick.variant.index]]
        dev_books: Sequence[Book] = k["dev_books"]
        add("- Plateau around the final pick (development net R per trade): " + "; ".join(
            f"{v.label} {_r(pooled(dev_books[v.index]).mean_net())}"
            for v in [pick.variant, *near]) + ".")
    add("")

    add("## Grid, development years\n")
    ranked = sorted(spec.variants, key=lambda v: -(pooled(k["dev_books"][v.index]).mean_net()
                                                    or -math.inf))
    add("| Variant | Trades | Net R | t |")
    add("|---|---|---|---|")
    for v in ranked:
        book = k["dev_books"][v.index]
        st = ratio_stats(clusters(book))
        add(f"| {v.id} {v.label} | {st.trades:,} | {_r(st.mean)} | {_t(st.t)} |")
    add("")

    add("## Caveats\n")
    dl = k["delisted"]
    if dl is None:
        add(f"- Survivorship bias: the universe is the symbols Alpaca listed on "
            f"{k['as_of'] or '?'}; stocks delisted before then are missing, more so in early "
            "years.")
    else:
        add(f"- Survivorship bias, reduced: {dl['count']:,} delisted stocks were added from "
            f"Alpaca's inactive assets and corporate actions (by year of their last day: "
            f"{dl.get('by_year', {})}). Coverage is thin before 2021 and misses some delistings "
            "(bank failures such as SIVB and FRC).")
    if spec.fills == "ticks":
        fb = k["tick_fallbacks"] or {}
        add(f"- Fills come from SIP trades and quotes: a stop elects on the first eligible trade "
            f"at its price inside the NBBO and fills at the far side of the quote "
            f"{spec.tick_latency_ms} ms later, plus {spec.costs.slippage_bps:g} bps; quote sizes "
            f"are ignored. Fills without ticks use the bar model (all variants and years: "
            f"{fb or 'none'}).")
    else:
        add("- Minute bars can't show whether the stop or the entry came first inside a bar; "
            "worst-case fills assume the stop. Spreads and slippage are modelled, not measured.")
    for note in spec.notes:
        add(f"- {note}")
    return "\n".join(out) + "\n"


# -- CLI --------------------------------------------------------------------------------------


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-bot-walkforward",
        description="Pre-registered walk-forward test of an ORB parameter grid, offline from "
        "the local store (no API keys).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("spec", type=Path, help="Walk-forward spec (TOML)")
    p.add_argument("--store", type=Path, default=Path("data-store/bars.sqlite"))
    p.add_argument("--out", type=Path, default=Path("backtests/walkforward"),
                   help="Output root; runs go to OUT/<spec name>/<timestamp>/")
    p.add_argument("--workers", type=int, default=default_workers(),
                   help="Years backtested in parallel")
    p.add_argument("--final", action="store_true",
                   help="Also evaluate the spec's final holdout year (one shot per spec)")
    p.add_argument("--reuse-holdout", action="store_true",
                   help="Allow --final although another spec already used the holdout")
    p.add_argument("--ledger", type=Path, default=ledger.DEFAULT_LEDGER)
    p.add_argument("--no-ledger", action="store_true", help="Don't read or write the ledger")
    p.add_argument("--allow-missing", action="store_true",
                   help="Run even if the store lacks some data (results will be biased)")
    p.add_argument("--fresh", action="store_true", help="Ignore cached year runs")
    p.add_argument("--ticks", type=Path, default=DEFAULT_TICKS,
                   help="Tick store, for specs with [fills] mode = \"ticks\"")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level.upper(), Path("logs"))
    try:
        spec = load_spec(args.spec)
        if not args.store.is_file():
            raise SpecError(f"store not found: {args.store}")
        outcome = run_walkforward(
            spec,
            store_path=args.store,
            out_root=args.out,
            workers=args.workers,
            final=args.final,
            reuse_holdout=args.reuse_holdout,
            ledger_path=None if args.no_ledger else args.ledger,
            allow_missing=args.allow_missing,
            fresh=args.fresh,
            ticks_path=args.ticks,
        )
    except (OSError, SpecError, HoldoutUsed) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except StoreIncomplete as exc:
        print(f"Store incomplete: {exc}", file=sys.stderr)
        return 1
    print(outcome.report)
    print(f"Wrote {outcome.run_dir}/ (report.md, summary.json, selections.csv, grid.csv, "
          "oos_trades.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

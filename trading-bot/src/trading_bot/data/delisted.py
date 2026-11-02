"""Stocks that stopped trading before today, so backtests don't only see the survivors.

The store's universe is today's listed stocks. A stock that was bought out, went bankrupt or was
delisted earlier is missing from every past day it traded, and those are often exactly the
volatile, news-driven names ORB trades. This module finds such stocks from free Alpaca sources:

1. **Inactive assets** (``/v2/assets?status=inactive``) listed on NYSE, NASDAQ or AMEX that look
   like operating companies (the same fund-name heuristic as ``historical.FUND_NAME``). No dates.
2. **Corporate actions**: the acquired company in cash, stock and stock-and-cash mergers, and
   worthless removals (bankruptcies), with their effective dates. Alpaca's records are sparse
   before 2020.
3. Optionally a CSV with ``symbol,last_day`` rows from another source (e.g. a paid
   survivorship-free list).

Each candidate's trading life comes from the daily bars under its ticker, fetched with
``asof="-"`` (no symbol mapping: whoever traded under that ticker), clipped at its last day when
one is known. A candidate is dropped where its bars are a current symbol's bars:

- **renames**: Alpaca serves a renamed company's whole history under its current symbol, so an
  old name whose last bar equals some current symbol's bar that day is the same company;
- **reused tickers**: days where a current symbol with the same ticker has the identical bar.

Kept stocks are stored under their ticker, or ``TICKER@YYYYMMDD`` (their last day) when a current
symbol uses the same ticker. Known gaps: stocks missing from both sources (bank failures such as
SIVB, FRC and SBNY are) and the early years; ``coverage`` reports counts by year.

Standard library only; no alpaca-py imports.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from trading_bot.data.models import DailyBar

SYMBOL = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")
"""Plain tickers (and share classes such as BRK.B); rules out CUSIPs, warrants, units, rights."""
STOCK_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX"})
MERGER_TYPES = ("cash_mergers", "stock_mergers", "stock_and_cash_mergers")
CA_TYPES = "cash_merger,stock_merger,stock_and_cash_merger,worthless_removal"


@dataclass
class Candidate:
    symbol: str
    sources: set[str] = field(default_factory=set)
    last_day: date | None = None
    """Latest known last trading day (corporate actions, CSV); ``None`` if only inactive."""

    def note(self, source: str, last_day: date | None = None) -> None:
        self.sources.add(source)
        if last_day is not None and (self.last_day is None or last_day > self.last_day):
            self.last_day = last_day


@dataclass(frozen=True)
class Delisted:
    key: str
    """Store key: the ticker, or ``TICKER@YYYYMMDD`` when a current symbol has the ticker."""
    symbol: str
    first: date
    last: date
    sources: tuple[str, ...]

    def to_meta(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "first": self.first.isoformat(),
            "last": self.last.isoformat(),
            "sources": list(self.sources),
        }


def from_inactive_assets(
    assets: Iterable[Mapping[str, Any]], fund_name: re.Pattern[str]
) -> list[str]:
    """Tickers of inactive assets that look like stocks on a major exchange."""
    out = set()
    for a in assets:
        sym = a.get("symbol") or ""
        if a.get("exchange") not in STOCK_EXCHANGES or not SYMBOL.fullmatch(sym):
            continue
        if fund_name.search(a.get("name") or ""):
            continue
        out.add(sym)
    return sorted(out)


def from_corporate_actions(payload: Mapping[str, Any]) -> list[tuple[str, str, date]]:
    """``(source, ticker, last day)`` for each delisting in a ``/v1/corporate-actions`` page."""
    actions = payload.get("corporate_actions") or {}
    out: list[tuple[str, str, date]] = []
    for kind in MERGER_TYPES:
        for row in actions.get(kind) or []:
            sym, when = row.get("acquiree_symbol"), row.get("effective_date") or row.get(
                "process_date"
            )
            if sym and when and SYMBOL.fullmatch(sym):
                out.append(("merger", sym, date.fromisoformat(when)))
    for row in actions.get("worthless_removals") or []:
        sym, when = row.get("symbol"), row.get("process_date")
        if sym and when and SYMBOL.fullmatch(sym):
            out.append(("worthless", sym, date.fromisoformat(when)))
    return out


def fetch_corporate_actions(
    get: Callable[[str, dict[str, Any]], Any], start: date, end: date
) -> list[tuple[str, str, date]]:
    """Every merger and worthless removal between ``start`` and ``end``, a quarter per query.

    ``get`` is ``CorporateActionsClient.get`` (or a fake). One request per page."""
    out: list[tuple[str, str, date]] = []
    for lo, hi in _quarters(start, end):
        params: dict[str, Any] = {
            "types": CA_TYPES, "start": lo.isoformat(), "end": hi.isoformat(), "limit": 1000,
        }
        while True:
            page = get("/corporate-actions", dict(params))
            page = page if isinstance(page, dict) else {}
            out.extend(from_corporate_actions(page))
            token = page.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    return out


def _quarters(start: date, end: date) -> Iterable[tuple[date, date]]:
    year, q = start.year, (start.month - 1) // 3
    while True:
        lo = date(year, 3 * q + 1, 1)
        if lo > end:
            return
        hi = date(year + (q == 3), (3 * q + 3) % 12 + 1, 1)
        yield max(lo, start), min(date.fromordinal(hi.toordinal() - 1), end)
        year, q = (year + 1, 0) if q == 3 else (year, q + 1)


def read_csv(path: Path) -> list[tuple[str, date]]:
    """``symbol,last_day`` rows (a header row and ``#`` comments are allowed)."""
    rows: list[tuple[str, date]] = []
    with path.open(newline="") as f:
        for line in csv.reader(f):
            if not line or line[0].strip().startswith("#") or line[0].strip().lower() == "symbol":
                continue
            if len(line) < 2:
                raise ValueError(f"{path}: expected 'symbol,last_day', got {line!r}")
            rows.append((line[0].strip().upper(), date.fromisoformat(line[1].strip())))
    return rows


def candidates(
    inactive: Iterable[str],
    actions: Iterable[tuple[str, str, date]],
    extra: Iterable[tuple[str, date]] = (),
) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    for sym in inactive:
        out.setdefault(sym, Candidate(sym)).note("inactive")
    for source, sym, when in actions:
        out.setdefault(sym, Candidate(sym)).note(source, when)
    for sym, when in extra:
        out.setdefault(sym, Candidate(sym)).note("csv", when)
    return out


def _signature(b: DailyBar) -> tuple[date, float, float, float, float, float]:
    return (b.day, b.open, b.high, b.low, b.close, b.volume)


def resolve(
    found: Mapping[str, Candidate],
    bars: Mapping[str, Sequence[DailyBar]],
    current: Mapping[str, Sequence[DailyBar]],
) -> tuple[list[tuple[Delisted, list[DailyBar]]], Counter[str]]:
    """Trading lives of the candidates (``bars``: their symbol-only daily bars) next to the
    store's current symbols (``current``). Returns the kept stocks and counts of what was
    dropped and why."""
    counts: Counter[str] = Counter()
    lives: dict[str, list[DailyBar]] = {}
    for sym, cand in found.items():
        series = sorted(bars.get(sym, ()), key=lambda b: b.day)
        if cand.last_day is not None:
            series = [b for b in series if b.day <= cand.last_day]
        if sym in current:
            same = {_signature(b) for b in current[sym]}
            kept = [b for b in series if _signature(b) not in same]
            if len(kept) < len(series):
                counts["reused_ticker_days_dropped"] += len(series) - len(kept)
            series = kept
        if not series:
            counts["no_bars"] += 1
            continue
        lives[sym] = series

    # A rename: the old ticker's last bar is some current symbol's bar that day.
    last_days = {s[-1].day for s in lives.values()}
    current_bars = {
        _signature(b)
        for bars_ in current.values()
        for b in bars_
        if b.day in last_days
    }
    kept_out: list[tuple[Delisted, list[DailyBar]]] = []
    for sym, series in sorted(lives.items()):
        if _signature(series[-1]) in current_bars:
            counts["renamed"] += 1
            continue
        last = series[-1].day
        key = f"{sym}@{last:%Y%m%d}" if sym in current else sym
        record = Delisted(key, sym, series[0].day, last, tuple(sorted(found[sym].sources)))
        kept_out.append((record, series))
        counts["kept"] += 1
    return kept_out, counts


def coverage(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Delisted stocks per year of their last trading day."""
    by_year = Counter(str(r["last"])[:4] for r in records)
    return dict(sorted(by_year.items()))

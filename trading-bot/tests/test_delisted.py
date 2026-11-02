"""Delisted stocks: candidates, trading lives, renames, reused tickers, and the download step."""

from __future__ import annotations

import argparse
import re
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import session, weekdays

from trading_bot.data import delisted as dl
from trading_bot.data.models import DailyBar
from trading_bot.data.store import BarStore, StoreSource

FUND = re.compile(r"\bETF\b|\bFund\b")
DAYS = weekdays(date(2023, 1, 2), 30)


def series(price: float, days: list[date], volume: float = 2e6) -> list[DailyBar]:
    return [DailyBar(d, price, price + 1, price - 1, price + 0.5, volume + i, price + 0.5)
            for i, d in enumerate(days)]


def test_inactive_assets_keep_plain_stocks_on_major_exchanges() -> None:
    assets = [
        {"symbol": "GONE", "exchange": "NYSE", "name": "Gone Inc"},
        {"symbol": "BRK.B", "exchange": "NYSE", "name": "Berkshire"},
        {"symbol": "OTCX", "exchange": "OTC", "name": "Pink Sheet Co"},
        {"symbol": "67058H102", "exchange": "NASDAQ", "name": "A CUSIP"},
        {"symbol": "ABCDW", "exchange": "NASDAQ", "name": "Warrants"},  # 5 letters: kept
        {"symbol": "OLDF", "exchange": "AMEX", "name": "Old ETF"},
        {"symbol": "X_DELISTED", "exchange": "NYSE", "name": "x"},
    ]
    assert dl.from_inactive_assets(assets, FUND) == ["ABCDW", "BRK.B", "GONE"]


def test_corporate_actions_and_quarters() -> None:
    page = {"corporate_actions": {
        "cash_mergers": [{"acquiree_symbol": "ATVI", "effective_date": "2023-10-13"},
                         {"acquiree_symbol": "bad sym", "effective_date": "2023-10-13"}],
        "stock_mergers": [{"acquiree_symbol": "XLNX", "process_date": "2022-02-14"}],
        "worthless_removals": [{"symbol": "BLPH", "process_date": "2024-12-19"}],
        "name_changes": [{"old_symbol": "FB", "new_symbol": "META"}],
    }}
    assert dl.from_corporate_actions(page) == [
        ("merger", "ATVI", date(2023, 10, 13)),
        ("merger", "XLNX", date(2022, 2, 14)),
        ("worthless", "BLPH", date(2024, 12, 19)),
    ]
    quarters = list(dl._quarters(date(2023, 2, 15), date(2024, 1, 10)))
    assert quarters[0] == (date(2023, 2, 15), date(2023, 3, 31))
    assert quarters[-1] == (date(2024, 1, 1), date(2024, 1, 10)) and len(quarters) == 5

    pages = [{**page, "next_page_token": "p2"}, {"corporate_actions": {}}]
    calls: list[dict[str, Any]] = []

    def get(path: str, params: dict[str, Any]) -> Any:
        calls.append(params)
        return pages.pop(0) if pages else {}

    found = dl.fetch_corporate_actions(get, date(2023, 1, 1), date(2023, 3, 31))
    assert len(found) == 3 and calls[1]["page_token"] == "p2"
    assert calls[0]["types"] == dl.CA_TYPES and calls[0]["start"] == "2023-01-01"


def test_read_csv_and_candidates(tmp_path: Path) -> None:
    path = tmp_path / "extra.csv"
    path.write_text("symbol,last_day\n# a comment\nsivb,2023-03-10\n")
    assert dl.read_csv(path) == [("SIVB", date(2023, 3, 10))]
    path.write_text("SIVB\n")
    with pytest.raises(ValueError):
        dl.read_csv(path)

    found = dl.candidates(
        ["GONE", "ATVI"],
        [("merger", "ATVI", date(2023, 10, 13)), ("merger", "ATVI", date(2021, 1, 4))],
        [("SIVB", date(2023, 3, 10))],
    )
    assert found["ATVI"].sources == {"inactive", "merger"}
    assert found["ATVI"].last_day == date(2023, 10, 13)  # the latest date wins
    assert found["GONE"].last_day is None and found["SIVB"].sources == {"csv"}


def test_resolve_drops_renames_and_splits_reused_tickers() -> None:
    first, second = DAYS[:15], DAYS[15:]
    current = {
        "NEW": series(20, DAYS),  # renamed from OLD on DAYS[15]: OLD's bars are NEW's bars
        "REUSE": series(70, second),  # a new company took the ticker REUSE
    }
    bars = {
        "OLD": current["NEW"][:15],
        "GONE": series(30, first),
        "BUY": series(40, DAYS),  # acquired: bars after the deal are dropped
        "REUSE": series(60, first) + current["REUSE"],
    }
    found = dl.candidates(["OLD", "GONE", "REUSE", "NOBARS"], [("merger", "BUY", DAYS[9])])
    kept, counts = dl.resolve(found, bars, current)
    by_key = {r.key: (r, s) for r, s in kept}
    assert sorted(by_key) == ["BUY", "GONE", f"REUSE@{DAYS[14]:%Y%m%d}"]
    assert counts["renamed"] == 1 and counts["no_bars"] == 1 and counts["kept"] == 3
    assert counts["reused_ticker_days_dropped"] == 15
    buy, buy_bars = by_key["BUY"]
    assert buy.last == DAYS[9] and len(buy_bars) == 10 and buy.sources == ("merger",)
    reuse, reuse_bars = by_key[f"REUSE@{DAYS[14]:%Y%m%d}"]
    assert reuse.symbol == "REUSE" and {b.open for b in reuse_bars} == {60}
    assert dl.coverage([r.to_meta() for r, _ in kept]) == {"2023": 3}


def test_store_universes_and_request_symbols(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars.sqlite")
    store.set_meta("universe", {"symbols": ["AAA", "REUSE"], "as_of": "2026-11-02"})
    store.set_meta("extra_symbols", ["SPY"])
    store.set_meta("delisted", {
        "GONE": {"symbol": "GONE", "first": "2023-01-02", "last": "2023-01-20", "sources": []},
        "REUSE@20230120": {"symbol": "REUSE", "first": "2023-01-02", "last": "2023-01-20",
                           "sources": []},
    })
    assert store.universe("stocks") == ["AAA", "REUSE"]
    assert store.universe("store") == ["AAA", "REUSE", "SPY"]
    assert store.universe("store+delisted") == ["AAA", "GONE", "REUSE", "REUSE@20230120", "SPY"]
    assert store.universe("stocks+delisted") == ["AAA", "GONE", "REUSE", "REUSE@20230120"]
    with pytest.raises(ValueError):
        store.universe("everything")
    assert store.request_symbol("REUSE@20230120") == ("REUSE", "-")
    assert store.request_symbol("GONE") == ("GONE", "-")
    assert store.request_symbol("AAA") == ("AAA", None)

    class Fallback:
        def __init__(self) -> None:
            self.asked: list[list[str]] = []

        def daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, Any]:
            self.asked.append(list(symbols))
            return {}

        def session_bars(self, symbols: list[str], day: Any) -> dict[str, Any]:
            self.asked.append(list(symbols))
            return {}

    fallback = Fallback()
    src = StoreSource(store, fallback=fallback)  # type: ignore[arg-type]
    src.daily_bars(["AAA", "REUSE@20230120"], DAYS[0], DAYS[1])
    src.session_bars(["REUSE@20230120"], session(DAYS[0]))
    assert fallback.asked == [["AAA"]]  # store keys of delisted stocks never reach Alpaca
    assert src.misses == {"daily_symbols": 2, "minute_symbol_days": 1}
    store.close()


class FakeData:
    """Stock data client: symbol-only daily bars per ticker."""

    def __init__(self, bars: dict[str, list[DailyBar]]) -> None:
        self.bars = bars
        self.params: list[dict[str, Any]] = []

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        assert path == "/stocks/bars" and params["asof"] == "-"
        self.params.append(params)
        out = {}
        for sym in params["symbols"].split(","):
            out[sym] = [
                {"t": f"{b.day.isoformat()}T05:00:00Z", "o": b.open, "h": b.high, "l": b.low,
                 "c": b.close, "v": b.volume}
                for b in self.bars.get(sym, [])
            ]
        return {"bars": out, "next_page_token": None}


def test_download_step_stores_lives_and_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_bot.data import download

    hist_start, end = DAYS[0] - timedelta(days=30), DAYS[-1]
    store = BarStore(tmp_path / "bars.sqlite")
    store.put_daily("NEW", series(20, DAYS), hist_start, end)
    fetcher = download.BarFetcher(
        FakeData({"OLD": series(20, DAYS)[:10], "GONE": series(30, DAYS[:12]),  # type: ignore[arg-type]
                  "BUY": series(40, DAYS)}),
        pages_per_minute=6000, sleep=lambda s: None,
    )

    class Trading:
        def get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            assert params["status"] == "inactive"
            return [{"symbol": s, "exchange": "NASDAQ", "name": f"{s} Inc"}
                    for s in ("OLD", "GONE")]

    class FakeActions:
        def __init__(self, *args: Any) -> None:
            pass

        def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
            if params["start"] <= DAYS[5].isoformat() <= params["end"]:
                return {"corporate_actions": {
                    "cash_mergers": [{"acquiree_symbol": "BUY", "effective_date":
                                      DAYS[5].isoformat()}]}}
            return {}

    module = types.ModuleType("alpaca.data.historical.corporate_actions")
    module.CorporateActionsClient = FakeActions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alpaca.data.historical.corporate_actions", module)
    args = argparse.Namespace(refresh_delisted=False, delisted_csv=None, end=end, rpm=6000,
                              daily_chunk_size=200)
    settings = types.SimpleNamespace(api_key="k", secret_key="s")
    clients = types.SimpleNamespace(trading=Trading())
    download._download_delisted(args, settings, clients, fetcher, store, ["NEW"], hist_start)

    meta = store.delisted()
    assert sorted(meta) == ["BUY", "GONE"]  # OLD is NEW under its former ticker
    assert meta["BUY"]["last"] == DAYS[5].isoformat() and meta["BUY"]["sources"] == ["merger"]
    assert store.daily_coverage()["GONE"] == (hist_start, end)
    assert len(store.daily(["BUY"], hist_start, end)["BUY"]) == 6
    info = store.get_meta("delisted_info")
    assert info["candidates"] == 3 and info["counts"]["renamed"] == 1
    assert "kept 2 of 3 candidates" in capsys.readouterr().out

    # A second run reuses the saved list without asking Alpaca again.
    calls = len(fetcher._client.params)  # type: ignore[attr-defined]
    download._download_delisted(args, settings, clients, fetcher, store, ["NEW"], hist_start)
    assert len(fetcher._client.params) == calls  # type: ignore[attr-defined]
    store.close()

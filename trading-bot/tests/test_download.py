from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from helpers import session
from test_engine import DAYS, build_source

from trading_bot.data.download import (
    BarFetcher,
    daily_from_raw,
    is_retryable,
    main,
    minutes_from_raw,
    plan_minutes,
)
from trading_bot.data.models import ET


class HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeClient:
    """Returns queued responses (or raises queued exceptions) and records the params."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.params: list[dict[str, Any]] = []

    def get(self, path: str, params: dict[str, Any]) -> Any:
        assert path == "/stocks/bars"
        self.params.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fetcher(client: FakeClient, sleeps: list[float], now: list[float]) -> BarFetcher:
    return BarFetcher(
        client,  # type: ignore[arg-type]
        pages_per_minute=60,
        sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),  # type: ignore[func-returns-value]
        clock=lambda: now[0],
        jitter=lambda: 0.0,
    )


START = datetime(2024, 3, 5, 9, 30, tzinfo=ET)
END = datetime(2024, 3, 5, 16, 0, tzinfo=ET)


def test_fetcher_follows_pages_and_spaces_every_page() -> None:
    client = FakeClient([
        {"bars": {"AAA": [{"t": "1"}], "BBB": [{"t": "2"}]}, "next_page_token": "p2"},
        {"bars": {"BBB": [{"t": "3"}]}, "next_page_token": None},
    ])
    sleeps: list[float] = []
    f = fetcher(client, sleeps, [100.0])
    out = f.bars(["AAA", "BBB"], "1Min", START, END)
    assert out == {"AAA": [{"t": "1"}], "BBB": [{"t": "2"}, {"t": "3"}]}
    assert f.pages == 2 and sleeps == [1.0]  # 60 pages/min -> one second between pages
    first, second = client.params
    assert first["symbols"] == "AAA,BBB" and first["timeframe"] == "1Min"
    assert first["start"] == "2024-03-05T14:30:00Z" and first["end"] == "2024-03-05T21:00:00Z"
    assert first["limit"] == 10_000 and first["feed"] == "sip" and first["adjustment"] == "raw"
    assert "page_token" not in first and second["page_token"] == "p2"


def test_fetcher_backs_off_on_rate_limits_and_gives_up_on_other_errors() -> None:
    client = FakeClient([HTTPError(429), ConnectionError("reset"), {"bars": {}}])
    sleeps: list[float] = []
    f = fetcher(client, sleeps, [0.0])
    assert f.bars(["AAA"], "1Day", START, END) == {}
    assert f.retries == 2 and 5.0 in sleeps and 10.0 in sleeps  # 5 s, then 10 s

    with pytest.raises(HTTPError):
        fetcher(FakeClient([HTTPError(403)]), [], [0.0]).bars(["AAA"], "1Day", START, END)
    assert is_retryable(HTTPError(503)) and is_retryable(TimeoutError())
    assert not is_retryable(ValueError())


def test_fetcher_stops_after_max_retries() -> None:
    client = FakeClient([HTTPError(429)] * 3)
    f = BarFetcher(client, pages_per_minute=6000, max_retries=2,  # type: ignore[arg-type]
                   sleep=lambda s: None, jitter=lambda: 0.0)
    with pytest.raises(HTTPError):
        f.bars(["AAA"], "1Min", START, END)
    assert f.retries == 2


def test_daily_from_raw_merges_split_adjusted_close() -> None:
    t = "2024-03-05T05:00:00Z"
    raw = {"AAA": [{"t": t, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}]}
    split = {"AAA": [{"t": t, "o": 5, "h": 5.5, "l": 4.5, "c": 5.25, "v": 200}]}
    (bar,) = daily_from_raw(raw, split)["AAA"]
    assert bar.day == date(2024, 3, 5) and bar.close == 10.5 and bar.adj_close == 5.25
    assert daily_from_raw(raw, {})["AAA"][0].adj_close is None


def test_minutes_from_raw_keeps_session_bars_as_offsets() -> None:
    day = session(date(2024, 3, 5))
    raw = {"AAA": [
        {"t": "2024-03-05T14:29:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},  # pre-market
        {"t": "2024-03-05T14:30:00Z", "o": 2, "h": 3, "l": 1, "c": 2.5, "v": 10, "vw": 2.2},
        {"t": "2024-03-05T14:31:00Z", "o": 2.5, "h": 3, "l": 2, "c": 2.8, "v": 5},
        {"t": "2024-03-05T21:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},  # after the close
    ]}
    assert minutes_from_raw(day, raw) == {
        "AAA": [[0, 2.0, 3.0, 1.0, 2.5, 10.0, 2.2], [60, 2.5, 3.0, 2.0, 2.8, 5.0, None]]
    }


def test_plan_uses_the_backtester_filters_and_adds_extras() -> None:
    src = build_source()
    days = [session(d) for d in DAYS]
    first_test = 20
    plan = plan_minutes(days, src.daily, first_test, ["SPY"], min_price=5, min_avg_volume=1e6,
                        min_atr=0.5, lookback_days=14)
    assert min(plan) == DAYS[first_test - 14] and max(plan) == DAYS[-1]
    assert plan[DAYS[20]] == {"AAA", "BBB", "SPY"}  # CCC is under $5
    assert all("SPY" in syms for syms in plan.values())


def test_main_validates_dates(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--start", "2024-02-01", "--end", "2024-01-01"]) == 2
    assert main(["--start", "2024-01-01", "--end", "2999-01-01"]) == 2
    assert "--end" in capsys.readouterr().err


def test_ordered_map_keeps_input_order_with_threads() -> None:
    import time as clock

    from trading_bot.data.download import ordered_map

    def slow(x: int) -> int:
        clock.sleep(0.005 * (5 - x))  # later items finish first
        return 2 * x

    assert list(ordered_map(slow, range(5), workers=3, ahead=2)) == [(i, 2 * i) for i in range(5)]
    assert list(ordered_map(slow, [], workers=3)) == []
    assert list(ordered_map(lambda x: x + 1, [1, 2], workers=1)) == [(1, 2), (2, 3)]


def test_fetcher_throttle_holds_across_threads() -> None:
    import time as clock

    from trading_bot.data.download import ordered_map

    client = FakeClient([{"bars": {}} for _ in range(12)])
    f = BarFetcher(client, pages_per_minute=3000)  # type: ignore[arg-type]
    started = clock.monotonic()
    list(ordered_map(lambda _: f.bars(["AAA"], "1Min", START, END), range(12), workers=4))
    assert f.pages == 12 and len(client.params) == 12
    assert clock.monotonic() - started >= 11 * 0.02 * 0.95  # 20 ms apart, even with 4 threads


def test_download_minutes_in_parallel_with_delisted_keys(tmp_path: Path) -> None:
    import threading

    from trading_bot.data.download import download_minutes
    from trading_bot.data.store import BarStore

    days = [session(d) for d in DAYS[:3]]
    seen: list[tuple[str, str | None, int]] = []

    class Client:
        def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
            seen.append((params["symbols"], params.get("asof"), threading.get_ident()))
            t = params["start"]  # the session open: one bar per symbol at the open
            return {"bars": {s: [{"t": t, "o": 1, "h": 2, "l": 1, "c": 2, "v": 9}]
                             for s in params["symbols"].split(",")}}

    store = BarStore(tmp_path / "bars.sqlite")  # opened in this thread only
    fetcher = BarFetcher(Client(), pages_per_minute=60_000)  # type: ignore[arg-type]
    remaining = {d.day: ["AAA", "BBB", "OLD@20240101"] for d in days}
    delisted = {"OLD@20240101": {"symbol": "OLD"}}
    done = download_minutes(fetcher, store, {d.day: d for d in days}, remaining, delisted,
                            chunk_size=1, workers=3)
    assert done == 9 and len(seen) == 9
    assert {(s, a) for s, a, _ in seen} == {("AAA", None), ("BBB", None), ("OLD", "-")}
    bars, stored = store.minutes(days[1], ["AAA", "OLD@20240101"])
    assert stored == {"AAA", "OLD@20240101"} and bars["OLD@20240101"][0].close == 2
    store.close()

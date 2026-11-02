"""AlpacaHistoricalSource against fake alpaca-py clients (no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from helpers import et, session

from trading_bot.data.historical import AlpacaHistoricalSource, active_us_equities
from trading_bot.data.models import ET, TradingDay

D = date(2024, 3, 5)  # EST: 09:30 ET = 14:30 UTC


def sdk_bar(ts: datetime, o: float, v: float = 100, vwap: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(timestamp=ts, open=o, high=o + 1, low=o - 1, close=o + 0.5, volume=v,
                           vwap=vwap)


class FakeStock:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def get_stock_bars(self, req: Any) -> SimpleNamespace:
        self.requests.append(req)
        symbols = req.symbol_or_symbols
        if req.timeframe.unit.value == "Day":
            close_offset = 0.0 if req.adjustment.value == "raw" else -5.0
            ts = datetime(2024, 3, 5, 5, 0, tzinfo=UTC)  # Alpaca daily bars stamp midnight ET
            data = {
                s: [SimpleNamespace(timestamp=ts, open=10, high=11, low=9,
                                    close=10.5 + close_offset, volume=1e6, vwap=None)]
                for s in symbols
            }
        else:
            data = {
                s: [sdk_bar(et(D, 9, 30 + i).astimezone(UTC), 10 + i, vwap=10.0) for i in range(6)]
                for s in symbols
                if s != "NOBARS"
            }
        return SimpleNamespace(data=data)


class FakeTrading:
    def __init__(self) -> None:
        self.calendar_calls = 0

    def get_calendar(self, req: Any) -> list[SimpleNamespace]:
        self.calendar_calls += 1
        return [SimpleNamespace(date=D, open=datetime(2024, 3, 5, 9, 30),
                                close=datetime(2024, 3, 5, 13, 0))]

    def get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert path == "/assets"
        assert params == {"status": "active", "asset_class": "us_equity"}

        def asset(sym: str, exch: str, tradable: bool = True, name: str = "Corp") -> dict[str, Any]:
            return {"symbol": sym, "exchange": exch, "tradable": tradable, "name": name}

        return [asset("MSFT", "NASDAQ"), asset("AAPL", "NASDAQ"), asset("IBM", "NYSE"),
                asset("PINK", "OTC"), asset("HALT", "NYSE", tradable=False),
                asset("NEWX", "TXSE"),  # an exchange alpaca-py 0.44.0's enum rejects
                asset("SPY", "ARCA", name="SPDR S&P 500 ETF Trust"),
                asset("QQQ", "NASDAQ", name="Invesco QQQ Trust, Series 1"),
                asset("TQQQ", "NASDAQ", name="ProShares UltraPro QQQ"),
                asset("IVZ", "NYSE", name="Invesco Ltd.")]


def make(tmp_path: Path | None, **kw: Any) -> tuple[AlpacaHistoricalSource, FakeStock, FakeTrading]:
    stock, trading = FakeStock(), FakeTrading()
    src = AlpacaHistoricalSource(stock, trading, cache_dir=tmp_path,  # type: ignore[arg-type]
                                 max_requests_per_minute=0, **kw)
    return src, stock, trading


def test_trading_days_are_eastern_and_cached(tmp_path: Path) -> None:
    src, _, trading = make(tmp_path)
    days = src.trading_days(D, D)
    assert days == [TradingDay(D, et(D, 9, 30), et(D, 13, 0))]
    assert days[0].open.tzinfo == ET
    src.trading_days(D, D)
    assert trading.calendar_calls == 1


def test_daily_bars_merge_split_adjusted_close(tmp_path: Path) -> None:
    src, stock, _ = make(tmp_path)
    bars = src.daily_bars(["AAA"], D, D)
    b = bars["AAA"][0]
    assert b.day == D  # 05:00 UTC is midnight ET, same trading date
    assert (b.close, b.adj_close, b.volume) == (10.5, 5.5, 1e6)
    assert {r.adjustment.value for r in stock.requests} == {"raw", "split"}
    assert all(r.feed.value == "sip" for r in stock.requests)


def test_opening_bars_filter_chunk_and_cache(tmp_path: Path) -> None:
    src, stock, _ = make(tmp_path, chunk_size=2)
    day = session(D)
    out = src.opening_bars(["AAA", "BBB", "NOBARS"], day, 5)
    assert sorted(out) == ["AAA", "BBB"]
    assert [b.timestamp for b in out["AAA"]] == [et(D, 9, 30 + i) for i in range(5)]  # 09:35 cut
    assert out["AAA"][0].vwap == 10.0
    assert len(stock.requests) == 2  # 3 symbols in chunks of 2
    req = stock.requests[0]
    assert req.start == datetime(2024, 3, 5, 14, 30)  # converted to naive UTC by alpaca-py
    assert req.adjustment.value == "raw" and req.timeframe.unit.value == "Min"

    again = src.opening_bars(["AAA", "BBB"], day, 5)
    assert again == {k: out[k] for k in ("AAA", "BBB")} and len(stock.requests) == 2

    src.opening_bars(["AAA", "CCC"], day, 5)  # only CCC is missing
    assert stock.requests[-1].symbol_or_symbols == ["CCC"] and len(stock.requests) == 3


def test_cache_survives_new_instance(tmp_path: Path) -> None:
    src, _, _ = make(tmp_path)
    first = src.session_bars(["AAA"], session(D))
    src2, stock2, _ = make(tmp_path)
    assert src2.session_bars(["AAA"], session(D)) == first
    assert stock2.requests == []


def test_no_cache_dir_always_fetches() -> None:
    src, stock, _ = make(None)
    src.session_bars(["AAA"], session(D))
    src.session_bars(["AAA"], session(D))
    assert len(stock.requests) == 2


def test_throttle_spaces_requests() -> None:
    sleeps: list[float] = []
    now = [100.0]
    stock, trading = FakeStock(), FakeTrading()
    src = AlpacaHistoricalSource(
        stock,  # type: ignore[arg-type]
        trading,  # type: ignore[arg-type]
        max_requests_per_minute=60,
        sleep=sleeps.append,
        clock=lambda: now[0],
    )
    src.session_bars(["AAA"], session(D))
    src.session_bars(["AAA"], session(D))
    assert sleeps == [1.0]


def test_active_us_equities_filters_exchange_and_tradable() -> None:
    # Unknown exchanges (TXSE) are skipped, not fatal.
    trading: Any = FakeTrading()
    assert active_us_equities(trading) == ["AAPL", "IBM", "IVZ", "MSFT", "QQQ", "SPY", "TQQQ"]
    assert active_us_equities(trading, stocks_only=True) == ["AAPL", "IBM", "IVZ", "MSFT"]

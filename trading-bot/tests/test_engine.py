from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from helpers import bar, session, weekdays

from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
    run_backtests,
)
from trading_bot.backtest.metrics import summarize
from trading_bot.backtest.report import format_summary, write_outputs
from trading_bot.data.models import Bar, DailyBar, TradingDay
from trading_bot.strategies.orb import OrbParams

DAYS = weekdays(date(2024, 1, 2), 25)
TEST_DAY = DAYS[20]


class FakeSource:
    """In-memory DataSource. Records which symbols were requested for opening ranges."""

    def __init__(self) -> None:
        self.daily: dict[str, list[DailyBar]] = {}
        self.opening: dict[tuple[date, str], list[Bar]] = {}
        self.session: dict[tuple[date, str], list[Bar]] = {}
        self.opening_requests: list[tuple[date, list[str]]] = []
        self.opening_minutes: list[int] = []

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        return [session(d) for d in DAYS if start <= d <= end]

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]:
        return {
            s: [b for b in self.daily[s] if start <= b.day <= end]
            for s in symbols
            if s in self.daily
        }

    def opening_bars(
        self, symbols: Sequence[str], day: TradingDay, minutes: int
    ) -> dict[str, list[Bar]]:
        self.opening_requests.append((day.day, list(symbols)))
        self.opening_minutes.append(minutes)
        return {s: self.opening[(day.day, s)] for s in symbols if (day.day, s) in self.opening}

    def session_bars(self, symbols: Sequence[str], day: TradingDay) -> dict[str, list[Bar]]:
        return {s: self.session[(day.day, s)] for s in symbols if (day.day, s) in self.session}


def or_bars(day: date, o: float, c: float, h: float, low: float, vol_each: float) -> list[Bar]:
    bars = [bar(day, 9, 30 + i, o, h, low, o, vol_each) for i in range(5)]
    bars[-1] = bar(day, 9, 34, o, h, low, c, vol_each)
    return bars


def build_source(split_bbb: bool = False) -> FakeSource:
    src = FakeSource()
    for sym, price in (("AAA", 50.0), ("BBB", 50.0), ("CCC", 4.0)):
        src.daily[sym] = []
        for i, d in enumerate(DAYS):
            adj = price / 2 if split_bbb and sym == "BBB" and i < 10 else price
            spread = 1.0 if price > 5 else 0.2
            src.daily[sym].append(
                DailyBar(d, price, price + spread, price - spread, price, 2_000_000, adj)
            )
            src.opening[(d, sym)] = or_bars(d, price, price + 0.1, price + 0.2, price - 0.1, 10_000)

    # Test day: AAA is in play (10x opening volume, up candle); BBB trades below normal volume.
    src.opening[(TEST_DAY, "AAA")] = or_bars(TEST_DAY, 50, 50.5, 50.6, 49.95, 100_000)
    src.opening[(TEST_DAY, "BBB")] = or_bars(TEST_DAY, 50, 50.5, 50.6, 49.95, 8_000)
    src.session[(TEST_DAY, "AAA")] = src.opening[(TEST_DAY, "AAA")] + [
        bar(TEST_DAY, 9, 35, 50.5, 50.8, 50.45, 50.75),
        bar(TEST_DAY, 12, 0, 50.9, 51.2, 50.9, 51.1),
        bar(TEST_DAY, 15, 50, 51.6, 51.7, 51.5, 51.6),
    ]
    return src


def config() -> BacktestConfig:
    return BacktestConfig(start=TEST_DAY, end=TEST_DAY)


def test_trades_only_the_stock_in_play() -> None:
    src = build_source()
    result = run_backtest(src, ["AAA", "BBB", "CCC"], OrbParams(), CostModel.zero(), config())

    assert [t.symbol for t in result.trades] == ["AAA"]
    t = result.trades[0]
    assert t.direction == "long" and t.relvol == pytest.approx(10)
    assert t.entry_price == 50.6 and t.exit_price == 51.6 and t.exit_reason == "close"
    # ATR = 2, stop = 0.2. Risk sizing says 1,250 shares; 4x leverage over 20 slots caps it at 98.
    assert t.qty == 98 and t.gross_r == pytest.approx(5)
    assert result.final_equity == pytest.approx(25_000 + 98)
    assert len(result.days) == 1 and result.days[0].trades == 1
    assert result.diagnostics["candidates"] == 1


def test_opening_bars_requested_only_for_eligible_symbols() -> None:
    src = build_source()
    run_backtest(src, ["AAA", "BBB", "CCC"], OrbParams(), CostModel.zero(), config())
    requested = {s for _, syms in src.opening_requests for s in syms}
    assert requested == {"AAA", "BBB"}  # CCC fails the price filter, so it is never fetched
    # The RelVol window needs the 14 sessions before the test day plus the test day itself.
    assert [d for d, _ in src.opening_requests] == DAYS[6:21]


def test_split_guard_skips_symbol() -> None:
    src = build_source(split_bbb=True)
    src.opening[(TEST_DAY, "BBB")] = or_bars(TEST_DAY, 50, 50.5, 50.6, 49.95, 100_000)
    result = run_backtest(src, ["AAA", "BBB"], OrbParams(), CostModel.zero(), config())
    assert [t.symbol for t in result.trades] == ["AAA"]
    assert result.diagnostics["split_guard_skips"] >= 1


def test_inverse_direction_placebo() -> None:
    src = build_source()
    result = run_backtest(
        src, ["AAA"], OrbParams(direction_mode="inverse"), CostModel.zero(), config()
    )
    assert result.trades == []  # the short trigger at 49.95 is never reached
    assert result.diagnostics["not_triggered"] == 1


def test_costs_are_charged() -> None:
    src = build_source()
    gross = run_backtest(src, ["AAA"], OrbParams(), CostModel.zero(), config())
    net = run_backtest(src, ["AAA"], OrbParams(), CostModel(), config())
    assert net.final_equity < gross.final_equity
    assert net.trades[0].gross_pnl == pytest.approx(gross.trades[0].gross_pnl)


def test_requires_enough_history() -> None:
    with pytest.raises(ValueError, match="history"):
        run_backtest(
            build_source(),
            ["AAA"],
            OrbParams(),
            CostModel.zero(),
            BacktestConfig(start=DAYS[5], end=DAYS[6]),
        )


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(start=DAYS[2], end=DAYS[1])
    with pytest.raises(ValueError):
        BacktestConfig(start=DAYS[1], end=DAYS[2], risk_per_trade=1.5)


def test_report_outputs(tmp_path: Path) -> None:
    result = run_backtest(build_source(), ["AAA"], OrbParams(), CostModel(), config())
    summary = summarize(result)
    write_outputs(result, summary, tmp_path / "run", extra={"universe_size": 1})
    out = tmp_path / "run"
    trades = list(csv.DictReader((out / "trades.csv").open()))
    assert [r["symbol"] for r in trades] == ["AAA"]
    assert float(trades[0]["gross_r"]) == pytest.approx(5, abs=1e-3)
    payload = json.loads((out / "summary.json").read_text())
    assert payload["summary"]["trades"] == 1 and payload["universe_size"] == 1
    assert payload["params"]["or_minutes"] == 5
    assert payload["config"]["start"] == TEST_DAY.isoformat()
    assert len(list(csv.reader((out / "equity.csv").open()))) == 2  # header + one day
    text = format_summary(summary, result)
    assert "Avg R per trade" in text and "cost" in text


def test_entry_window_blocks_late_breakouts() -> None:
    src = build_source()
    src.session[(TEST_DAY, "AAA")] = src.opening[(TEST_DAY, "AAA")] + [
        bar(TEST_DAY, 9, 35, 50.5, 50.55, 50.45, 50.5),  # below the 50.6 trigger
        bar(TEST_DAY, 12, 0, 50.9, 51.2, 50.9, 51.1),  # late breakout (gaps past the trigger)
        bar(TEST_DAY, 15, 50, 51.6, 51.7, 51.5, 51.6),
    ]
    late = run_backtest(src, ["AAA"], OrbParams(), CostModel.zero(), config())
    assert len(late.trades) == 1 and late.trades[0].entry_price == 50.9

    early_only = OrbParams(entry_window_minutes=30)
    blocked = run_backtest(src, ["AAA"], early_only, CostModel.zero(), config())
    assert blocked.trades == [] and blocked.diagnostics["not_triggered"] == 1


def fingerprint(result: BacktestResult) -> list[tuple[object, ...]]:
    return [(t.symbol, t.direction, t.entry_price, t.exit_price, t.qty) for t in result.trades]


def test_run_backtests_matches_separate_runs() -> None:
    universe = ["AAA", "BBB", "CCC"]
    variants = [
        OrbParams(),
        OrbParams(or_minutes=3),
        OrbParams(stop_mode="range"),
        OrbParams(min_relvol=20),  # filters everything out
        OrbParams(direction_mode="inverse"),
    ]
    src = build_source()
    together = run_backtests(src, universe, variants, CostModel(), config())
    # One opening-range request per day, for the longest window among the variants.
    assert set(src.opening_minutes) == {5} and len(src.opening_minutes) == 15
    for params, combined in zip(variants, together, strict=True):
        alone = run_backtest(build_source(), universe, params, CostModel(), config())
        assert fingerprint(combined) == fingerprint(alone)
        assert combined.final_equity == pytest.approx(alone.final_equity)
        assert combined.diagnostics == alone.diagnostics
    assert together[0].trades and not together[3].trades


def test_run_backtests_seeds_and_validation() -> None:
    random_dir = OrbParams(direction_mode="random")
    results = run_backtests(
        build_source(), ["AAA"], [random_dir] * 3, CostModel.zero(), config(), seeds=[1, 2, 3]
    )
    assert [r.config.seed for r in results] == [1, 2, 3]
    for seed, r in zip([1, 2, 3], results, strict=True):
        alone = run_backtest(
            build_source(), ["AAA"], random_dir, CostModel.zero(), replace(config(), seed=seed)
        )
        assert fingerprint(r) == fingerprint(alone)
    with pytest.raises(ValueError, match="share"):
        run_backtests(build_source(), ["AAA"], [OrbParams(), OrbParams(min_price=10)],
                      CostModel(), config())
    with pytest.raises(ValueError, match="seed"):
        run_backtests(build_source(), ["AAA"], [OrbParams()], CostModel(), config(), seeds=[])
    assert run_backtests(build_source(), ["AAA"], [], CostModel(), config()) == []


def test_longer_opening_range_slices_the_shared_window() -> None:
    src = build_source()
    # Add 9:35-9:44 bars (100 shares each, closing up) on every day.
    for (d, sym), bars in list(src.opening.items()):
        extra = [bar(d, 9, 35 + i, 50.5, 50.58, 50.45, 50.55, 100) for i in range(10)]
        src.opening[(d, sym)] = bars + extra
    five, fifteen = run_backtests(
        src, ["AAA"], [OrbParams(), OrbParams(or_minutes=15)], CostModel.zero(), config()
    )
    assert set(src.opening_minutes) == {15}  # fetched once, for the longest window
    # 5 minutes: 500k vs 50k a day. 15 minutes: 501k vs 51k (the extra bars count too).
    assert five.trades[0].relvol == pytest.approx(10)
    assert fifteen.trades[0].relvol == pytest.approx(501_000 / 51_000)


def test_comparison_outputs(tmp_path: Path) -> None:
    from trading_bot.backtest.metrics import combine_equal_weight, curve_stats
    from trading_bot.backtest.report import format_comparison, write_comparison

    result = run_backtest(build_source(), ["AAA"], OrbParams(), CostModel.zero(), config())
    rows = [{"label": "or5", **summarize(result).to_dict()}]
    combined = combine_equal_weight([result, result])
    rows.append({"label": "equal-weight", **curve_stats(25_000, combined)})
    write_comparison(rows, combined, tmp_path)
    table = list(csv.DictReader((tmp_path / "comparison.csv").open()))
    assert [r["label"] for r in table] == ["or5", "equal-weight"]
    assert table[1]["trades"] == ""  # the combination has no per-trade stats
    curve = list(csv.reader((tmp_path / "combined_equity.csv").open()))
    assert curve == [["day", "equity"], [TEST_DAY.isoformat(), "25098.00"]]
    text = format_comparison(rows)
    assert "or5" in text and "equal-weight" in text and "n/a" in text

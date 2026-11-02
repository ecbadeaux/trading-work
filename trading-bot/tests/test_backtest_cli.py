from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from trading_bot.backtest.cli import (
    build_parser,
    main,
    minutes_list,
    params_from_args,
    read_symbols,
)
from trading_bot.backtest.costs import CostModel
from trading_bot.strategies.orb import OrbParams

BASE = ["--start", "2024-01-02", "--end", "2024-06-28"]


def test_defaults_match_strategy_defaults() -> None:
    args = build_parser().parse_args([*BASE, "--symbols", "aapl, msft,AAPL"])
    params, costs, config = params_from_args(args)
    assert params == OrbParams()
    assert costs == CostModel()
    assert config.initial_equity == 25_000 and config.worst_case_intrabar
    assert read_symbols(args) == ["AAPL", "MSFT"]


def test_zero_costs_and_overrides() -> None:
    args = build_parser().parse_args(
        [*BASE, "--universe", "active", "--zero-costs", "--or-minutes", "15", "--target-r", "3",
         "--direction", "random", "--optimistic-intrabar"]
    )
    params, costs, config = params_from_args(args)
    assert costs == CostModel.zero()
    assert (params.or_minutes, params.target_r, params.direction_mode) == (15, 3.0, "random")
    assert not config.worst_case_intrabar
    assert read_symbols(args) is None


def test_optimization_flags() -> None:
    args = build_parser().parse_args(
        [*BASE, "--universe", "stocks", "--min-gap", "0.02", "--gap-aligned", "--min-atr-pct",
         "0.01", "--min-range-pct", "0.02", "--min-range-atr", "0.2", "--max-range-atr", "1.5",
         "--entry-mode", "close", "--entry-window", "30", "--breakeven-r", "2"]
    )
    params, _, _ = params_from_args(args)
    assert params == OrbParams(
        min_gap=0.02, gap_aligned=True, min_atr_pct=0.01, min_range_pct=0.02, min_range_atr=0.2,
        max_range_atr=1.5, entry_mode="close", entry_window_minutes=30, breakeven_r=2.0,
    )
    assert args.universe == "stocks"


def test_or_minutes_accepts_a_list() -> None:
    assert minutes_list("30, 5,15,5") == [5, 15, 30]
    assert minutes_list("15") == [15]
    for bad in ("", "0,5", "five"):
        with pytest.raises(argparse.ArgumentTypeError):
            minutes_list(bad)
    args = build_parser().parse_args([*BASE, "--symbols", "AAPL", "--or-minutes", "5,15,30"])
    params, _, _ = params_from_args(args)
    assert args.or_minutes == [5, 15, 30] and params.or_minutes == 5


def test_symbols_file_ignores_comments(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text("# universe\nnvda\n\ntsla  # comment\n")
    args = build_parser().parse_args([*BASE, "--symbols-file", str(f)])
    assert read_symbols(args) == ["NVDA", "TSLA"]


def test_end_date_must_be_in_the_past(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--start", "2024-01-02", "--end", "2999-01-01", "--symbols", "AAPL"])
    assert code == 2
    assert "--end must be before today" in capsys.readouterr().err


def test_missing_keys_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*BASE, "--symbols", "AAPL"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_offline_needs_no_keys_and_records_the_run(tmp_path: Path) -> None:
    from test_engine import TEST_DAY
    from test_store import build_store

    from trading_bot.backtest import ledger

    build_store(tmp_path / "bars.sqlite").close()
    day = TEST_DAY.isoformat()
    common = ["--start", day, "--end", day, "--store", str(tmp_path / "bars.sqlite"),
              "--offline", "--benchmark", "", "--out", str(tmp_path / "out")]
    assert main([*common, "--universe", "store", "--or-minutes", "5,3"]) == 0  # no keys set
    records = ledger.load(ledger.DEFAULT_LEDGER)
    assert [r["label"] for r in records] == ["or3", "or5"]
    assert records[1]["trades"] == 1 and records[1]["tool"] == "backtest"
    assert len(ledger.trial_keys(records)) == 2

    assert main([*common, "--symbols", "AAA", "--no-ledger"]) == 0
    assert len(ledger.load(ledger.DEFAULT_LEDGER)) == 2


def test_offline_rejects_api_universes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    store = tmp_path / "bars.sqlite"
    store.touch()
    assert main([*BASE, "--universe", "stocks", "--store", str(store), "--offline"]) == 2
    assert "--offline needs" in capsys.readouterr().err


def test_offline_tick_fills_use_stored_ticks_and_fall_back_without_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from test_engine import TEST_DAY
    from test_store import build_store
    from tick_helpers import fill_from_bars

    from trading_bot.backtest import ledger, tickplan
    from trading_bot.backtest.costs import CostModel
    from trading_bot.backtest.engine import BacktestConfig
    from trading_bot.data.store import StoreSource

    store = build_store(tmp_path / "bars.sqlite")
    windows = tickplan.plan_backtest(
        StoreSource(store), ["AAA", "BBB", "CCC"], [OrbParams()], CostModel(),
        BacktestConfig(TEST_DAY, TEST_DAY),
    )
    store.close()
    assert len(windows) == 3  # entry minute (trades + quotes) and the 15:50 quotes
    day = TEST_DAY.isoformat()
    common = ["--start", day, "--end", day, "--store", str(tmp_path / "bars.sqlite"),
              "--offline", "--benchmark", "", "--universe", "store", "--fills", "ticks",
              "--ticks", str(tmp_path / "ticks.sqlite")]

    assert main([*common, "--out", str(tmp_path / "bare")]) == 0
    assert "Bar-model fallbacks: {'missing_entry': 1, 'missing_exit': 1}" in capsys.readouterr().out
    (run,) = (tmp_path / "bare").iterdir()
    summary = json.loads((run / "summary.json").read_text())
    assert summary["tick_misses"] == {"minute": 1, "moment": 1}
    assert summary["fill_stats"]["fill_sources"] == {"bars": 1}

    assert fill_from_bars(tmp_path / "bars.sqlite", tmp_path / "ticks.sqlite", windows) == 3
    assert main([*common, "--out", str(tmp_path / "tape")]) == 0
    out = capsys.readouterr().out
    assert "Fills from the tape: 1 of 1 trades (100%)" in out and "Quoted spread" in out
    (run,) = (tmp_path / "tape").iterdir()
    summary = json.loads((run / "summary.json").read_text())
    assert summary["tick_misses"] == {} and summary["config"]["fills"] == "ticks"
    assert "fill_source" in (run / "trades.csv").read_text().splitlines()[0]

    records = ledger.load(ledger.DEFAULT_LEDGER)
    assert {r["fills"] for r in records} == {"ticks"} and records[0]["tick_latency_ms"] == 100
    bars_key = ledger.strategy_key(OrbParams(), CostModel(), True)
    assert records[0]["key"] != bars_key and len(ledger.trial_keys(records)) == 1

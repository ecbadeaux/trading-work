from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_engine import build_source, config

from trading_bot.backtest import ledger
from trading_bot.backtest.costs import CostModel
from trading_bot.backtest.engine import run_backtest
from trading_bot.backtest.metrics import summarize
from trading_bot.backtest.report import write_outputs
from trading_bot.strategies.orb import OrbParams


def record(params: OrbParams, out: str | None = None) -> dict[str, object]:
    return ledger.make_record(
        tool="test", params=params, costs=CostModel(), worst_case_intrabar=True,
        start="2024-01-02", end="2024-12-31", trades=10, avg_net_r=0.1, out=out,
    )


def test_strategy_key_covers_params_costs_and_fills_only() -> None:
    key = ledger.strategy_key(OrbParams(), CostModel(), True)
    assert key == ledger.strategy_key(OrbParams(), CostModel(), True)
    assert key != ledger.strategy_key(OrbParams(min_relvol=2.0), CostModel(), True)
    assert key != ledger.strategy_key(OrbParams(), CostModel.zero(), True)
    assert key != ledger.strategy_key(OrbParams(), CostModel(), False)
    a = record(OrbParams(), out="a")
    b = {**record(OrbParams(), out="b"), "start": "2019-01-02"}
    assert a["key"] == b["key"] == key  # the period doesn't make a new trial


def test_append_load_and_count_trials(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    assert ledger.load(path) == []
    ledger.append(path, [record(OrbParams()), record(OrbParams())])
    ledger.append(path, [record(OrbParams(min_relvol=5.0))])
    ledger.append(path, [record(OrbParams(direction_mode="random"))])  # a placebo
    with path.open("a") as f:
        f.write("not json\n")
    records = ledger.load(path)
    assert len(records) == 4
    assert len(ledger.trial_keys(records)) == 2
    assert "Distinct trials   2" in ledger.describe(records)
    assert ledger.append(path, []) == 0


def test_backfill_adds_each_old_run_once(tmp_path: Path) -> None:
    runs = tmp_path / "backtests"
    for name, params in (("one", OrbParams()), ("two", OrbParams(stop_mode="range"))):
        result = run_backtest(build_source(), ["AAA"], params, CostModel(), config())
        write_outputs(result, summarize(result), runs / name)
    # A run from before a parameter existed still hashes like today's defaults.
    old = runs / "two" / "summary.json"
    payload = json.loads(old.read_text())
    del payload["params"]["breakeven_r"]
    old.write_text(json.dumps(payload))
    (runs / "broken").mkdir()
    (runs / "broken" / "summary.json").write_text("{}")

    path = tmp_path / "ledger.jsonl"
    assert ledger.backfill(runs, path) == 2
    assert ledger.backfill(runs, path) == 0
    keys = ledger.trial_keys(ledger.load(path))
    assert keys == {
        ledger.strategy_key(OrbParams(), CostModel(), True),
        ledger.strategy_key(replace(OrbParams(), stop_mode="range"), CostModel(), True),
    }


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, [record(OrbParams())])
    assert ledger.main(["--ledger", str(path)]) == 0
    assert "Distinct trials   1" in capsys.readouterr().out
    assert ledger.main(["--ledger", str(path), "--backfill", str(tmp_path / "nope")]) == 2

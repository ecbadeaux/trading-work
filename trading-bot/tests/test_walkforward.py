from __future__ import annotations

import csv
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from helpers import session

from trading_bot.backtest import ledger, walkforward
from trading_bot.backtest.stats import Agg, Book, DayAgg
from trading_bot.backtest.walkforward import (
    HoldoutUsed,
    SpecError,
    StoreIncomplete,
    choose,
    load_spec,
    main,
    realized_vol,
    run_walkforward,
    scores,
    select_step,
    year_windows,
)
from trading_bot.data.models import DailyBar
from trading_bot.data.store import BarStore

V1 = Path(__file__).resolve().parents[1] / "docs" / "research" / "walkforward-orb-v1.toml"

YEARS = """
[years]
first = 2022
first_test = 2023
last_test = 2023
final = 2024
"""
TINY = f"""
name = "tiny"
{YEARS}
[selection]
min_trades = 1
[checks]
pbo_blocks = 4
bootstrap = 50
placebo_seeds = 2
[grid]
min_relvol = [1, 20]
"""


def write(tmp_path: Path, text: str, name: str = "spec.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# -- spec ------------------------------------------------------------------------------------


def test_v1_spec_parses() -> None:
    spec = load_spec(V1)
    assert spec.name == "orb-wf-v1" and len(spec.variants) == 48
    first = spec.variants[0]
    assert first.id == "v01" and first.label == (
        "or_minutes=5 stop_mode=range min_relvol=1 setup=all"
    )
    # Neighbours only along ordered (numeric) axes: or_minutes and min_relvol.
    assert [spec.variants[i].id for i in spec.neighbors[0]] == ["v17", "v03"]
    last = spec.variants[-1].params
    assert (last.or_minutes, last.stop_mode, last.min_relvol) == (30, "atr", 10.0)
    assert (last.min_atr_pct, last.min_gap, last.min_range_pct) == (0.021, 0.0066, 0.0118)
    assert isinstance(first.params.min_relvol, float)  # TOML ints become floats
    assert spec.train_years(2019) == [2017, 2018] and spec.final_year == 2026
    assert spec.train_years(2026) == list(range(2017, 2026))
    assert spec.criteria.min_t == 3.0 and spec.score == "neighborhood"
    assert spec.fills == "bars" and spec.universe == "store"  # v1 predates both options
    assert spec.config(date(2020, 1, 2), date(2020, 1, 3)).fills == "bars"


def test_fills_and_universe_options(tmp_path: Path) -> None:
    text = TINY.replace('name = "tiny"', 'name = "tiny"\nuniverse = "store+delisted"')
    spec = load_spec(write(tmp_path, text.replace("[grid]", '[fills]\nmode = "ticks"\n'
                                                   "latency_ms = 250\n[grid]")))
    assert spec.universe == "store+delisted" and spec.fills == "ticks"
    config = spec.config(date(2020, 1, 2), date(2020, 1, 3))
    assert config.fills == "ticks" and config.tick_latency_ms == 250


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (TINY.replace('name = "tiny"', 'name = "tiny"\nbogus = 1'), "unknown key"),
        (TINY.replace("first_test = 2023", "first_test = 2022"), "first < first_test"),
        (TINY.replace("min_trades = 1", 'metric = "sharpe"'), "metric"),
        (TINY.replace("pbo_blocks = 4", "pbo_blocks = 3"), "pbo_blocks"),
        (TINY.replace("min_relvol = [1, 20]", "min_relvol = [1, 1]"), "duplicate"),
        (TINY.replace("min_relvol = [1, 20]", "or_minutes = [0]"), "variant"),
        (TINY.replace("min_relvol = [1, 20]", "setup = [{ min_gap = 0.01 }]"), "label"),
        (TINY.replace("min_relvol = [1, 20]", 'setup = [{ label = "a", nope = 1 }]'), "unknown"),
        (TINY.replace("[grid]", "[params]\nmin_relvol = 2\n[grid]"), "both"),
        (TINY.replace("[grid]", "[portfolio]\nrisk_per_trade = 2\n[grid]"), "portfolio"),
        (TINY.replace("[grid]\nmin_relvol = [1, 20]", "[grid]"), "at least one axis"),
        (TINY.replace("[grid]", '[fills]\nmode = "quotes"\n[grid]'), "fills"),
        (TINY.replace("[grid]", "[fills]\nlatency_ms = -1\n[grid]"), "fills"),
        (TINY.replace("[grid]", "[fills]\nlag = 1\n[grid]"), "unknown key"),
        (TINY.replace('name = "tiny"', 'name = "tiny"\nuniverse = "all"'), "universe"),
        ("name = [", "spec.toml"),
    ],
)
def test_spec_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        load_spec(write(tmp_path, text))


# -- selection --------------------------------------------------------------------------------


def test_neighborhood_score_prefers_plateaus(tmp_path: Path) -> None:
    text = TINY.replace("min_relvol = [1, 20]", "min_relvol = [1, 2, 3, 4, 5]").replace(
        "min_trades = 1", 'min_trades = 100\nscore = "neighborhood"'
    )
    spec = load_spec(write(tmp_path, text))
    means = [0.30, -0.20, 0.10, 0.14, 0.05]  # a spike at v1, a plateau around v4
    counts = [1000] * 5
    nets = [m * n for m, n in zip(means, counts, strict=True)]
    assert choose(replace(spec, score="own"), nets, counts) == 0
    assert choose(spec, nets, counts) == 3
    smoothed = scores(spec, nets, counts)
    assert smoothed[3] == pytest.approx((0.10 + 0.14 + 0.05) / 3)
    # Variants under min_trades can't be picked and don't count as neighbours.
    thin = scores(spec, nets, [1000, 1000, 1000, 1000, 10])
    assert thin[4] is None and thin[3] == pytest.approx((0.10 + 0.14) / 2)
    assert choose(spec, nets, [10] * 5) is None


def book(net_per_trade: float, trades: int = 10, day: date = date(2020, 6, 1)) -> Book:
    return {day: DayAgg(long=Agg(trades, 0, net_per_trade * trades, 0.0))}


def test_select_step_uses_only_the_training_years(tmp_path: Path) -> None:
    text = TINY.replace(YEARS, "[years]\nfirst = 2017\nfirst_test = 2019\nlast_test = 2021\n")
    spec = load_spec(write(tmp_path, text))
    # v01 is best in 2017-2018, v02 in 2019-2021.
    books = {y: {"v1": book(0.2 if y < 2019 else -0.1), "v2": book(0.0 if y < 2019 else 0.3)}
             for y in range(2017, 2022)}
    step = select_step(spec, books, 2019)
    assert step.train_years == (2017, 2018) and step.variant is not None
    assert step.variant.id == "v1" and step.train.trades == 20 and step.eligible == 2
    later = select_step(spec, books, 2021)  # anchored: 2017-2020 pooled
    assert later.variant is not None and later.variant.id == "v2"
    rolling = replace(spec, window="rolling", rolling_years=1)
    assert rolling.train_years(2021) == [2020]
    assert select_step(rolling, books, 2020).variant.id == "v2"  # type: ignore[union-attr]


def test_year_windows_clip_to_the_store() -> None:
    windows = year_windows([2017, 2026], (date(2016, 11, 29), date(2026, 10, 30)), 14)
    assert windows[2017] == (date(2017, 1, 3), date(2017, 12, 31))
    assert windows[2026] == (date(2026, 1, 1), date(2026, 10, 30))
    with pytest.raises(SpecError, match="2015"):
        year_windows([2015], (date(2016, 11, 29), date(2026, 10, 30)), 14)


def test_realized_vol_uses_prior_days_only() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(30)]
    steady = {d: 100 * 1.001**i for i, d in enumerate(days)}
    assert all(v == pytest.approx(0, abs=1e-9) for v in realized_vol(steady).values())
    choppy = {d: 100 * (1.02 if i % 2 else 1.0) for i, d in enumerate(days)}
    vol = realized_vol(choppy)
    assert min(vol) == days[21] and all(v > 0.2 for v in vol.values())
    shocked = {**choppy, days[25]: 10.0}
    assert realized_vol(shocked)[days[25]] == pytest.approx(vol[days[25]])


# -- end to end on a synthetic store ----------------------------------------------------------

START, END = date(2021, 11, 1), date(2024, 12, 31)


def or_rows(o: float, h: float, low: float, c: float, volume: float) -> list[list[object]]:
    rows: list[list[object]] = [[60 * i, o, h, low, o, volume, None] for i in range(5)]
    rows[-1][4] = c
    return rows


def build_store(path: Path) -> Path:
    """AAA is in play every 4th day (won 2 times in 3); BBB and SPY never trigger."""
    days = []
    d = START
    while d <= END:
        if d.weekday() < 5:
            days.append(session(d))
        d += timedelta(days=1)
    store = BarStore(path)
    store.put_calendar(days, START, END)
    spy, close = [], 400.0
    for i, day in enumerate(days):
        close *= 1.01 if (i * 7) % 5 < 2 else 0.994
        spy.append(DailyBar(day.day, close, close + 2, close - 2, close, 2e6, close))
    flat = [DailyBar(day.day, 50, 51, 49, 50, 2e6, 50) for day in days]
    store.put_daily("AAA", flat, START, END)
    store.put_daily("BBB", flat, START, END)
    store.put_daily("SPY", spy, START, END)
    for i, (day, spy_bar) in enumerate(zip(days, spy, strict=True)):
        if i % 4 == 0:
            aaa = or_rows(50, 50.6, 49.95, 50.5, 100_000)
            if (i // 4) % 3:  # win: entry 50.6, exit 51.6 at 15:50 (+5R with the ATR stop)
                aaa += [[300, 50.5, 50.8, 50.45, 50.75, 1000, None],
                        [9000, 50.9, 51.2, 50.9, 51.1, 1000, None],
                        [22800, 51.6, 51.7, 51.5, 51.6, 1000, None]]
            else:  # loss: the 10:00 bar gaps through the stop
                aaa += [[300, 50.5, 50.7, 50.45, 50.65, 1000, None],
                        [1800, 50.3, 50.35, 49.5, 49.6, 1000, None],
                        [22800, 49.6, 49.7, 49.5, 49.6, 1000, None]]
        else:
            aaa = or_rows(50, 50.2, 49.9, 50.1, 10_000)
        c = spy_bar.close
        store.put_minutes(day, {
            "AAA": aaa,
            "BBB": or_rows(50, 50.2, 49.9, 50.1, 10_000),
            "SPY": or_rows(c, c + 0.5, c - 0.5, c + 0.1, 10_000),
        })
    store.set_meta("universe", {"symbols": ["AAA", "BBB"], "as_of": END.isoformat()})
    store.set_meta("extra_symbols", ["SPY"])
    store.close()
    return path


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return build_store(tmp_path / "bars.sqlite")


def run(tmp_path: Path, spec_path: Path, store_path: Path, **kw: object) -> walkforward.Outcome:
    return run_walkforward(
        load_spec(spec_path), store_path=store_path, out_root=tmp_path / "wf",
        ledger_path=tmp_path / "ledger.jsonl", **kw,  # type: ignore[arg-type]
    )


def test_run_walkforward_end_to_end(
    tmp_path: Path, store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = write(tmp_path, TINY)
    outcome = run(tmp_path, spec_path, store_path)

    selections = list(csv.DictReader((outcome.run_dir / "selections.csv").open()))
    assert [r["year"] for r in selections] == ["2023"]  # the holdout year is not touched
    assert selections[0]["variant"] == "v1"  # min_relvol=20 never trades
    assert int(selections[0]["test_trades"]) > 50 and float(selections[0]["test_net_r"]) > 0
    assert selections[0]["placebo_net_r"] != "" and selections[0]["optimistic_net_r"] != ""
    assert [c.name for c in outcome.checks][:2] == [
        "Out-of-sample t (day-clustered)", "Positive test years",
    ]
    assert len(outcome.checks) == 6 and outcome.passed == all(c.passed for c in outcome.checks)
    for name in ("report.md", "summary.json", "grid.csv", "oos_trades.csv"):
        assert (outcome.run_dir / name).is_file()
    oos = list(csv.DictReader((outcome.run_dir / "oos_trades.csv").open()))
    assert {r["symbol"] for r in oos} == {"AAA"} and {r["test_year"] for r in oos} == {"2023"}
    assert len(oos) == int(selections[0]["test_trades"])
    report = outcome.report
    assert "# Walk-forward tiny" in report and "| 2023 | 2022 | v1 min_relvol=1" in report
    assert "By market volatility" in report and "Survivorship" in report

    records = ledger.load(tmp_path / "ledger.jsonl")
    assert [r["label"] for r in records] == ["min_relvol=1", "min_relvol=20"]
    assert {r["tool"] for r in records} == {"walkforward"}
    cached = sorted(p.name[:4] for p in (tmp_path / "wf" / "tiny" / "cache").iterdir())
    assert "2024" not in cached and cached.count("2023") == 3  # grid + two check runs

    def fail(task: object) -> None:
        raise AssertionError("should have come from the cache")

    monkeypatch.setattr(walkforward, "run_year", fail)
    again = run(tmp_path, spec_path, store_path)
    assert again.report.split("\n", 1)[1] == report.split("\n", 1)[1]


def test_final_holdout_is_one_shot(tmp_path: Path, store_path: Path) -> None:
    spec_path = write(tmp_path, TINY)
    outcome = run(tmp_path, spec_path, store_path, final=True)
    years = [r["year"] for r in csv.DictReader((outcome.run_dir / "selections.csv").open())]
    assert years == ["2023", "2024"]
    assert outcome.checks[-1].name == "Holdout 2024 t (day-clustered)"
    assert "## Holdout 2024" in outcome.report
    assert (tmp_path / "wf" / "tiny" / "holdout.json").is_file()

    run(tmp_path, spec_path, store_path, final=True)  # the same spec may repeat it
    changed = write(tmp_path, TINY.replace("bootstrap = 50", "bootstrap = 60"), "v2.toml")
    with pytest.raises(HoldoutUsed):
        run(tmp_path, changed, store_path, final=True)
    run(tmp_path, changed, store_path, final=True, reuse_holdout=True)
    run(tmp_path, changed, store_path)  # without --final there is nothing to guard


def test_incomplete_store_is_refused(tmp_path: Path, store_path: Path) -> None:
    with sqlite3.connect(store_path) as db:
        db.execute("DELETE FROM minute WHERE day = '2023-03-01' AND symbol = 'AAA'")
    spec_path = write(tmp_path, TINY)
    with pytest.raises(StoreIncomplete, match="2023"):
        run(tmp_path, spec_path, store_path)
    run(tmp_path, spec_path, store_path, allow_missing=True)


def test_main(tmp_path: Path, store_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec_path = write(tmp_path, TINY)
    out = ["--store", str(store_path), "--out", str(tmp_path / "wf"), "--workers", "1"]
    assert main([str(spec_path), *out, "--no-ledger"]) == 0
    assert "# Walk-forward tiny" in capsys.readouterr().out
    assert not (tmp_path / "backtests").exists()  # --no-ledger
    assert main([str(tmp_path / "missing.toml"), *out]) == 2
    assert main([str(spec_path), "--store", str(tmp_path / "nope.sqlite")]) == 2
    no_final = write(tmp_path, TINY.replace("final = 2024\n", ""), "nofinal.toml")
    assert main([str(no_final), *out, "--final"]) == 2
    assert "no final year" in capsys.readouterr().err


# -- tick fills --------------------------------------------------------------------------------

TICKS = TINY.replace("[grid]", '[fills]\nmode = "ticks"\n[grid]')


def test_tick_fills_need_the_tick_store(tmp_path: Path, store_path: Path) -> None:
    from tick_helpers import fill_from_bars

    from trading_bot.backtest import tickplan
    from trading_bot.data.ticks import TickStore

    spec_path = write(tmp_path, TICKS)
    ticks = tmp_path / "ticks.sqlite"
    with pytest.raises(StoreIncomplete, match="tick store not found"):
        run(tmp_path, spec_path, store_path, ticks_path=ticks)
    TickStore(ticks).close()
    with pytest.raises(StoreIncomplete, match="trading-bot-ticks"):
        run(tmp_path, spec_path, store_path, ticks_path=ticks)

    # trading-bot-ticks plans the windows (no keys needed for --estimate) ...
    out = ["--store", str(store_path), "--ticks", str(ticks), "--out", str(tmp_path / "wf"),
           "--workers", "1"]
    assert tickplan.main([str(spec_path), *out, "--estimate"]) == 0
    assert tickplan.main([str(spec_path), *out, "--variants", "v9"]) == 2
    assert tickplan.main([str(spec_path), *out]) == 2  # downloading needs API keys

    spec = load_spec(spec_path)
    cal = BarStore(store_path).calendar_range()
    assert cal is not None
    windows = year_windows(spec.dev_years, cal, 14)
    needed = set()
    for y in spec.dev_years:
        task = walkforward.YearTask(
            store=str(store_path), universe=("AAA", "BBB", "SPY"), year=y, start=windows[y][0],
            end=windows[y][1], ids=("v1", "v2"), variants=tuple(v.params for v in spec.variants),
            seeds=(0, 0), costs=spec.costs, config=spec.config(*windows[y]),
        )
        needed |= tickplan.plan_year(task)[1]
    # ... and a synthetic tape built from the bars completes the store.
    fill_from_bars(store_path, ticks, needed)
    outcome = run(tmp_path, spec_path, store_path, ticks_path=ticks)
    report = outcome.report
    assert "with fills from real trades and quotes (100 ms latency)" in report
    assert "Net R minus random-direction placebo (bar fills)" in report
    assert "- Fills: " in report and "fully from the tape" in report
    assert "Fills come from SIP trades and quotes" in report
    oos = list(csv.DictReader((outcome.run_dir / "oos_trades.csv").open()))
    assert oos and {r["fill_source"] for r in oos} == {"ticks"}
    assert all(float(r["entry_spread_bps"]) > 0 for r in oos)
    records = ledger.load(tmp_path / "ledger.jsonl")
    assert {r["fills"] for r in records} == {"ticks"}


def test_delisted_universe_in_the_report(tmp_path: Path, store_path: Path) -> None:
    store = BarStore(store_path)
    store.set_meta("delisted", {"BBB": {"symbol": "BBB", "first": "2021-11-01",
                                        "last": "2024-12-31", "sources": ["inactive"]}})
    store.set_meta("delisted_info", {"by_year": {"2024": 1}})
    store.close()
    text = TINY.replace('name = "tiny"', 'name = "tiny"\nuniverse = "stocks+delisted"')
    outcome = run(tmp_path, write(tmp_path, text), store_path)
    assert "Survivorship bias, reduced: 1 delisted stocks" in outcome.report
    assert "(universe `stocks+delisted`)" in outcome.report

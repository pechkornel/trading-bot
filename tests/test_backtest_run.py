"""Offline tests for backtest.run: the pipeline on synthetic data and the CLI."""

from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd
import pytest

from backtest import run as brun
from backtest.ablations import VARIANTS
from backtest.data import DataError, MarketData
from tests.backtest_synth import make_frames, small_cfg

N = 90
SYMS = ["SPY", "TLT", "GLD", "IEF", "AGG", "BIL", "HYG", "LQD", "DX-Y.NYB", "^VIX"]
START = "2024-01-16"  # index 10 of the business days: the 11th bar (min_bars = 11)


def synth_data(n: int = N, first_dates: dict | None = None) -> MarketData:
    drift = {
        "SPY": 0.0010,
        "TLT": 0.0004,
        "GLD": -0.0003,
        "IEF": 0.0002,
        "AGG": 0.0003,
        "BIL": 0.0001,
        "HYG": 0.0005,
        "LQD": 0.0004,
        "DX-Y.NYB": 0.0,
        "^VIX": 0.0,
    }
    returns = {}
    for k, (s, d) in enumerate(drift.items()):
        # small deterministic wiggle so the scores and the metrics are not degenerate
        returns[s] = [0.0] + [d + 0.004 * (((i * (k + 3)) % 7) - 3) / 3 for i in range(1, n)]
    raw, adj = make_frames(returns, first_dates=first_dates)
    raw["^VIX"] = 15.0
    adj["^VIX"] = 15.0
    fred = {"T10Y2Y": pd.Series([0.5, 0.6], index=pd.to_datetime(["2023-12-01", "2023-12-15"]))}
    manifest = {
        "downloaded_at": "2026-11-24T10:00:00Z",
        "yfinance_version": "0.2.99",
        "tickers": {s: {"rows": n, "first": "2024-01-02", "last": "2024-05-03"} for s in SYMS},
        "fred": {"T10Y2Y": {"rows": 2, "first": "2023-12-01", "last": "2023-12-15"}},
    }
    return MarketData(raw_close=raw, adj_close=adj, fred=fred, manifest=manifest)


def run_synth(data=None, **kw):
    return brun.run_all(data or synth_data(), small_cfg(), 0, **kw)


# ---- run_all ----


def test_run_all_structure_and_start_date():
    r = run_synth()
    assert r["start_date"] == START
    assert r["trading_days"] > 50
    assert set(r["benchmarks"]) == {"spy_buy_hold", "sixty_forty", "equal_weight"}
    assert list(r["variants"]) == [v.name for v in VARIANTS]
    for key in ("cagr", "sharpe", "max_drawdown", "calmar", "annual_turnover"):
        assert key in r["strategy"]
    assert r["strategy"] == r["variants"]["base"]
    # the strategy starts in cash and trades, so it has fills and a positive turnover
    assert r["strategy"]["fills"] >= 1
    assert r["strategy"]["annual_turnover"] > 0
    assert 0.0 <= r["strategy"]["risk_off_share"] <= 1.0
    assert set(r["strategy"]["triggers"]) == {"calendar", "drift"}


def test_run_all_buy_and_hold_matches_spy_after_one_entry_cost():
    data = synth_data()
    r = run_synth(data)
    cal = data.adj_close.index[10:]
    spy = data.adj_close["SPY"].loc[cal]
    # bought at the close of day 1, so equity = SPY / SPY[1] * (1 - 5 bps) from day 1
    expected = spy.iloc[-1] / spy.iloc[1] * (1 - 0.0005)
    n_returns = len(cal) - 1
    want = expected ** (252 / n_returns) - 1
    assert r["benchmarks"]["spy_buy_hold"]["cagr"] == pytest.approx(want, rel=1e-9)


def test_run_all_costs_present_for_every_cost_rate():
    r = run_synth()
    assert set(r["benchmarks_by_cost"]) == {"0", "5", "20"}
    assert r["benchmarks"] == r["benchmarks_by_cost"]["5"]
    for cost in ("0", "5", "20"):
        assert set(r["benchmarks_by_cost"][cost]) == set(r["benchmarks"])
        assert "cagr" in r["strategy_by_cost"][cost]
    # a higher cost never raises the benchmark CAGR
    c = r["benchmarks_by_cost"]
    assert c["0"]["spy_buy_hold"]["cagr"] > c["5"]["spy_buy_hold"]["cagr"]
    assert c["5"]["spy_buy_hold"]["cagr"] > c["20"]["spy_buy_hold"]["cagr"]


def test_run_all_end_date_truncates():
    full = run_synth()
    short = run_synth(end="2024-03-01")
    assert short["end_date"] == "2024-03-01"
    assert short["trading_days"] < full["trading_days"]


def test_run_all_start_date_waits_for_late_ticker():
    data = synth_data(first_dates={"HYG": "2024-02-01"})
    r = run_synth(data)
    # 11 bars for HYG from 2024-02-01 (a Thursday): the 11th bar is 2024-02-15
    assert r["start_date"] == "2024-02-15"


def test_run_all_raises_when_no_date_qualifies():
    data = synth_data(n=8)
    with pytest.raises(ValueError, match="short tickers"):
        run_synth(data)


# ---- sensitivity and uncertainty sections ----


def test_weekday_sensitivity_covers_mon_to_fri_and_marks_the_live_day():
    r = run_synth()
    w = r["weekday_sensitivity"]
    assert list(w) == ["0", "1", "2", "3", "4"]
    assert [w[k]["name"] for k in w] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert [k for k, v in w.items() if v["live"]] == ["4"]  # small_cfg uses the default Friday
    for v in w.values():
        assert set(v) == {"name", "live", "cagr", "volatility", "sharpe", "max_drawdown"}
    # the live weekday is the base run
    assert w["4"]["cagr"] == r["strategy"]["cagr"]
    assert w["4"]["sharpe"] == r["strategy"]["sharpe"]


def test_weekday_sensitivity_follows_the_config_weekday():
    r = brun.run_all(synth_data(), small_cfg(rebalance_day_of_week=1), 0)
    w = r["weekday_sensitivity"]
    assert [k for k, v in w.items() if v["live"]] == ["1"]
    assert w["1"]["cagr"] == r["strategy"]["cagr"]


def test_subperiods_have_strategy_and_benchmarks():
    r = run_synth()
    sp = r["subperiods"]
    assert set(sp) == {"Strategy", "spy_buy_hold", "sixty_forty", "equal_weight"}
    assert list(sp["Strategy"]) == ["2008 to 2012", "2013 to 2019", "2020 to 2024"]
    assert sp["Strategy"]["2008 to 2012"]["days"] == 0
    assert sp["spy_buy_hold"]["2020 to 2024"]["days"] > 50
    assert sp["spy_buy_hold"]["2020 to 2024"]["cagr"] is not None


def test_bootstrap_is_none_when_the_sample_is_too_short():
    assert run_synth()["bootstrap"] is None


def test_bootstrap_section_on_a_long_sample_is_deterministic():
    r1 = brun.run_all(synth_data(n=400), small_cfg(), 0)
    r2 = brun.run_all(synth_data(n=400), small_cfg(), 0)
    b = r1["bootstrap"]
    assert b == r2["bootstrap"]
    assert (b["block_days"], b["draws"]) == (63, 2000)
    assert set(b["vs"]) == {"spy_buy_hold", "sixty_forty", "equal_weight"}
    for v in b["vs"].values():
        assert v["lo"] <= v["hi"]
        assert 0.0 <= v["share_better"] <= 1.0
    assert b["strategy_sharpe"]["lo"] <= b["strategy_sharpe"]["hi"]


# ---- results files ----


def test_results_json_valid_and_byte_identical(tmp_path):
    from backtest.report import write_json

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_json(run_synth(), a)
    write_json(run_synth(), b)
    assert a.read_bytes() == b.read_bytes()
    parsed = json.loads(a.read_text(), parse_constant=lambda c: pytest.fail(c))
    assert parsed["start_date"] == START
    assert set(parsed["variants"]) == {v.name for v in VARIANTS}
    assert set(parsed["benchmarks"]) == {"spy_buy_hold", "sixty_forty", "equal_weight"}


def test_main_writes_outputs(tmp_path):
    out = tmp_path / "out"
    code = brun.main(
        ["--out-dir", str(out), "--cache-dir", str(tmp_path / "cache")],
        loader=lambda *a, **k: synth_data(),
        core_cfg=small_cfg(),
    )
    assert code == 0
    parsed = json.loads((out / "results.json").read_text())
    assert parsed["start_date"] == START
    assert parsed["data"]["yfinance_version"] == "0.2.99"
    md = (out / "results.md").read_text()
    assert START in md
    for heading in (
        "## Summary",
        "## Data sources",
        "## Assumptions and deviations from live",
        "## Headline results",
        "## Rebalance weekday sensitivity",
        "## Subperiods",
        "## Uncertainty",
        "## Rebalance triggers",
        "## Calendar-year returns",
        "## Ablations",
        "## Costs",
    ):
        assert heading in md
    assert "2026-11-24T10:00:00Z" in md  # data download time
    assert "0.2.99" in md  # yfinance version
    assert "Generated:" in md
    for name in [v.name for v in VARIANTS]:
        assert f"| {name} |" in md
    for forbidden in (chr(0x2013), chr(0x2014)):
        assert forbidden not in md


def test_main_returns_1_and_writes_nothing_on_data_error(tmp_path, capsys):
    out = tmp_path / "out"

    def loader(*a, **k):
        raise DataError("SPY: empty price data")

    code = brun.main(["--out-dir", str(out), "--cache-dir", str(tmp_path / "c")], loader=loader)
    assert code == 1
    assert "SPY: empty price data" in capsys.readouterr().out
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_main_returns_1_and_writes_nothing_when_start_date_fails(tmp_path):
    out = tmp_path / "out"
    code = brun.main(
        ["--out-dir", str(out), "--cache-dir", str(tmp_path / "c")],
        loader=lambda *a, **k: synth_data(n=8),
        core_cfg=small_cfg(),
    )
    assert code == 1
    assert not out.exists()


def test_main_passes_refresh_and_cache_dir_to_loader(tmp_path):
    seen = {}

    def loader(tickers, fred_ids, cache_dir, refresh=False, **kw):
        seen.update(tickers=tickers, fred_ids=fred_ids, cache_dir=str(cache_dir), refresh=refresh)
        return synth_data()

    brun.main(
        ["--refresh", "--out-dir", str(tmp_path / "o"), "--cache-dir", str(tmp_path / "c")],
        loader=loader,
        core_cfg=small_cfg(),
    )
    assert seen["refresh"] is True
    assert seen["cache_dir"] == str(tmp_path / "c")
    assert seen["fred_ids"] == ["T10Y2Y"]
    assert "SPY" in seen["tickers"] and "^VIX" in seen["tickers"]


def test_main_skips_chart_with_message_when_matplotlib_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # makes `import matplotlib` fail
    out = tmp_path / "out"
    code = brun.main(
        ["--out-dir", str(out), "--cache-dir", str(tmp_path / "c")],
        loader=lambda *a, **k: synth_data(),
        core_cfg=small_cfg(),
    )
    assert code == 0
    assert "matplotlib is not installed" in capsys.readouterr().out
    assert (out / "results.json").exists()
    assert (out / "results.md").exists()
    assert not (out / "equity_curve.png").exists()
    assert "equity_curve.png" not in (out / "results.md").read_text()


def test_main_writes_png_when_matplotlib_present(tmp_path):
    pytest.importorskip("matplotlib")
    out = tmp_path / "out"
    code = brun.main(
        ["--out-dir", str(out), "--cache-dir", str(tmp_path / "c")],
        loader=lambda *a, **k: synth_data(),
        core_cfg=small_cfg(),
    )
    assert code == 0
    assert (out / "equity_curve.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "equity_curve.png" in (out / "results.md").read_text()


def test_import_run_without_yfinance_or_matplotlib():
    code = (
        "import sys\n"
        "sys.modules['yfinance'] = None\n"
        "sys.modules['matplotlib'] = None\n"
        "import backtest.run, backtest.report\n"
        "assert 'yfinance' not in [m for m, v in sys.modules.items() if v]\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_help_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.run", "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "--refresh" in proc.stdout and "--out-dir" in proc.stdout

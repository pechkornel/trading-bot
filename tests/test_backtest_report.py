"""Offline tests for backtest.report helpers."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest import report


def test_write_json_rounds_and_nulls_non_finite(tmp_path):
    path = tmp_path / "r.json"
    report.write_json(
        {"a": 1.23456789, "b": float("nan"), "c": float("inf"), "d": [0.5, None]}, path
    )
    parsed = json.loads(path.read_text())
    assert parsed == {"a": 1.234568, "b": None, "c": None, "d": [0.5, None]}
    assert path.read_text().endswith("\n")


def test_write_json_sorts_keys(tmp_path):
    path = tmp_path / "r.json"
    report.write_json({"b": 1, "a": 2}, path)
    assert path.read_text().index('"a"') < path.read_text().index('"b"')


def _results(bootstrap):
    row = {"cagr": 0.05, "volatility": 0.1, "sharpe": 0.5, "max_drawdown": -0.2}
    per = {
        "2008 to 2012": {"cagr": 0.01, "sharpe": 0.1},
        "2013 to 2019": {"cagr": None, "sharpe": None},
    }
    return {
        "base_cost_bps": 5.0,
        "weekday_sensitivity": {
            str(i): {"name": n, "live": i == 4, **row}
            for i, n in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"])
        },
        "subperiods": {k: per for k in ("Strategy", "spy_buy_hold")},
        "bootstrap": bootstrap,
    }


def test_weekday_section_marks_live_day():
    text = "\n".join(report._weekday_section(_results(None)))
    assert "## Rebalance weekday sensitivity" in text
    assert "| 4 Fri (live) |" in text
    assert "| 0 Mon |" in text
    assert text.count("(live)") == 1


def test_subperiod_section_has_a_column_pair_per_range_and_na_for_empty():
    text = "\n".join(report._subperiod_section(_results(None)))
    assert "2008 to 2012 CAGR" in text and "2013 to 2019 Sharpe" in text
    assert "| SPY buy and hold | 1.00% | 0.10 | n/a | n/a |" in text


def test_bootstrap_section_documents_method_and_prints_intervals():
    boot = {
        "block_days": 63,
        "draws": 2000,
        "seed": 5,
        "days": 900,
        "strategy_sharpe": {"point": 0.5, "lo": 0.1, "hi": 0.9},
        "vs": {"spy_buy_hold": {"diff_point": -0.1, "lo": -0.4, "hi": 0.2, "share_better": 0.3}},
    }
    text = "\n".join(report._bootstrap_section(_results(boot)))
    assert "63 day blocks" in text and "2000 draws" in text and "seed 5" in text
    assert "| SPY buy and hold | -0.10 | -0.40 to 0.20 | 30.0% |" in text


def test_bootstrap_section_when_not_computed():
    assert "Not computed" in "\n".join(report._bootstrap_section(_results(None)))


def test_plot_equity_writes_png(tmp_path):
    pytest.importorskip("matplotlib")
    idx = pd.bdate_range("2024-01-02", periods=30)
    eq = pd.Series([1.0 + 0.01 * i for i in range(30)], index=idx)
    dd = eq / eq.cummax() - 1.0
    path = tmp_path / "c.png"
    report.plot_equity({"Strategy": eq, "SPY": eq * 0.9}, {"Strategy": dd, "SPY": dd}, path)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_plot_equity_raises_import_error_without_matplotlib(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "matplotlib", None)
    with pytest.raises(ImportError):
        report.plot_equity({}, {}, tmp_path / "c.png")

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

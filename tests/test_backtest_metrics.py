"""Known-answer tests for backtest.metrics on tiny synthetic series."""

from __future__ import annotations

import importlib
import math
import sys

import pandas as pd
import pytest

from backtest import metrics


def equity_from_returns(returns, index=None) -> pd.Series:
    """Equity curve starting at 1.0 whose daily returns are `returns`."""
    values = [1.0]
    for r in returns:
        values.append(values[-1] * (1.0 + r))
    if index is None:
        index = pd.RangeIndex(len(values))
    return pd.Series(values, index=index)


def test_trading_days_constant():
    assert metrics.TRADING_DAYS == 252


def test_daily_returns():
    eq = pd.Series([1.0, 1.1, 0.99])
    r = metrics.daily_returns(eq)
    assert len(r) == 2
    assert r.iloc[0] == pytest.approx(0.1)
    assert r.iloc[1] == pytest.approx(-0.1)


def test_cagr_doubling_over_504_returns():
    eq = pd.Series([1.0] * 504 + [2.0])  # 504 returns, all growth on the last
    assert metrics.cagr(eq) == pytest.approx(math.sqrt(2) - 1)


def test_annual_volatility_two_values():
    r = pd.Series([0.01, -0.01])
    expected = 0.01 * math.sqrt(2) * math.sqrt(252)
    assert metrics.annual_volatility(r) == pytest.approx(expected)


def test_sharpe_alternating_by_hand():
    r = pd.Series([0.02, 0.0, 0.02, 0.0])
    rf = pd.Series([0.005] * 4)
    # x = [0.015, -0.005, 0.015, -0.005]: mean 0.005, deviations +-0.01
    std = 0.01 * math.sqrt(4 / 3)
    assert metrics.sharpe(r, rf) == pytest.approx(0.005 / std * math.sqrt(252))


def test_sharpe_none_when_constant_excess():
    r = pd.Series([0.001] * 5)
    rf = pd.Series([0.001] * 5)
    assert metrics.sharpe(r, rf) is None


def test_sharpe_aligns_rf_on_index():
    idx = pd.date_range("2020-01-01", periods=4, freq="B")
    r = pd.Series([0.02, 0.0, 0.02, 0.0], index=idx)
    rf = pd.Series([0.005] * 6, index=pd.date_range("2019-12-30", periods=6, freq="B"))
    std = 0.01 * math.sqrt(4 / 3)
    assert metrics.sharpe(r, rf) == pytest.approx(0.005 / std * math.sqrt(252))


def test_sortino_by_hand():
    r = pd.Series([0.02, 0.0, 0.02, 0.0])
    rf = pd.Series([0.005] * 4)
    # downside squares: 0, 2.5e-5, 0, 2.5e-5 -> mean 1.25e-5
    dd = math.sqrt(1.25e-5)
    assert metrics.sortino(r, rf) == pytest.approx(0.005 * 252 / (dd * math.sqrt(252)))


def test_sortino_none_without_downside():
    r = pd.Series([0.01, 0.02, 0.03])
    rf = pd.Series([0.0, 0.0, 0.0])
    assert metrics.sortino(r, rf) is None


def test_max_drawdown_known_series():
    dd = metrics.max_drawdown(pd.Series([1.0, 1.2, 0.9, 1.0, 1.3]))
    assert dd.depth == pytest.approx(-0.25)
    assert dd.peak == 1
    assert dd.trough == 2
    assert dd.recovery == 4


def test_max_drawdown_never_recovered():
    dd = metrics.max_drawdown(pd.Series([1.0, 1.2, 0.9, 1.0, 1.1]))
    assert dd.depth == pytest.approx(-0.25)
    assert dd.recovery is None


def test_max_drawdown_monotonic_up_has_zero_depth():
    dd = metrics.max_drawdown(pd.Series([1.0, 1.1, 1.2]))
    assert dd.depth == 0.0
    assert dd.recovery is None


def test_max_drawdown_uses_index_labels():
    idx = pd.date_range("2020-01-01", periods=5, freq="B")
    dd = metrics.max_drawdown(pd.Series([1.0, 1.2, 0.9, 1.0, 1.3], index=idx))
    assert dd.peak == idx[1]
    assert dd.trough == idx[2]
    assert dd.recovery == idx[4]


def test_calmar():
    assert metrics.calmar(0.10, -0.25) == pytest.approx(0.4)
    assert metrics.calmar(0.10, 0.0) is None


def test_annual_turnover():
    t = pd.Series([1.0, 0.0, 0.5, 0.5])
    # 2.0 total over 126 returns = half a year
    assert metrics.annual_turnover(t, 126) == pytest.approx(4.0)


def test_calendar_year_returns_across_year_boundary():
    idx = pd.to_datetime(["2020-12-30", "2020-12-31", "2021-01-04", "2021-12-31", "2022-01-03"])
    eq = pd.Series([1.0, 1.1, 1.21, 1.331, 1.4641], index=idx)
    out = metrics.calendar_year_returns(eq)
    assert set(out) == {2020, 2021, 2022}
    assert out[2020] == pytest.approx(0.1)  # partial: from first point
    assert out[2021] == pytest.approx(1.331 / 1.1 - 1)
    assert out[2022] == pytest.approx(1.4641 / 1.331 - 1)


def test_summarize_contains_every_field():
    idx = pd.date_range("2020-01-01", periods=6, freq="B")
    eq = equity_from_returns([0.01, -0.02, 0.01, 0.02, -0.005], index=idx)
    rf = metrics.daily_returns(eq) * 0 + 0.0001
    turnover = pd.Series([1.0, 0, 0, 0.5, 0, 0], index=idx)
    out = metrics.summarize(eq, rf, turnover, risk_off=0.2)
    for key in (
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "drawdown_peak",
        "drawdown_trough",
        "drawdown_recovery",
        "calmar",
        "annual_turnover",
        "calendar_year_returns",
        "risk_off_share",
    ):
        assert key in out
    assert out["drawdown_peak"] == idx[1].date().isoformat()
    assert out["drawdown_trough"] == idx[2].date().isoformat()
    assert out["risk_off_share"] == 0.2
    assert out["max_drawdown"] < 0


def test_import_without_yfinance_or_matplotlib(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.delitem(sys.modules, "backtest.metrics", raising=False)
    mod = importlib.import_module("backtest.metrics")
    assert hasattr(mod, "summarize")


# ---- block bootstrap and subperiods ----


def _synthetic(n=600, mean=0.0004, seed=1):
    import numpy as np

    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    return pd.Series(rng.normal(mean, 0.01, n), index=idx), pd.Series(0.0, index=idx)


def test_bootstrap_is_deterministic_with_the_seed():
    r, rf = _synthetic()
    b, _ = _synthetic(seed=2)
    a1 = metrics.bootstrap_sharpe(r, {"b": b}, rf, draws=200, seed=7)
    a2 = metrics.bootstrap_sharpe(r, {"b": b}, rf, draws=200, seed=7)
    a3 = metrics.bootstrap_sharpe(r, {"b": b}, rf, draws=200, seed=8)
    assert a1 == a2
    assert a1 != a3
    assert a1["block_days"] == 63 and a1["draws"] == 200 and a1["seed"] == 7


def test_bootstrap_interval_contains_point_estimate_and_is_ordered():
    r, rf = _synthetic()
    out = metrics.bootstrap_sharpe(r, {}, rf, draws=300, seed=3)
    s = out["strategy_sharpe"]
    assert s["lo"] < s["hi"]
    assert s["lo"] < s["point"] < s["hi"]


def test_bootstrap_identical_series_gives_exactly_zero_difference():
    r, rf = _synthetic()
    out = metrics.bootstrap_sharpe(r, {"same": r.copy()}, rf, draws=200, seed=3)
    d = out["vs"]["same"]
    assert d["diff_point"] == 0.0
    assert d["lo"] == 0.0 and d["hi"] == 0.0
    assert d["share_better"] == 0.0


def test_bootstrap_clearly_better_series_has_share_near_one():
    r, rf = _synthetic(mean=0.0004)
    better = r + 0.004  # same noise, much higher mean: a paired difference
    out = metrics.bootstrap_sharpe(better, {"worse": r}, rf, draws=300, seed=3)
    d = out["vs"]["worse"]
    assert d["share_better"] > 0.99
    assert d["lo"] > 0


def test_bootstrap_rejects_too_short_series():
    r, rf = _synthetic(n=40)
    with pytest.raises(ValueError):
        metrics.bootstrap_sharpe(r, {}, rf, draws=10, block=63)


def test_subperiod_stats_known_answer_and_empty_period():
    idx = pd.bdate_range("2010-01-04", periods=10)
    r = pd.Series([0.01] * 10, index=idx)
    rf = pd.Series(0.0, index=idx)
    periods = [("2010 to 2010", 2010, 2010), ("2020 to 2021", 2020, 2021)]
    out = metrics.subperiod_stats(r, rf, periods)
    assert out["2010 to 2010"]["cagr"] == pytest.approx(1.01**252 - 1)
    assert out["2010 to 2010"]["days"] == 10
    assert out["2020 to 2021"] == {"days": 0, "cagr": None, "sharpe": None}

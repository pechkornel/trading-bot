"""Offline tests for the backtest portfolio loop, calendar, start date and benchmarks."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engine import (
    buy_and_hold,
    first_start_date,
    min_bars_for,
    monthly_rebalance,
    simulate,
    trading_calendar,
)
from core.config import CoreSleeveConfig
from tests.backtest_synth import make_frames

BPS5 = 5.0


def scripted(plan: dict[int, dict[str, float]], calendar: pd.DatetimeIndex):
    """A decide function that returns plan[i] on calendar day i, else None."""
    pos = {t: i for i, t in enumerate(calendar)}

    def decide(t, weights):
        return plan.get(pos[t])

    return decide


def test_target_set_on_day_t_changes_holdings_from_t_plus_1():
    _, adj = make_frames({"A": [0, 0.10, 0.10, 0.10, 0.10]})
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 1.0}}, cal), BPS5)
    cost = 0.0005
    assert res.equity.iloc[0] == 1.0
    # day 1: A rose 10% but we were in cash; the fill happens at day 1 close, cost charged
    assert res.equity.iloc[1] == pytest.approx(1.0 - cost)
    assert res.weights["A"].iloc[0] == 0.0
    assert res.weights["A"].iloc[1] == pytest.approx(1.0)
    # day 2: first return earned
    assert res.equity.iloc[2] == pytest.approx((1.0 - cost) * 1.10)
    assert [t for t, _ in res.fills] == [cal[1]]
    assert res.turnover.iloc[1] == pytest.approx(1.0)
    assert res.costs.iloc[1] == pytest.approx(cost)


def test_day0_and_day1_returns_are_zero_before_fill():
    _, adj = make_frames({"A": [0, 0.05, 0.05, 0.05]})
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 1.0}}, cal), 0.0)
    assert res.equity.iloc[0] == 1.0
    assert res.equity.iloc[1] == pytest.approx(1.0)
    assert res.equity.iloc[2] == pytest.approx(1.05)


def test_switch_costs_turnover_two_and_ten_bps():
    _, adj = make_frames({"A": [0, 0, 0, 0, 0], "B": [0, 0, 0, 0, 0]})
    cal = adj.index
    plan = {0: {"A": 1.0}, 2: {"B": 1.0}}
    res = simulate(adj, cal, scripted(plan, cal), BPS5)
    assert res.turnover.iloc[1] == pytest.approx(1.0)
    assert res.turnover.iloc[3] == pytest.approx(2.0)
    assert res.costs.iloc[3] == pytest.approx(0.0010)
    assert res.equity.iloc[3] == pytest.approx((1 - 0.0005) * (1 - 0.0010))
    assert res.weights.loc[cal[3], "B"] == pytest.approx(1.0)
    assert res.weights.loc[cal[3], "A"] == pytest.approx(0.0)


def test_zero_bps_costs_nothing():
    _, adj = make_frames({"A": [0, 0, 0, 0], "B": [0, 0, 0, 0]})
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 1.0}, 1: {"B": 1.0}}, cal), 0.0)
    assert (res.costs == 0.0).all()
    assert (res.equity == 1.0).all()
    assert res.turnover.sum() == pytest.approx(3.0)


def test_weights_drift_between_trades():
    _, adj = make_frames({"A": [0, 0, 0.10, 0], "B": [0, 0, 0, 0]})
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 0.5, "B": 0.5}}, cal), 0.0)
    assert res.weights.loc[cal[1], "A"] == pytest.approx(0.5)
    assert res.weights.loc[cal[2], "A"] == pytest.approx(0.55 / 1.05)
    assert res.weights.loc[cal[2], "B"] == pytest.approx(0.5 / 1.05)
    assert res.weights.loc[cal[2], "A"] == pytest.approx(0.5238, abs=1e-4)
    assert res.weights.loc[cal[2], "B"] == pytest.approx(0.4762, abs=1e-4)
    assert res.equity.iloc[2] == pytest.approx(1.05)


def test_rebalance_turnover_uses_drifted_weights():
    _, adj = make_frames({"A": [0, 0, 0.10, 0], "B": [0, 0, 0, 0]})
    cal = adj.index
    plan = {0: {"A": 0.5, "B": 0.5}, 2: {"A": 0.5, "B": 0.5}}
    res = simulate(adj, cal, scripted(plan, cal), BPS5)
    drifted_a = 0.55 / 1.05
    drifted_b = 0.5 / 1.05
    expected_turnover = abs(0.5 - drifted_a) + abs(0.5 - drifted_b)
    assert res.turnover.iloc[3] == pytest.approx(expected_turnover)
    assert res.costs.iloc[3] == pytest.approx(expected_turnover * 0.0005)


def test_partial_weights_leave_cash():
    _, adj = make_frames({"A": [0, 0, 0.20]})
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 0.5}}, cal), 0.0)
    assert res.equity.iloc[2] == pytest.approx(1.10)
    assert res.weights["A"].iloc[2] == pytest.approx(0.60 / 1.10)


def test_decision_on_last_day_is_never_filled():
    _, adj = make_frames({"A": [0, 0, 0]})
    cal = adj.index
    res = simulate(adj, cal, scripted({2: {"A": 1.0}}, cal), BPS5)
    assert res.fills == []
    assert (res.equity == 1.0).all()


def test_decide_sees_post_fill_weights():
    _, adj = make_frames({"A": [0, 0, 0.10, 0]})
    cal = adj.index
    seen: list[dict[str, float]] = []

    def decide(t, weights):
        seen.append(dict(weights))
        return {"A": 1.0} if t == cal[0] else None

    simulate(adj, cal, decide, 0.0)
    assert seen[0] == {}
    assert seen[1]["A"] == pytest.approx(1.0)


def test_buy_and_hold_one_fill_and_known_equity():
    _, adj = make_frames({"SPY": [0, 0.01, 0.02, -0.01, 0.03, 0.0]})
    cal = adj.index
    res = simulate(adj, cal, buy_and_hold("SPY"), BPS5)
    c = 0.0005
    assert len(res.fills) == 1
    assert res.fills[0][0] == cal[1]
    for i in range(1, len(cal)):
        expected = (1 - c) * adj["SPY"].iloc[i] / adj["SPY"].iloc[1]
        assert res.equity.iloc[i] == pytest.approx(expected)


def test_monthly_rebalance_fills_on_first_trading_day_of_each_month():
    _, adj = make_frames({"A": [0.0] * 85, "B": [0.0] * 85}, start="2024-04-01")
    cal = adj.index
    assert cal[-1] >= pd.Timestamp("2024-07-01")
    decide = monthly_rebalance({"A": 0.5, "B": 0.5}, cal)
    res = simulate(adj, cal, decide, 0.0)
    fill_dates = [t for t, _ in res.fills]
    # Jun 1 2024 is a Saturday: the first trading day of June is Monday Jun 3.
    assert fill_dates == [
        pd.Timestamp("2024-04-02"),
        pd.Timestamp("2024-05-01"),
        pd.Timestamp("2024-06-03"),
        pd.Timestamp("2024-07-01"),
    ]


def test_monthly_rebalance_restores_target_weights():
    _, adj = make_frames(
        {"A": [0.0] * 3 + [0.10] + [0.0] * 40, "B": [0.0] * 44}, start="2024-04-01"
    )
    cal = adj.index
    res = simulate(adj, cal, monthly_rebalance({"A": 0.5, "B": 0.5}, cal), 0.0)
    assert res.weights.loc[pd.Timestamp("2024-04-29"), "A"] > 0.5
    assert res.weights.loc[pd.Timestamp("2024-05-01"), "A"] == pytest.approx(0.5)


def test_nan_adjusted_close_for_held_symbol_raises():
    _, adj = make_frames({"A": [0, 0, 0, 0, 0]})
    adj.iloc[3, adj.columns.get_loc("A")] = float("nan")
    cal = adj.index
    with pytest.raises(ValueError, match=r"A.*2024-01-05"):
        simulate(adj, cal, scripted({0: {"A": 1.0}}, cal), 0.0)


def test_nan_for_unheld_symbol_is_fine():
    _, adj = make_frames({"A": [0, 0, 0, 0], "B": [0, 0, 0, 0]})
    adj.iloc[2, adj.columns.get_loc("B")] = float("nan")
    cal = adj.index
    res = simulate(adj, cal, scripted({0: {"A": 1.0}}, cal), 0.0)
    assert res.equity.iloc[-1] == pytest.approx(1.0)


def test_trading_calendar_drops_a_day_missing_for_one_symbol():
    raw, adj = make_frames({"A": [0] * 6, "B": [0] * 6})
    missing = raw.index[2]
    raw.loc[missing, "B"] = float("nan")
    cal = trading_calendar(raw, adj, ["A", "B"])
    assert missing not in cal
    assert len(cal) == 5
    adj2 = adj.copy()
    adj2.loc[adj2.index[4], "A"] = float("nan")
    cal2 = trading_calendar(raw, adj2, ["A", "B"])
    assert len(cal2) == 4


def test_first_start_date_picks_the_day_latest_symbol_reaches_min_bars():
    raw, _ = make_frames({"A": [0] * 12, "B": [0] * 12}, first_dates={"B": "2024-01-05"})
    cal = raw.index
    # B's first bar is index 3 (Jan 5), so it has 4 bars on index 6
    assert first_start_date(raw, ["A", "B"], 4, cal) == cal[6]
    # A alone reaches 4 bars on index 3
    assert first_start_date(raw, ["A"], 4, cal) == cal[3]


def test_first_start_date_raises_naming_the_short_ticker():
    raw, _ = make_frames({"A": [0] * 8, "B": [0] * 8}, first_dates={"B": "2024-01-08"})
    with pytest.raises(ValueError, match="B"):
        first_start_date(raw, ["A", "B"], 6, raw.index)


def test_first_start_date_missing_symbol_column_is_short():
    raw, _ = make_frames({"A": [0] * 8})
    with pytest.raises(ValueError, match="ZZZ"):
        first_start_date(raw, ["A", "ZZZ"], 3, raw.index)


def test_min_bars_for_live_config_is_253():
    assert min_bars_for(CoreSleeveConfig()) == 253


def test_min_bars_for_takes_the_largest_requirement():
    cfg = CoreSleeveConfig(momentum_lookback_days=10, trend_ma_days=30, risk_off_spy_ma_days=5)
    assert min_bars_for(cfg) == 30


def test_import_engine_without_yfinance_or_matplotlib(monkeypatch):
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    sys.modules.pop("backtest.engine", None)
    importlib.import_module("backtest.engine")

"""HistoricalMacroFetcher: the live MacroFetcher.snapshot() replayed as of a date."""

from __future__ import annotations

import importlib
import sys

import pandas as pd
import pytest

from backtest.macro_replay import HistoricalMacroFetcher, replay_macro_config
from core.config import MacroConfig
from core.macro import MacroFetcher

T = pd.Timestamp("2021-06-30")
ONE_DAY = pd.Timedelta(days=1)
DAYS = pd.date_range("2020-01-01", "2021-12-31", freq="D")


def _frame() -> pd.DataFrame:
    n = len(DAYS)
    return pd.DataFrame(
        {
            "^VIX": pd.Series([20.0 + i * 0.01 for i in range(n)], index=DAYS),
            "DX-Y.NYB": pd.Series([90.0 + i * 0.1 for i in range(n)], index=DAYS),
            "HYG": pd.Series([80.0 + i * 0.02 for i in range(n)], index=DAYS),
            "LQD": pd.Series([120.0 + i * 0.01 for i in range(n)], index=DAYS),
        }
    )


def _fred() -> dict[str, pd.Series]:
    return {"T10Y2Y": pd.Series([0.5 + i * 0.001 for i in range(len(DAYS))], index=DAYS)}


def _fetcher(frame=None, fred=None) -> HistoricalMacroFetcher:
    cfg = replay_macro_config(MacroConfig())
    return HistoricalMacroFetcher(
        _frame() if frame is None else frame, _fred() if fred is None else fred, cfg
    )


def test_replay_config_keeps_tickers_and_only_yield_curve():
    live = MacroConfig()
    cfg = replay_macro_config(live)
    assert cfg.yf_tickers == live.yf_tickers
    assert cfg.fred_series == {"yield_curve": "T10Y2Y"}


def test_snapshot_values_match_hand_computation():
    f = _fetcher()
    f.set_asof(T)
    snap = f.snapshot()
    frame = _frame()
    loc = frame.loc[:T]
    window = loc[loc.index > T - pd.Timedelta(days=365)]
    assert snap.vix == pytest.approx(frame.loc[T, "^VIX"])
    assert snap.vix_60d_mean == pytest.approx(window["^VIX"].iloc[-60:].mean())
    assert snap.dxy == pytest.approx(frame.loc[T, "DX-Y.NYB"])
    dxy = window["DX-Y.NYB"]
    assert snap.dxy_trend_60d == pytest.approx((dxy.iloc[-1] - dxy.iloc[-60]) / dxy.iloc[-60])
    ratio = window["HYG"] / window["LQD"]
    assert snap.credit_ratio == pytest.approx(ratio.iloc[-1])
    # Hand-computed from the 60th row back, proving the live snapshot() path runs.
    assert snap.credit_ratio_trend_60d == pytest.approx(
        (ratio.iloc[-1] - ratio.iloc[-60]) / ratio.iloc[-60]
    )
    fred = _fred()["T10Y2Y"]
    assert snap.yield_curve == pytest.approx(fred.loc[T - ONE_DAY])


def test_fred_value_dated_t_is_not_used_on_t_but_is_on_t_plus_1():
    fred = _fred()
    fred["T10Y2Y"].loc[T] = 9.99
    f = _fetcher(fred=fred)
    f.set_asof(T)
    assert f.snapshot().yield_curve != 9.99
    f.set_asof(T + ONE_DAY)
    assert f.snapshot().yield_curve == 9.99


def test_vix_on_t_is_the_close_dated_t():
    frame = _frame()
    frame.loc[T, "^VIX"] = 33.3
    f = _fetcher(frame)
    f.set_asof(T)
    assert f.snapshot().vix == 33.3


def test_changing_rows_after_t_does_not_change_snapshot():
    base = _fetcher()
    base.set_asof(T)
    a = base.snapshot()

    frame = _frame()
    fred = _fred()
    frame.loc[frame.index > T, :] = 1234.0
    fred["T10Y2Y"][fred["T10Y2Y"].index >= T] = -50.0  # includes the row dated t
    other = _fetcher(frame, fred)
    other.set_asof(T)
    b = other.snapshot()
    for name in (
        "yield_curve",
        "yield_curve_trend_60d",
        "vix",
        "vix_60d_mean",
        "dxy",
        "dxy_trend_60d",
        "credit_ratio",
        "credit_ratio_trend_60d",
    ):
        assert getattr(a, name) == getattr(b, name), name


def test_set_asof_moves_the_window_each_call():
    f = _fetcher()
    f.set_asof(T)
    first = f.snapshot().vix
    f.set_asof(T + 10 * ONE_DAY)
    assert f.snapshot().vix != first


def test_value_older_than_365_days_is_ignored():
    f = _fetcher()
    f.set_asof(T)
    s = f._yf_series("^VIX")
    assert s.index.min() > T - pd.Timedelta(days=365)
    assert s.index.max() == T
    assert (T - pd.Timedelta(days=365)) not in s.index


def test_fred_series_returns_only_rows_before_t():
    f = _fetcher()
    f.set_asof(T)
    assert f._fred_series("T10Y2Y").index.max() == T - ONE_DAY


def test_fed_funds_none_complete_false_and_nothing_printed(capsys):
    f = _fetcher()
    f.set_asof(T)
    snap = f.snapshot()
    assert snap.fed_funds is None
    assert snap.fed_funds_trend_60d is None
    assert snap.complete is False
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_missing_ticker_gives_none_fields_without_raising(capsys):
    frame = _frame().drop(columns=["HYG", "^VIX"])
    f = _fetcher(frame)
    f.set_asof(T)
    snap = f.snapshot()
    assert snap.vix is None and snap.vix_60d_mean is None
    assert snap.credit_ratio is None and snap.credit_ratio_trend_60d is None
    assert snap.dxy is not None
    assert capsys.readouterr().out == ""


def test_missing_fred_series_gives_none_yield_curve():
    f = _fetcher(fred={})
    f.set_asof(T)
    assert f.snapshot().yield_curve is None


def test_nan_closes_are_dropped():
    frame = _frame()
    frame.loc[T, "^VIX"] = float("nan")
    f = _fetcher(frame)
    f.set_asof(T)
    assert f.snapshot().vix == pytest.approx(frame.loc[T - ONE_DAY, "^VIX"])


def test_is_a_macrofetcher_and_never_touches_the_network():
    f = _fetcher()
    assert isinstance(f, MacroFetcher)
    f.set_asof(T)
    f.snapshot()  # the autouse offline guard fails the test on any socket use


def test_import_works_without_yfinance(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.delitem(sys.modules, "backtest.macro_replay", raising=False)
    importlib.import_module("backtest.macro_replay")


def test_data_access_before_set_asof_raises():
    f = _fetcher()
    with pytest.raises(RuntimeError):
        f._yf_series("^VIX")
    with pytest.raises(RuntimeError):
        f._fred_series("T10Y2Y")

"""Offline tests for backtest.data: cache round trip, validation, errors."""

from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd
import pytest

from backtest import data as bd
from backtest.data import DataError, MarketData, load_market_data, required_tickers, validate_frame
from core.config import load_macro_config, load_phase8_sleeves


def _frame(n: int = 5, start: str = "2020-01-01", base: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series([base + i for i in range(n)], index=idx, dtype=float)
    return pd.DataFrame({"Close": close, "Adj Close": close * 0.98})


class Fakes:
    def __init__(self, fail: dict[str, int] | None = None):
        self.yahoo_calls: list[str] = []
        self.fred_calls: list[str] = []
        self.fail = dict(fail or {})

    def fetch_yahoo(self, ticker: str) -> pd.DataFrame:
        self.yahoo_calls.append(ticker)
        if self.fail.get(ticker, 0) > 0:
            self.fail[ticker] -= 1
            raise RuntimeError(f"boom {ticker}")
        return _frame(base=50.0 if ticker.startswith("^") else 100.0)

    def fetch_fred(self, series_id: str) -> pd.Series:
        self.fred_calls.append(series_id)
        idx = pd.bdate_range("2020-01-01", periods=5)
        return pd.Series([0.1, 0.2, 0.3, 0.4, 0.5], index=idx, name=series_id)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bd.time, "sleep", lambda *_: None)


def _load(tmp_path, fakes, tickers=("SPY", "^VIX"), refresh=False):
    return load_market_data(
        list(tickers),
        ["T10Y2Y"],
        tmp_path,
        refresh=refresh,
        fetch_yahoo=fakes.fetch_yahoo,
        fetch_fred=fakes.fetch_fred,
    )


def test_first_load_writes_cache_second_reads_it(tmp_path):
    f = Fakes()
    md = _load(tmp_path, f)
    assert isinstance(md, MarketData)
    assert sorted(f.yahoo_calls) == ["SPY", "^VIX"]
    assert f.fred_calls == ["T10Y2Y"]
    assert (tmp_path / "yahoo" / "SPY.csv").exists()
    assert (tmp_path / "fred" / "T10Y2Y.csv").exists()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert "downloaded_at" in manifest
    assert manifest["tickers"]["SPY"]["rows"] == 5

    f2 = Fakes()
    md2 = _load(tmp_path, f2)
    assert f2.yahoo_calls == [] and f2.fred_calls == []
    pd.testing.assert_frame_equal(md.raw_close, md2.raw_close)
    pd.testing.assert_frame_equal(md.adj_close, md2.adj_close)
    pd.testing.assert_series_equal(md.fred["T10Y2Y"], md2.fred["T10Y2Y"])


def test_refresh_downloads_again(tmp_path):
    _load(tmp_path, Fakes())
    f = Fakes()
    _load(tmp_path, f, refresh=True)
    assert sorted(f.yahoo_calls) == ["SPY", "^VIX"]
    assert f.fred_calls == ["T10Y2Y"]


def test_vix_cached_as_underscore_and_read_back_as_caret(tmp_path):
    md = _load(tmp_path, Fakes())
    assert (tmp_path / "yahoo" / "_VIX.csv").exists()
    assert not (tmp_path / "yahoo" / "^VIX.csv").exists()
    assert "^VIX" in md.raw_close.columns
    md2 = _load(tmp_path, Fakes())
    assert "^VIX" in md2.raw_close.columns and "^VIX" in md2.adj_close.columns


def test_index_is_naive_datetime_and_columns_aligned(tmp_path):
    md = _load(tmp_path, Fakes())
    for df in (md.raw_close, md.adj_close):
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is None
        assert list(df.columns) == ["SPY", "^VIX"]
    assert md.raw_close.index.equals(md.adj_close.index)
    # raw and adjusted differ (fake adj is 0.98 * close)
    assert (md.adj_close["SPY"] < md.raw_close["SPY"]).all()
    assert md.fred["T10Y2Y"].index.tz is None


def test_tz_aware_fetch_result_is_made_naive(tmp_path):
    def tz_fetch(ticker):
        df = _frame()
        df.index = df.index.tz_localize("America/New_York")
        return df

    md = load_market_data(
        ["SPY"],
        ["T10Y2Y"],
        tmp_path,
        fetch_yahoo=tz_fetch,
        fetch_fred=Fakes().fetch_fred,
    )
    assert md.raw_close.index.tz is None
    assert md.raw_close.index[0] == pd.Timestamp("2020-01-01")


def test_raising_fetcher_gives_data_error_naming_ticker_after_3_attempts(tmp_path):
    f = Fakes(fail={"SPY": 99})
    with pytest.raises(DataError, match="SPY"):
        _load(tmp_path, f, tickers=("SPY",))
    assert f.yahoo_calls == ["SPY", "SPY", "SPY"]
    assert not (tmp_path / "yahoo" / "SPY.csv").exists()


def test_transient_failure_is_retried(tmp_path):
    f = Fakes(fail={"SPY": 2})
    md = _load(tmp_path, f, tickers=("SPY",))
    assert f.yahoo_calls == ["SPY"] * 3
    assert len(md.raw_close) == 5


def test_empty_download_is_an_error_not_fake_data(tmp_path):
    with pytest.raises(DataError, match="SPY"):
        load_market_data(
            ["SPY"],
            ["T10Y2Y"],
            tmp_path,
            fetch_yahoo=lambda t: pd.DataFrame(),
            fetch_fred=Fakes().fetch_fred,
        )


def test_empty_fred_series_is_an_error(tmp_path):
    with pytest.raises(DataError, match="T10Y2Y"):
        load_market_data(
            ["SPY"],
            ["T10Y2Y"],
            tmp_path,
            fetch_yahoo=Fakes().fetch_yahoo,
            fetch_fred=lambda s: pd.Series(dtype=float),
        )


def test_failed_fred_fetch_names_series(tmp_path):
    def bad(_s):
        raise OSError("down")

    with pytest.raises(DataError, match="T10Y2Y"):
        load_market_data(
            ["SPY"], ["T10Y2Y"], tmp_path, fetch_yahoo=Fakes().fetch_yahoo, fetch_fred=bad
        )


def test_missing_ticker_in_cache_is_downloaded_others_untouched(tmp_path):
    _load(tmp_path, Fakes(), tickers=("SPY",))
    f = Fakes()
    md = _load(tmp_path, f, tickers=("SPY", "^VIX"))
    assert f.yahoo_calls == ["^VIX"]
    assert list(md.raw_close.columns) == ["SPY", "^VIX"]


def test_validate_frame_rejects_bad_frames():
    good = _frame()
    validate_frame("SPY", good)
    with pytest.raises(DataError, match="SPY"):
        validate_frame("SPY", good.iloc[0:0])
    with pytest.raises(DataError, match="Adj Close"):
        validate_frame("SPY", good[["Close"]])
    bad = good.copy()
    bad.iloc[2, 0] = 0.0
    with pytest.raises(DataError, match="non-positive"):
        validate_frame("SPY", bad)
    neg = good.copy()
    neg.iloc[1, 1] = -1.0
    with pytest.raises(DataError, match="non-positive"):
        validate_frame("SPY", neg)
    dup = pd.concat([good, good.iloc[[0]]])
    with pytest.raises(DataError, match="duplicate"):
        validate_frame("SPY", dup)


def test_required_tickers_on_live_config():
    core, *_ = load_phase8_sleeves()
    got = required_tickers(core, load_macro_config())
    expected = {
        "SPY", "IWM", "EFA", "VWO", "TLT", "IEF", "AGG", "GLD", "DBC", "BIL",
        "^VIX", "DX-Y.NYB", "HYG", "LQD",
    }  # fmt: skip
    assert got == sorted(expected)


def test_import_does_not_import_yfinance():
    code = "import sys; sys.modules['yfinance'] = None; import backtest.data; print('ok')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
    code2 = "import sys, backtest.data; print('yfinance' in sys.modules)"
    out2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True, check=True)
    assert out2.stdout.strip() == "False"


class TailNaNFakes(Fakes):
    """Yahoo returns a placeholder row with NaN prices for the unfinished latest day."""

    def __init__(self, nan_at: int = -1):
        super().__init__()
        self.nan_at = nan_at

    def fetch_yahoo(self, ticker: str) -> pd.DataFrame:
        df = super().fetch_yahoo(ticker).copy()
        df.iloc[self.nan_at] = float("nan")
        return df


def test_trailing_nan_row_is_dropped_and_not_cached(tmp_path):
    data = _load(tmp_path, TailNaNFakes(nan_at=-1))

    assert len(data.raw_close) == 4
    assert not data.raw_close.isna().any().any()
    assert not data.adj_close.isna().any().any()
    cached = pd.read_csv(tmp_path / "yahoo" / "SPY.csv")
    assert len(cached) == 4


def test_nan_inside_the_series_is_still_an_error(tmp_path):
    with pytest.raises(DataError, match="SPY: missing"):
        _load(tmp_path, TailNaNFakes(nan_at=2), tickers=("SPY",))


def _ny(text: str):
    return pd.Timestamp(text, tz="America/New_York").to_pydatetime()


class TodayFakes(Fakes):
    """Frames whose last row is dated 2024-03-13 (a Wednesday), with a live Close."""

    def fetch_yahoo(self, ticker: str) -> pd.DataFrame:
        self.yahoo_calls.append(ticker)
        idx = pd.bdate_range(end="2024-03-13", periods=5)
        close = pd.Series([100.0 + i for i in range(5)], index=idx)
        return pd.DataFrame({"Close": close, "Adj Close": close * 0.98})


def _load_at(tmp_path, when):
    return load_market_data(
        ["SPY", "^VIX"],
        ["T10Y2Y"],
        tmp_path,
        fetch_yahoo=TodayFakes().fetch_yahoo,
        fetch_fred=Fakes().fetch_fred,
        now=lambda: _ny(when),
    )


def test_download_during_market_hours_drops_todays_row(tmp_path):
    md = _load_at(tmp_path, "2024-03-13 11:00")
    assert md.raw_close.index[-1] == pd.Timestamp("2024-03-12")
    assert len(md.raw_close) == 4
    assert len(pd.read_csv(tmp_path / "yahoo" / "SPY.csv")) == 4


def test_download_at_17h_keeps_etf_row_but_drops_vix_row(tmp_path):
    md = _load_at(tmp_path, "2024-03-13 17:00")
    assert md.raw_close["SPY"].index[-1] == pd.Timestamp("2024-03-13")
    assert pd.isna(md.raw_close.loc["2024-03-13", "^VIX"])
    assert md.raw_close["^VIX"].dropna().index[-1] == pd.Timestamp("2024-03-12")


def test_download_at_18h_keeps_both(tmp_path):
    md = _load_at(tmp_path, "2024-03-13 18:00")
    assert md.raw_close.loc["2024-03-13"].notna().all()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["tickers"]["SPY"]["dropped_unsettled"] == []
    assert manifest["tickers"]["^VIX"]["dropped_unsettled"] == []


def test_manifest_records_dropped_dates(tmp_path):
    _load_at(tmp_path, "2024-03-13 17:00")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["tickers"]["SPY"]["dropped_unsettled"] == []
    assert manifest["tickers"]["^VIX"]["dropped_unsettled"] == ["2024-03-13"]


def test_weekend_download_keeps_friday(tmp_path):
    md = _load_at(tmp_path, "2024-03-16 11:00")
    assert md.raw_close.index[-1] == pd.Timestamp("2024-03-13")

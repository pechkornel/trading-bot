"""Download and cache the daily data the backtest needs.

Yahoo daily bars (raw `Close` and `Adj Close`) and FRED series are cached as CSV
under a cache folder (default `data/cache/backtest/`, git-ignored). There is no
fake or fallback data: an empty or failed download raises `DataError` naming the
ticker or series.

yfinance is imported only inside `_default_fetch_yahoo`, so importing this module
needs neither yfinance nor the network.

Both a fresh download and a cache hit go through the same CSV write and read, so
the frames a caller gets do not depend on whether the cache was warm.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import CoreSleeveConfig, MacroConfig

_ATTEMPTS = 3
_PAUSE_SEC = 1.0
_COLUMNS = ["Close", "Adj Close"]

_NY = ZoneInfo("America/New_York")
_SETTLE_ETF = dtime(16, 30)
_SETTLE_LATE = dtime(17, 15)
_LATE_SETTLERS = {"^VIX", "DX-Y.NYB"}

Clock = Callable[[], datetime]
FetchYahoo = Callable[[str], pd.DataFrame]
FetchFred = Callable[[str], pd.Series]


class DataError(RuntimeError):
    """A download or cached file is missing, empty or invalid."""


@dataclass
class MarketData:
    raw_close: pd.DataFrame
    adj_close: pd.DataFrame
    fred: dict[str, pd.Series]
    manifest: dict = field(default_factory=dict)


def required_tickers(core_cfg: CoreSleeveConfig, macro_cfg: MacroConfig) -> list[str]:
    """Core universe, risk-off universe, SPY, AGG, BIL and the macro Yahoo tickers."""
    names = set(core_cfg.universe) | set(core_cfg.risk_off_universe)
    names |= {"SPY", "AGG", "BIL"}
    names |= set(macro_cfg.yf_tickers.values())
    return sorted(names)


def validate_frame(ticker: str, df: pd.DataFrame) -> None:
    """Raise DataError for an empty frame, missing columns, bad prices or repeated dates."""
    if df is None or len(df) == 0:
        raise DataError(f"{ticker}: empty price data")
    for col in _COLUMNS:
        if col not in df.columns:
            raise DataError(f"{ticker}: missing column {col!r}")
    prices = df[_COLUMNS]
    if prices.isna().any().any():
        raise DataError(f"{ticker}: missing (NaN) prices")
    if (prices <= 0).any().any():
        raise DataError(f"{ticker}: non-positive prices")
    if df.index.has_duplicates:
        raise DataError(f"{ticker}: duplicate dates")


def drop_incomplete_tail(df: pd.DataFrame) -> pd.DataFrame:
    """Drop trailing rows with any missing price.

    Yahoo returns a placeholder row with NaN prices for the latest day while it
    is not finished. Only rows at the very end are dropped; a gap inside the
    series is still rejected by validate_frame.
    """
    cols = [c for c in _COLUMNS if c in df.columns]
    if not cols or len(df) == 0:
        return df
    complete = ~df[cols].isna().any(axis=1)
    if complete.all():
        return df
    if not complete.any():
        return df.iloc[0:0]
    last_complete = complete.to_numpy().nonzero()[0][-1]
    return df.iloc[: last_complete + 1]


def drop_unsettled_today(
    df: pd.DataFrame, ticker: str, now: datetime
) -> tuple[pd.DataFrame, list[str]]:
    """Drop rows dated on or after today (New York) unless the day has settled.

    During US market hours Yahoo returns today's row with a live Close that is
    not final. A row for today is kept only when `now` is after 16:30 New York
    time for ETFs, or after 17:15 for ^VIX and DX-Y.NYB, which settle later.
    Returns the kept frame and the dropped dates as YYYY-MM-DD strings.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "the clock must return a timezone-aware datetime (for example "
            "datetime.now(timezone.utc)), got a naive one"
        )
    if len(df) == 0:
        return df, []
    local = now.astimezone(_NY)
    cutoff = _SETTLE_LATE if ticker in _LATE_SETTLERS else _SETTLE_ETF
    today = pd.Timestamp(local.date())
    limit = today + pd.Timedelta(days=1) if local.time() > cutoff else today
    dates = _naive_dates(df.index)
    drop = dates >= limit
    if not drop.any():
        return df, []
    return df[~drop], [d.strftime("%Y-%m-%d") for d in dates[drop]]


_SAFE_NAME = re.compile(r"[A-Za-z0-9._^=-]+")


def _safe(name: str) -> str:
    """Cache file stem for a ticker or series id. Rejects anything that could leave the cache."""
    if not _SAFE_NAME.fullmatch(name) or name.startswith("."):
        raise DataError(f"unsafe cache name: {name!r}")
    return name.replace("^", "_")


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def _default_fetch_yahoo(ticker: str) -> pd.DataFrame:
    import yfinance as yf  # lazy: only the real download needs it

    df = yf.Ticker(ticker).history(period="max", interval="1d", auto_adjust=False, actions=False)
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_COLUMNS)
    return df[[c for c in _COLUMNS if c in df.columns]]


def _default_fetch_fred(series_id: str) -> pd.Series:
    from core.macro import MacroFetcher

    return MacroFetcher(MacroConfig())._fred_series(series_id)


def _retry(label: str, call: Callable[[], object]):
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - surfaced as DataError below
            last = exc
            if attempt < _ATTEMPTS - 1:
                time.sleep(_PAUSE_SEC)
    raise DataError(f"{label}: download failed after {_ATTEMPTS} attempts: {last}") from last


def _write_yahoo(path: Path, ticker: str, df: pd.DataFrame) -> None:
    if len(df) == 0:
        raise DataError(f"{ticker}: empty price data")
    for col in _COLUMNS:
        if col not in df.columns:
            raise DataError(f"{ticker}: missing column {col!r}")
    df = df[_COLUMNS].copy()
    df.index = _naive_dates(df.index)
    df = df.sort_index()
    validate_frame(ticker, df)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index_label="Date")


def _read_yahoo(path: Path, ticker: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df.index)
    df.index.name = None
    validate_frame(ticker, df)
    return df[_COLUMNS]


def _write_fred(path: Path, series_id: str, s: pd.Series) -> None:
    if s is None or len(s) == 0:
        raise DataError(f"{series_id}: empty FRED series")
    s = pd.Series(s).dropna().astype(float)
    if len(s) == 0:
        raise DataError(f"{series_id}: empty FRED series")
    s.index = _naive_dates(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    s.to_csv(path, index_label="Date", header=["Value"])


def _read_fred(path: Path, series_id: str) -> pd.Series:
    df = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
    s = df["Value"]
    s.index = pd.DatetimeIndex(s.index)
    s.index.name = None
    s.name = series_id
    if len(s) == 0:
        raise DataError(f"{series_id}: empty FRED series")
    return s


def _yfinance_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("yfinance")
    except Exception:  # noqa: BLE001 - not installed is fine, just unrecorded
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _range(idx: pd.Index) -> dict:
    return {
        "rows": int(len(idx)),
        "first": idx[0].strftime("%Y-%m-%d"),
        "last": idx[-1].strftime("%Y-%m-%d"),
    }


def _load_all(
    tickers: list[str],
    fred_ids: list[str],
    cache: Path,
    stage: Path,
    refresh: bool,
    fetch_yahoo: FetchYahoo,
    fetch_fred: FetchFred,
    clock: Clock,
    manifest: dict,
):
    """Read cached files and download the rest into `stage`. Never writes to `cache`."""
    downloaded = False
    raw: dict[str, pd.Series] = {}
    adj: dict[str, pd.Series] = {}
    for ticker in tickers:
        path = cache / "yahoo" / f"{_safe(ticker)}.csv"
        if refresh or not path.exists():
            if downloaded:
                time.sleep(_PAUSE_SEC)
            df = _retry(ticker, lambda t=ticker: fetch_yahoo(t))
            if not isinstance(df, pd.DataFrame):
                raise DataError(f"{ticker}: fetcher returned no data frame")
            df, dropped = drop_unsettled_today(drop_incomplete_tail(df), ticker, clock())
            path = stage / "yahoo" / f"{_safe(ticker)}.csv"
            _write_yahoo(path, ticker, df)
            downloaded = True
            manifest["tickers"][ticker] = {
                "downloaded_at": _stamp(),
                "dropped_unsettled": dropped,
            }
        frame = _read_yahoo(path, ticker)
        raw[ticker] = frame["Close"]
        adj[ticker] = frame["Adj Close"]
        manifest["tickers"].setdefault(ticker, {}).update(_range(frame.index))

    fred: dict[str, pd.Series] = {}
    for sid in fred_ids:
        path = cache / "fred" / f"{_safe(sid)}.csv"
        if refresh or not path.exists():
            if downloaded:
                time.sleep(_PAUSE_SEC)
            s = _retry(sid, lambda i=sid: fetch_fred(i))
            path = stage / "fred" / f"{_safe(sid)}.csv"
            _write_fred(path, sid, s)
            downloaded = True
            manifest["fred"][sid] = {"downloaded_at": _stamp()}
        fred[sid] = _read_fred(path, sid)
        manifest["fred"].setdefault(sid, {}).update(_range(fred[sid].index))
    return raw, adj, fred, downloaded


def _commit_stage(stage: Path, cache: Path) -> None:
    """Move staged files into the cache, the manifest last."""
    if not stage.exists():
        return
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    files.sort(key=lambda p: p.name == "manifest.json")
    for src in files:
        dest = cache / src.relative_to(stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)


def load_market_data(
    tickers: list[str],
    fred_ids: list[str],
    cache_dir: str | Path,
    refresh: bool = False,
    fetch_yahoo: FetchYahoo | None = None,
    fetch_fred: FetchFred | None = None,
    now: Clock | None = None,
) -> MarketData:
    """Load every ticker and FRED series, downloading what is missing (or all if refresh).

    `now` is the clock used by the intraday guard (default: current UTC time). It must
    return a timezone-aware datetime; a naive one raises ValueError.
    """
    clock = now or _now
    cache = Path(cache_dir)
    fetch_yahoo = fetch_yahoo or _default_fetch_yahoo
    fetch_fred = fetch_fred or _default_fetch_fred
    manifest_path = cache / "manifest.json"
    old: dict = {}
    if manifest_path.exists() and not refresh:
        try:
            old = json.loads(manifest_path.read_text())
        except ValueError:
            old = {}
    manifest = {
        "yfinance_version": old.get("yfinance_version"),
        "downloaded_at": old.get("downloaded_at"),
        "tickers": dict(old.get("tickers", {})),
        "fred": dict(old.get("fred", {})),
    }

    # Downloads go to a temporary sibling folder and replace the cache only after every
    # ticker and series succeeded, so a failure leaves the old cache and manifest intact.
    stage = cache.with_name(cache.name + ".tmp")
    if stage.exists():
        shutil.rmtree(stage)
    try:
        raw, adj, fred, downloaded = _load_all(
            tickers, fred_ids, cache, stage, refresh, fetch_yahoo, fetch_fred, clock, manifest
        )
        if downloaded or not manifest_path.exists():
            manifest["yfinance_version"] = _yfinance_version()
            manifest["downloaded_at"] = _stamp()
            stage.mkdir(parents=True, exist_ok=True)
            (stage / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
        _commit_stage(stage, cache)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    raw_df = pd.DataFrame(raw, columns=list(tickers)).sort_index()
    adj_df = pd.DataFrame(adj, columns=list(tickers)).sort_index()
    return MarketData(raw_close=raw_df, adj_close=adj_df, fred=fred, manifest=manifest)

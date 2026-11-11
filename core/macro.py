"""
Macro data fetcher.

Pulls from two free sources:
  - FRED (St. Louis Fed): Treasury yields, fed funds, CPI, unemployment.
    https://fred.stlouisfed.org/  — no API key needed for the public CSV endpoint.
  - yfinance: VIX, DXY, credit-spread proxies (HYG/LQD). Not in FRED.

Cached per-process — re-fetches when older than `cache_ttl_sec`. Each fetcher
returns the last observation and a 60-day trailing window for trend signals.

The cache is intentionally simple — process-local, no Redis/SQLite. The core
sleeve only evaluates once per cycle (hourly at most), so we're nowhere near
hitting FRED's rate limits.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from core.config import MacroConfig

_FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_HTTP_TIMEOUT = 30.0


@dataclass
class MacroSnapshot:
    """One reading of the macro state, used by core/scoring.py."""

    # Yield curve: 10Y - 2Y, percent. Negative = inverted = recession signal.
    yield_curve: float | None
    yield_curve_trend_60d: float | None  # change vs 60 days ago, percentage points

    # Fed funds rate, percent
    fed_funds: float | None
    fed_funds_trend_60d: float | None

    # VIX level. >25 ~ stressed.
    vix: float | None
    vix_60d_mean: float | None  # 60-day average

    # Dollar index (DXY). Higher = stronger USD = headwind for commodities/EM.
    dxy: float | None
    dxy_trend_60d: float | None  # pct change vs 60d ago

    # Credit spread proxy: HYG/LQD ratio. Falling = spreads widening = risk-off.
    credit_ratio: float | None
    credit_ratio_trend_60d: float | None  # pct change vs 60d ago

    # When this snapshot was assembled
    as_of: datetime

    # Raw flag — True if every field is populated, False if any were missing
    complete: bool


class MacroFetcher:
    """One instance shared across the runner. Caches results per series."""

    def __init__(self, cfg: MacroConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, tuple[float, pd.Series]] = {}

    # ---- public API ----

    def snapshot(self) -> MacroSnapshot:
        """Pull every series and pack into a snapshot. Missing series -> None field."""

        def _get_fred(series_key: str) -> pd.Series | None:
            fid = self.cfg.fred_series.get(series_key)
            if not fid:
                return None
            try:
                return self._fred_series(fid)
            except Exception as e:
                print(f"[macro] FRED {series_key}={fid} fetch failed: {e}")
                return None

        def _get_yf(ticker_key: str) -> pd.Series | None:
            ticker = self.cfg.yf_tickers.get(ticker_key)
            if not ticker:
                return None
            try:
                return self._yf_series(ticker)
            except Exception as e:
                print(f"[macro] yfinance {ticker_key}={ticker} fetch failed: {e}")
                return None

        yc = _get_fred("yield_curve")
        ff = _get_fred("fed_funds")
        vix = _get_yf("vix")
        dxy = _get_yf("dxy")
        hyg = _get_yf("hyg")
        lqd = _get_yf("lqd")

        credit = None
        if hyg is not None and lqd is not None:
            # Align indexes and divide
            aligned = pd.concat([hyg, lqd], axis=1).dropna()
            if len(aligned) > 0:
                credit = aligned.iloc[:, 0] / aligned.iloc[:, 1]

        snap = MacroSnapshot(
            yield_curve=_last(yc),
            yield_curve_trend_60d=_pp_change_60d(yc),
            fed_funds=_last(ff),
            fed_funds_trend_60d=_pp_change_60d(ff),
            vix=_last(vix),
            vix_60d_mean=_window_mean(vix, 60),
            dxy=_last(dxy),
            dxy_trend_60d=_pct_change_60d(dxy),
            credit_ratio=_last(credit),
            credit_ratio_trend_60d=_pct_change_60d(credit),
            as_of=datetime.utcnow(),
            complete=all(s is not None and len(s) > 0 for s in (yc, ff, vix, dxy, hyg, lqd)),
        )
        return snap

    # ---- private fetchers ----

    def _fred_series(self, series_id: str) -> pd.Series:
        """Pull a daily FRED series. Caches for cfg.cache_ttl_sec."""
        cached = self._cached(f"fred:{series_id}")
        if cached is not None:
            return cached
        url = f"{_FRED_BASE}?{urlencode({'id': series_id})}"
        req = Request(url, headers={"User-Agent": "tradingbot/1.0"})
        with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(text))
        # Columns: 'observation_date' (or 'DATE') and the series id (or 'VALUE')
        date_col = next(
            (c for c in df.columns if c.lower() in {"date", "observation_date"}), df.columns[0]
        )
        val_col = next((c for c in df.columns if c != date_col), df.columns[-1])
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col])
        # FRED uses "." for missing values
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
        series = df.set_index(date_col)[val_col].dropna().sort_index()
        self._cache_put(f"fred:{series_id}", series)
        return series

    def _yf_series(self, ticker: str, lookback_days: int = 365) -> pd.Series:
        """
        Pull a daily close series from Yahoo's CSV endpoint. We use the chart
        endpoint (no auth) and parse JSON. yfinance package wraps this; we do it
        manually to avoid the dependency for a single use case.
        """
        cached = self._cached(f"yf:{ticker}")
        if cached is not None:
            return cached
        import json

        period2 = int(time.time())
        period1 = int(time.time() - lookback_days * 86400)
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?period1={period1}&period2={period2}&interval=1d&events=history"
        )
        req = Request(url, headers={"User-Agent": "tradingbot/1.0"})
        with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read())
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"yfinance returned no data for {ticker}")
        r = result[0]
        timestamps = r.get("timestamp") or []
        indicators = r.get("indicators", {}).get("quote", [{}])[0]
        closes = indicators.get("close") or []
        idx = pd.to_datetime(timestamps, unit="s")
        s = pd.Series(closes, index=idx, name=ticker).dropna()
        self._cache_put(f"yf:{ticker}", s)
        return s

    # ---- cache ----

    def _cached(self, key: str) -> pd.Series | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        fetched_at, series = entry
        if time.time() - fetched_at > self.cfg.cache_ttl_sec:
            return None
        return series

    def _cache_put(self, key: str, series: pd.Series) -> None:
        self._cache[key] = (time.time(), series)


# ---- helpers ----


def _last(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    return float(series.iloc[-1])


def _window_mean(series: pd.Series | None, n: int) -> float | None:
    if series is None or len(series) < n:
        return None
    return float(series.iloc[-n:].mean())


def _pct_change_60d(series: pd.Series | None) -> float | None:
    """Percent change vs ~60 days ago. None if not enough history."""
    if series is None or len(series) < 60:
        return None
    cur = float(series.iloc[-1])
    past = float(series.iloc[-60])
    if past == 0:
        return None
    return (cur - past) / past


def _pp_change_60d(series: pd.Series | None) -> float | None:
    """Absolute change in percentage points vs ~60 days ago (for rates)."""
    if series is None or len(series) < 60:
        return None
    return float(series.iloc[-1]) - float(series.iloc[-60])

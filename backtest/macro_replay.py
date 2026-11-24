"""Replay the live macro snapshot as of a past date.

`HistoricalMacroFetcher` subclasses the bot's `MacroFetcher` and overrides only
the two data-access methods, so `MacroFetcher.snapshot()` (credit ratio, the
60-row helpers, `MacroSnapshot` assembly) runs unchanged on cached series.

Because both fetch methods are overridden, the parent's TTL cache is never read
or written, so no per-date cache clearing is needed. `snapshot()` stamps
`as_of` with the wall clock (`datetime.utcnow()`); nothing in scoring reads it,
so replay results do not depend on it.

Timing rules:
- FRED T10Y2Y: only rows dated strictly before `t` (FRED posts day `t` on the
  next business day, so the live bot sees a one-day lag).
- Yahoo series: raw closes with `t - lookback_days < date <= t` (live keeps a
  365 calendar day window and ends at the latest close).

Nothing here imports yfinance or touches the network.
"""

from __future__ import annotations

import dataclasses

import pandas as pd

from core.config import MacroConfig
from core.macro import MacroFetcher

_EMPTY = pd.Series(dtype=float)


class HistoricalMacroFetcher(MacroFetcher):
    """MacroFetcher whose data methods serve cached series sliced as of a date."""

    def __init__(
        self,
        yahoo_close: pd.DataFrame,
        fred: dict[str, pd.Series],
        cfg: MacroConfig,
    ) -> None:
        super().__init__(cfg)
        self._yahoo = yahoo_close
        self._fred = fred
        self._asof: pd.Timestamp | None = None

    def set_asof(self, t) -> None:
        """Set the decision date. Only the date part is used."""
        self._asof = pd.Timestamp(t).normalize()

    def _require_asof(self) -> pd.Timestamp:
        if self._asof is None:
            raise RuntimeError("HistoricalMacroFetcher.set_asof() was not called")
        return self._asof

    def _fred_series(self, series_id: str) -> pd.Series:
        t = self._require_asof()
        s = self._fred.get(series_id)
        if s is None:
            return _EMPTY.copy()
        s = s.dropna().sort_index()
        return s[s.index < t]

    def _yf_series(self, ticker: str, lookback_days: int = 365) -> pd.Series:
        t = self._require_asof()
        if ticker not in self._yahoo.columns:
            return _EMPTY.copy()
        s = self._yahoo[ticker].dropna().sort_index()
        return s[(s.index > t - pd.Timedelta(days=lookback_days)) & (s.index <= t)]


def replay_macro_config(live: MacroConfig) -> MacroConfig:
    """Same Yahoo tickers as live; FRED reduced to the yield curve (D3)."""
    keep = {k: v for k, v in live.fred_series.items() if k == "yield_curve"}
    return dataclasses.replace(live, fred_series=keep)

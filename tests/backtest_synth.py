"""Tiny synthetic price frames with known answers for the backtest tests."""

from __future__ import annotations

import pandas as pd


def make_frames(
    returns: dict[str, list[float]],
    start: str = "2024-01-02",
    first_dates: dict[str, str] | None = None,
    base: float = 100.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build business-day (raw_close, adj_close) frames from per-symbol return lists.

    returns[s][i] is the return of day i versus day i-1 (entry 0 is ignored: day 0 is
    the base price). raw_close equals adj_close. A symbol listed in first_dates is NaN
    before that date.
    """
    n = max(len(r) for r in returns.values())
    index = pd.bdate_range(start, periods=n)
    adj = pd.DataFrame(index=index, dtype=float)
    for sym, rets in returns.items():
        price = base
        values = []
        for i in range(n):
            if i > 0:
                price *= 1.0 + rets[i]
            values.append(price)
        adj[sym] = values
    if first_dates:
        for sym, first in first_dates.items():
            adj.loc[adj.index < pd.Timestamp(first), sym] = float("nan")
    return adj.copy(), adj

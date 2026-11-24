"""Pure pandas/numpy performance metrics. No network, no yfinance, no matplotlib."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Drawdown:
    depth: float  # 0.0 or negative
    peak: Any  # index label of the last running max before the trough
    trough: Any  # index label of the first minimum
    recovery: Any  # first label after the trough with equity >= peak equity, else None


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def cagr(equity: pd.Series) -> float:
    n_returns = len(equity) - 1
    if n_returns < 1:
        raise ValueError("need at least two equity points")
    return float((equity.iloc[-1] / equity.iloc[0]) ** (TRADING_DAYS / n_returns) - 1.0)


def annual_volatility(returns: pd.Series) -> float:
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS))


def _excess(returns: pd.Series, rf: pd.Series) -> pd.Series:
    """Returns minus the risk-free series, aligned on the returns' index."""
    return returns - rf.reindex(returns.index)


def sharpe(returns: pd.Series, rf: pd.Series) -> float | None:
    x = _excess(returns, rf)
    std = x.std(ddof=1)
    if not std or math.isnan(std):
        return None
    return float(x.mean() / std * math.sqrt(TRADING_DAYS))


def sortino(returns: pd.Series, rf: pd.Series) -> float | None:
    x = _excess(returns, rf)
    downside = math.sqrt(float((np.minimum(x, 0.0) ** 2).mean()))
    if downside == 0.0 or math.isnan(downside):
        return None
    return float(x.mean() * TRADING_DAYS / (downside * math.sqrt(TRADING_DAYS)))


def max_drawdown(equity: pd.Series) -> Drawdown:
    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    depth = float(dd.min())
    if depth == 0.0:
        return Drawdown(0.0, None, None, None)
    trough_pos = int(dd.to_numpy().argmin())
    trough = equity.index[trough_pos]
    before = equity.iloc[: trough_pos + 1]
    peak_value = float(before.max())
    peak_pos = int(np.flatnonzero(before.to_numpy() == peak_value)[-1])
    peak = equity.index[peak_pos]
    after = equity.iloc[trough_pos + 1 :]
    regained = after[after >= peak_value]
    recovery = regained.index[0] if len(regained) else None
    return Drawdown(depth, peak, trough, recovery)


def calmar(cagr_value: float, depth: float) -> float | None:
    if depth == 0.0:
        return None
    return float(cagr_value / abs(depth))


def annual_turnover(turnover: pd.Series, n_returns: int) -> float:
    """One-way annual turnover, 1.0 = 100%."""
    return float(turnover.sum() / (n_returns / TRADING_DAYS))


def calendar_year_returns(equity: pd.Series) -> dict[int, float]:
    """Return per calendar year; the first and last years are usually partial."""
    last = equity.groupby(equity.index.year).last()
    out: dict[int, float] = {}
    prev = float(equity.iloc[0])
    for year, value in last.items():
        out[int(year)] = float(value / prev - 1.0)
        prev = float(value)
    return out


def _iso(label: Any) -> str | None:
    if label is None:
        return None
    return label.date().isoformat() if hasattr(label, "date") else str(label)


def summarize(
    equity: pd.Series,
    rf: pd.Series,
    turnover: pd.Series,
    risk_off: float | None = None,
) -> dict:
    returns = daily_returns(equity)
    growth = cagr(equity)
    dd = max_drawdown(equity)
    return {
        "cagr": growth,
        "volatility": annual_volatility(returns),
        "sharpe": sharpe(returns, rf),
        "sortino": sortino(returns, rf),
        "max_drawdown": dd.depth,
        "drawdown_peak": _iso(dd.peak),
        "drawdown_trough": _iso(dd.trough),
        "drawdown_recovery": _iso(dd.recovery),
        "calmar": calmar(growth, dd.depth),
        "annual_turnover": annual_turnover(turnover, len(returns)),
        "calendar_year_returns": calendar_year_returns(equity),
        "risk_off_share": risk_off,
    }

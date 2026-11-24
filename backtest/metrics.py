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
    """Two-way annual turnover (buys plus sells): a full switch counts as 2.0."""
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


BOOTSTRAP_BLOCK_DAYS = 63
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20240611


def _sharpe_rows(x: np.ndarray) -> np.ndarray:
    """Annualised Sharpe of each row of an excess-return matrix (NaN if a row is constant)."""
    std = x.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = x.mean(axis=1) / std * math.sqrt(TRADING_DAYS)
    return np.where(std > 0, out, np.nan)


def _interval(values: np.ndarray) -> tuple[float, float]:
    lo, hi = np.nanpercentile(values, [2.5, 97.5])
    return float(lo), float(hi)


def bootstrap_sharpe(
    strategy: pd.Series,
    benchmarks: dict[str, pd.Series],
    rf: pd.Series,
    block: int = BOOTSTRAP_BLOCK_DAYS,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Circular fixed-block bootstrap of daily excess returns.

    Every draw resamples whole `block`-day blocks (random start, wrapping around the
    end) and the same blocks are applied to the strategy and to every benchmark, so
    differences are paired. Reports the 95% percentile interval of the strategy Sharpe
    and of the Sharpe difference (strategy minus benchmark), and the share of draws in
    which the strategy Sharpe is strictly higher. Deterministic for a given seed.
    """
    cols = {"strategy": strategy, **benchmarks}
    frame = pd.concat(cols, axis=1).dropna()
    frame = frame.sub(rf.reindex(frame.index), axis=0).dropna()
    n = len(frame)
    if n < 2 * block:
        raise ValueError(f"need at least {2 * block} aligned returns, got {n}")
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n, size=(draws, n_blocks))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(draws, -1)[:, :n]
    values = frame.to_numpy(dtype=float)
    sharpes = {name: _sharpe_rows(values[:, j][idx]) for j, name in enumerate(frame.columns)}
    point = {
        name: float(frame[name].mean() / frame[name].std(ddof=1) * math.sqrt(TRADING_DAYS))
        for name in frame.columns
    }
    lo, hi = _interval(sharpes["strategy"])
    out: dict = {
        "block_days": block,
        "draws": draws,
        "seed": seed,
        "days": n,
        "strategy_sharpe": {"point": point["strategy"], "lo": lo, "hi": hi},
        "vs": {},
    }
    for name in benchmarks:
        diff = sharpes["strategy"] - sharpes[name]
        d_lo, d_hi = _interval(diff)
        out["vs"][name] = {
            "diff_point": point["strategy"] - point[name],
            "lo": d_lo,
            "hi": d_hi,
            "share_better": float(np.mean(diff[~np.isnan(diff)] > 0)),
        }
    return out


def subperiod_stats(
    returns: pd.Series, rf: pd.Series, periods: list[tuple[str, int, int]]
) -> dict[str, dict]:
    """CAGR (252 day basis) and Sharpe over calendar-year ranges (label, first, last year)."""
    out: dict[str, dict] = {}
    for label, first, last in periods:
        part = returns[(returns.index.year >= first) & (returns.index.year <= last)]
        if len(part) < 2:
            out[label] = {"days": int(len(part)), "cagr": None, "sharpe": None}
            continue
        growth = float((1.0 + part).prod() ** (TRADING_DAYS / len(part)) - 1.0)
        out[label] = {"days": int(len(part)), "cagr": growth, "sharpe": sharpe(part, rf)}
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

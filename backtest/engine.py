"""Daily portfolio loop, trading calendar, start-date rule and benchmark decide functions.

Offline and dependency-light: pandas and numpy only. No yfinance, no matplotlib.

Timing: a decision made at the close of day t is filled at the close of the next trading
day. Weights drift with prices between trades. Cost is `cost_bps / 10_000 * turnover` of
equity on the fill day, where turnover is `sum(|target - drifted|)` over assets (cash
excluded), so a first buy from cash is 1.0 and a full switch is 2.0 (buys plus sells). Cash earns 0.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core.config import CoreSleeveConfig

# Called at the close of t with post-fill weights; returns targets to fill at the next
# close, or None for no trade.
DecideFn = Callable[[pd.Timestamp, dict[str, float]], "dict[str, float] | None"]

_WEIGHT_TOL = 1e-9


@dataclass
class SimResult:
    equity: pd.Series  # starts at 1.0 on the first calendar day
    weights: pd.DataFrame  # post-fill weights per day (cash is 1 - row sum)
    turnover: pd.Series  # two-way turnover (buys plus sells) on fill days, else 0
    costs: pd.Series  # cost as a fraction of pre-cost equity on fill days, else 0
    fills: list[tuple[pd.Timestamp, dict[str, float]]] = field(default_factory=list)


def simulate(
    adj_close: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    decide: DecideFn,
    cost_bps: float,
) -> SimResult:
    """Run the daily loop over `calendar` using adjusted closes for returns."""
    if len(calendar) < 1:
        raise ValueError("empty calendar")
    px = adj_close.reindex(calendar)
    symbols = list(px.columns)
    rate = cost_bps / 10_000.0

    equity = np.ones(len(calendar))
    weights = np.zeros((len(calendar), len(symbols)))
    turnover = np.zeros(len(calendar))
    costs = np.zeros(len(calendar))
    fills: list[tuple[pd.Timestamp, dict[str, float]]] = []

    w = np.zeros(len(symbols))
    pending = decide(calendar[0], {})

    for i in range(1, len(calendar)):
        t = calendar[i]
        # (1) drift: grow each weight by its price relative, renormalise by new equity
        held = w > 0.0
        if held.any():
            ratio = (px.iloc[i].to_numpy(dtype=float) / px.iloc[i - 1].to_numpy(dtype=float))[held]
            bad = np.isnan(ratio) | ~np.isfinite(ratio)
            if bad.any():
                sym = np.array(symbols)[held][bad][0]
                raise ValueError(
                    f"missing or invalid adjusted close for held symbol {sym} on {t.date()}"
                )
            values = np.zeros(len(symbols))
            values[held] = w[held] * ratio
            growth = values.sum() + (1.0 - w.sum())
            equity[i] = equity[i - 1] * growth
            w = values / growth
        else:
            equity[i] = equity[i - 1]

        # (2) fill the pending decision at this close
        if pending is not None:
            target = np.zeros(len(symbols))
            for sym, weight in pending.items():
                if sym not in symbols:
                    raise ValueError(f"target symbol {sym} is not in adj_close")
                target[symbols.index(sym)] = weight
            if target.sum() > 1.0 + _WEIGHT_TOL or (target < -_WEIGHT_TOL).any():
                raise ValueError(f"invalid target weights on {t.date()}: {pending}")
            turn = float(np.abs(target - w).sum())
            cost = rate * turn
            equity[i] *= 1.0 - cost
            turnover[i] = turn
            costs[i] = cost
            w = target
            fills.append((t, {s: float(x) for s, x in pending.items() if x != 0.0}))

        weights[i] = w
        # (3) decide at this close with post-fill weights
        pending = decide(t, {s: float(x) for s, x in zip(symbols, w, strict=True) if x > 0.0})

    return SimResult(
        equity=pd.Series(equity, index=calendar, name="equity"),
        weights=pd.DataFrame(weights, index=calendar, columns=symbols),
        turnover=pd.Series(turnover, index=calendar, name="turnover"),
        costs=pd.Series(costs, index=calendar, name="cost"),
        fills=fills,
    )


def trading_calendar(
    raw_close: pd.DataFrame, adj_close: pd.DataFrame, symbols: list[str]
) -> pd.DatetimeIndex:
    """Dates on which every symbol has both a raw and an adjusted close."""
    index = raw_close.index.union(adj_close.index)
    ok = pd.Series(True, index=index)
    for sym in symbols:
        for frame in (raw_close, adj_close):
            if sym not in frame.columns:
                return pd.DatetimeIndex([])
            ok &= frame[sym].notna().reindex(index, fill_value=False)
    return pd.DatetimeIndex(index[ok.to_numpy()]).sort_values()


def min_bars_for(cfg: CoreSleeveConfig) -> int:
    """Raw closes the sleeve needs: momentum window plus one, trend MA, risk-off MA."""
    return max(cfg.momentum_lookback_days + 1, cfg.trend_ma_days, cfg.risk_off_spy_ma_days)


def first_start_date(
    raw_close: pd.DataFrame,
    symbols: list[str],
    min_bars: int,
    calendar: pd.DatetimeIndex,
) -> pd.Timestamp:
    """First calendar day on which every symbol has >= min_bars raw closes up to that day."""
    counts = pd.DataFrame(index=calendar)
    for sym in symbols:
        if sym in raw_close.columns:
            cum = raw_close[sym].notna().cumsum()
            counts[sym] = cum.reindex(calendar, method="ffill").fillna(0)
        else:
            counts[sym] = 0
    ok = (counts >= min_bars).all(axis=1)
    if not ok.any():
        short = [
            s
            for s in symbols
            if s not in raw_close.columns or raw_close[s].notna().sum() < min_bars
        ]
        if not short:
            short = list(symbols)
        raise ValueError(
            f"no date has {min_bars} bars for every ticker; short tickers: {', '.join(short)}"
        )
    return pd.Timestamp(ok.index[ok.to_numpy()][0])


def buy_and_hold(symbol: str) -> DecideFn:
    """Buy `symbol` with everything on the first call, never trade again."""
    state = {"done": False}

    def decide(t: pd.Timestamp, weights: dict[str, float]) -> dict[str, float] | None:
        if state["done"]:
            return None
        state["done"] = True
        return {symbol: 1.0}

    return decide


def monthly_rebalance(weights: dict[str, float], calendar: pd.DatetimeIndex) -> DecideFn:
    """Decide on the first call and on every day whose next trading day is in a new month."""
    target = dict(weights)
    pos = {t: i for i, t in enumerate(calendar)}
    state = {"first": True}

    def decide(t: pd.Timestamp, current: dict[str, float]) -> dict[str, float] | None:
        if state["first"]:
            state["first"] = False
            return dict(target)
        i = pos[t]
        if i + 1 >= len(calendar):
            return None
        nxt = calendar[i + 1]
        if (nxt.year, nxt.month) != (t.year, t.month):
            return dict(target)
        return None

    return decide

"""
Portfolio sizing: vol-target × signal-weight, with per-symbol and gross exposure caps.

Pure functions, no I/O. The portfolio runner calls `size_portfolio()` once per cycle
after gathering per-symbol predictions; the result is a dict of target share counts.

The sizing decision for one symbol is:

    if prediction not in bullish_classes or confidence < confidence_floor:
        target = 0
    else:
        # signal weight ∈ [0, 1]: 0 at coin-flip, 1 at perfect confidence
        signal_w = max(0, (confidence - confidence_floor) / (1 - confidence_floor))

        # vol-target: bigger size on quiet symbols, smaller on noisy ones
        vol_scale = target_vol_per_bar / max(realized_vol, eps)
        vol_scale = clamp(vol_scale, 0.1, 5.0)     # sanity bounds

        raw_value = equity * vol_scale * signal_w * per_symbol_weight
        raw_value = min(raw_value, equity * max_per_symbol_pct)
        target = int(raw_value / close)            # shares (integer)

After per-symbol sizing, if the sum of position values exceeds `equity * max_gross_pct`,
all positions are scaled down proportionally so gross exposure fits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


# Predictions considered "bullish enough to open a long". Multiclass: 2 = uptrend,
# Binary: 1 = uptrend.
BULLISH_CLASSES: frozenset[int] = frozenset({1, 2})


@dataclass
class SymbolSignal:
    """One symbol's snapshot at decision time."""
    symbol: str
    prediction: int
    confidence: float
    close: float
    atr: float
    realized_vol: float       # std of pct returns over lookback bars


@dataclass
class SizingDecision:
    symbol: str
    target_shares: int
    position_value: float
    signal_weight: float
    vol_scale: float
    realized_vol: float
    reason: str


@dataclass
class PortfolioParams:
    starting_equity: float = 100_000.0
    target_vol_per_bar: float = 0.005       # 0.5 % target per-symbol vol per bar
    vol_lookback: int = 20                   # bars used to compute realized vol
    max_per_symbol_pct: float = 0.30         # 30 % equity max per name
    max_gross_pct: float = 1.0               # 100 % gross exposure max
    confidence_floor: float = 0.55           # below this confidence, no position
    vol_scale_min: float = 0.1
    vol_scale_max: float = 5.0


def realized_vol(closes: pd.Series, lookback: int) -> float:
    """Std of percent returns over the last `lookback` bars."""
    if len(closes) < lookback + 1:
        return float("nan")
    returns = closes.pct_change().tail(lookback)
    return float(returns.std(ddof=0))


def _size_one(
    sig: SymbolSignal,
    equity: float,
    params: PortfolioParams,
    per_symbol_weight: float = 1.0,
) -> SizingDecision:
    if sig.prediction not in BULLISH_CLASSES:
        return SizingDecision(
            symbol=sig.symbol, target_shares=0, position_value=0.0,
            signal_weight=0.0, vol_scale=0.0, realized_vol=sig.realized_vol,
            reason=f"prediction={sig.prediction} not bullish",
        )

    if sig.confidence < params.confidence_floor:
        return SizingDecision(
            symbol=sig.symbol, target_shares=0, position_value=0.0,
            signal_weight=0.0, vol_scale=0.0, realized_vol=sig.realized_vol,
            reason=f"confidence {sig.confidence:.3f} < floor {params.confidence_floor}",
        )

    # signal weight: linearly maps [floor, 1.0] → [0, 1]
    signal_w = (sig.confidence - params.confidence_floor) / (1.0 - params.confidence_floor)
    signal_w = max(0.0, min(1.0, signal_w))

    # vol scale: cheap symbols (lower realized vol) get more capital
    vol = max(sig.realized_vol, 1e-6) if np.isfinite(sig.realized_vol) else float("nan")
    if not np.isfinite(vol) or vol <= 0:
        return SizingDecision(
            symbol=sig.symbol, target_shares=0, position_value=0.0,
            signal_weight=signal_w, vol_scale=0.0, realized_vol=sig.realized_vol,
            reason="realized_vol unavailable",
        )

    vol_scale = params.target_vol_per_bar / vol
    vol_scale = max(params.vol_scale_min, min(params.vol_scale_max, vol_scale))

    raw_value = equity * vol_scale * signal_w * per_symbol_weight
    capped_value = min(raw_value, equity * params.max_per_symbol_pct)

    shares = int(capped_value / max(sig.close, 1e-6))
    final_value = shares * sig.close

    return SizingDecision(
        symbol=sig.symbol,
        target_shares=shares,
        position_value=final_value,
        signal_weight=signal_w,
        vol_scale=vol_scale,
        realized_vol=sig.realized_vol,
        reason="sized" if shares > 0 else "rounded to 0 shares",
    )


def size_portfolio(
    signals: Iterable[SymbolSignal],
    equity: float,
    params: PortfolioParams,
) -> list[SizingDecision]:
    """
    Size a portfolio in one shot.
    Returns one SizingDecision per input signal (same order).
    """
    decisions = [_size_one(s, equity, params) for s in signals]

    gross = sum(d.position_value for d in decisions)
    gross_cap = equity * params.max_gross_pct
    if gross <= gross_cap or gross <= 0:
        return decisions

    # Scale down proportionally so total fits the gross cap.
    scale = gross_cap / gross
    scaled: list[SizingDecision] = []
    for d, sig in zip(decisions, signals):
        if d.target_shares == 0:
            scaled.append(d)
            continue
        new_value = d.position_value * scale
        new_shares = int(new_value / max(sig.close, 1e-6))
        scaled.append(SizingDecision(
            symbol=d.symbol,
            target_shares=new_shares,
            position_value=new_shares * sig.close,
            signal_weight=d.signal_weight,
            vol_scale=d.vol_scale,
            realized_vol=d.realized_vol,
            reason=f"{d.reason}; gross-capped ×{scale:.3f}",
        ))
    return scaled

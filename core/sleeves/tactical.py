"""
Tactical sleeve — short/mid-term, signal-driven.

Each cycle:
  1. For each symbol in the sleeve's universe, fetch the latest TA prediction
     (from the runner) + latest sentiment (from SQLite).
  2. Fuse them via core.signal_fusion.fuse().
  3. Apply the existing vol-target × signal-weight sizing from core.sizing,
     but scoped to this sleeve's equity budget (not total).
  4. Return sizing decisions per symbol.

The sleeve doesn't fetch market data — the runner does. The sleeve is pure
decision logic that takes a `TacticalInput` and returns `TacticalDecision`s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.config import TacticalSleeveConfig
from core.signal_fusion import FusedSignal, fuse
from core.sizing import PortfolioParams, SizingDecision, SymbolSignal, size_portfolio


@dataclass
class TacticalInput:
    """One symbol's snapshot at decision time, pre-fusion."""

    symbol: str
    ta_prediction: int
    ta_confidence: float
    close: float
    atr: float
    realized_vol: float
    sentiment_score: float | None  # None if no fresh sentiment


@dataclass
class TacticalDecision:
    symbol: str
    fused: FusedSignal
    sizing: SizingDecision
    sleeve_equity: float  # equity allocated to this sleeve at decision time


class TacticalSleeve:
    name = "tactical"

    def __init__(self, cfg: TacticalSleeveConfig) -> None:
        self.cfg = cfg

    def decide(
        self,
        inputs: Iterable[TacticalInput],
        sleeve_equity: float,
    ) -> list[TacticalDecision]:
        inputs = list(inputs)
        # 1) fuse TA + sentiment per symbol
        fused_by_sym: dict[str, FusedSignal] = {}
        for inp in inputs:
            fused_by_sym[inp.symbol] = fuse(
                ta_prediction=inp.ta_prediction,
                ta_confidence=inp.ta_confidence,
                sentiment_score=inp.sentiment_score,
                ta_weight=self.cfg.ta_weight,
                sentiment_weight=self.cfg.sentiment_weight,
            )

        # 2) translate fused outputs to SymbolSignal-compatible structs the sizer wants.
        #    The sizer uses `confidence` for the signal weight ramp; we hand it
        #    `combined_confidence`.
        signals: list[SymbolSignal] = []
        for inp in inputs:
            f = fused_by_sym[inp.symbol]
            signals.append(
                SymbolSignal(
                    symbol=inp.symbol,
                    prediction=inp.ta_prediction,  # sizer checks BULLISH_CLASSES
                    confidence=f.combined_confidence,
                    close=inp.close,
                    atr=inp.atr,
                    realized_vol=inp.realized_vol,
                )
            )

        # 3) size with this sleeve's own equity + caps
        params = PortfolioParams(
            starting_equity=sleeve_equity,
            target_vol_per_bar=self.cfg.target_vol_per_bar,
            vol_lookback=self.cfg.vol_lookback,
            max_per_symbol_pct=self.cfg.max_per_symbol_pct,
            max_gross_pct=self.cfg.max_gross_pct,
            confidence_floor=self.cfg.confidence_floor,
        )
        sizings = size_portfolio(signals, equity=sleeve_equity, params=params)

        return [
            TacticalDecision(
                symbol=inp.symbol,
                fused=fused_by_sym[inp.symbol],
                sizing=sz,
                sleeve_equity=sleeve_equity,
            )
            for inp, sz in zip(inputs, sizings)
        ]

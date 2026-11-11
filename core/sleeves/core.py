"""
Core sleeve — medium-term (1–3 month) macro+TA portfolio.

Evaluation cadence: hourly cycles, but only ACTS on the configured rebalance
day-of-week (Friday close by default) OR when any existing holding has
drifted >drift_threshold from its target weight.

Decision flow per evaluation:
  1. Score every asset in the universe (core.scoring.score_universe)
  2. If risk-off filter triggered → target = equal-weight in risk_off_universe
     Otherwise → target = equal-weight top hold_top_n by composite score
  3. Compare to current holdings; emit BUY/SELL deltas to reach target

The sleeve does NOT fetch data — the runner does. It receives a closes-dict
+ MacroSnapshot + current holdings and returns a rebalance plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

import pandas as pd

from core.config import CoreSleeveConfig
from core.macro import MacroSnapshot
from core.scoring import AssetScore, score_universe, select_holdings


@dataclass
class CoreDecision:
    """One symbol's piece of a rebalance plan."""

    symbol: str
    target_weight: float
    target_value: float
    target_shares: int
    current_shares: int
    delta_shares: int  # +ve = buy, -ve = sell, 0 = hold
    close_price: float
    score: AssetScore | None  # None for risk-off bond positions not in scoring
    reason: str


@dataclass
class CoreCycleResult:
    triggered: bool
    reason: str
    risk_off: bool
    scores: list[AssetScore]
    decisions: list[CoreDecision]


class CoreSleeve:
    name = "core"

    def __init__(self, cfg: CoreSleeveConfig) -> None:
        self.cfg = cfg
        if abs(cfg.momentum_weight + cfg.macro_weight + cfg.trend_weight - 1.0) > 0.01:
            raise ValueError(
                "Core sleeve weights must sum to 1.0, got "
                f"{cfg.momentum_weight + cfg.macro_weight + cfg.trend_weight:.3f}"
            )

    # ---- trigger logic ----

    def should_evaluate(
        self,
        *,
        last_rebalance_ts: datetime | None,
        current_holdings: Mapping[str, int],
        prices: Mapping[str, float],
        sleeve_equity: float,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Should we make a decision THIS cycle?
        Triggers: (a) it's the configured rebalance day-of-week and we haven't
        rebalanced today, OR (b) some holding has drifted past drift_threshold.
        """
        now = now or datetime.now(timezone.utc)

        # Drift check — always evaluate if anything is off-target
        drift = self._max_drift(current_holdings, prices, sleeve_equity)
        if drift is not None and drift[1] > self.cfg.drift_threshold:
            sym, mag = drift
            return True, f"drift: {sym} off by {mag:.1%} (>{self.cfg.drift_threshold:.0%})"

        # Calendar trigger
        if now.weekday() != self.cfg.rebalance_day_of_week:
            return (
                False,
                f"not rebalance day (today={now.strftime('%a')}, want={_day_name(self.cfg.rebalance_day_of_week)})",
            )

        # Don't rebalance more than once per calendar day
        if last_rebalance_ts is not None and last_rebalance_ts.date() == now.date():
            return False, "already rebalanced today"

        return True, f"calendar: {_day_name(self.cfg.rebalance_day_of_week)} close"

    def _max_drift(
        self,
        holdings: Mapping[str, int],
        prices: Mapping[str, float],
        sleeve_equity: float,
    ) -> tuple[str, float] | None:
        """Largest absolute drift of any held symbol from its CURRENT target weight.
        For the core sleeve, current target depends on what's held. We approximate:
        each held symbol's target is `1 / N_held`. So drift = |held_w - 1/N|."""
        if sleeve_equity <= 0 or not holdings:
            return None
        n_held = sum(1 for s, sh in holdings.items() if sh > 0)
        if n_held == 0:
            return None
        target_w = 1.0 / n_held
        worst: tuple[str, float] | None = None
        for sym, shares in holdings.items():
            if shares <= 0:
                continue
            value = shares * prices.get(sym, 0.0)
            current_w = value / sleeve_equity
            mag = abs(current_w - target_w)
            if worst is None or mag > worst[1]:
                worst = (sym, mag)
        return worst

    # ---- decision logic ----

    def evaluate(
        self,
        *,
        closes: Mapping[str, pd.Series],
        macro: MacroSnapshot,
        current_holdings: Mapping[str, int],
        sleeve_equity: float,
        reason: str,
    ) -> CoreCycleResult:
        """
        Score the universe, pick target weights, compute deltas vs current.
        Caller decides whether to actually place orders (paper/live or dry).
        """
        result = score_universe(closes, macro, self.cfg)
        target_weights = select_holdings(result, self.cfg)
        decisions: list[CoreDecision] = []

        # All symbols we need to think about: current holdings + target symbols
        all_symbols = set(current_holdings) | set(target_weights)
        scores_by_sym = {s.symbol: s for s in result.scores}

        for sym in sorted(all_symbols):
            target_w = target_weights.get(sym, 0.0)
            close = _last(closes.get(sym))
            if close is None or close <= 0:
                continue
            target_value = sleeve_equity * target_w
            target_shares = int(target_value / close)
            current = current_holdings.get(sym, 0)
            decisions.append(
                CoreDecision(
                    symbol=sym,
                    target_weight=target_w,
                    target_value=target_shares * close,
                    target_shares=target_shares,
                    current_shares=current,
                    delta_shares=target_shares - current,
                    close_price=close,
                    score=scores_by_sym.get(sym),
                    reason=(
                        "risk-off bond bucket"
                        if (result.risk_off and target_w > 0)
                        else (
                            f"top {self.cfg.hold_top_n} by score"
                            if target_w > 0
                            else "out of top set"
                        )
                    ),
                )
            )

        return CoreCycleResult(
            triggered=True,
            reason=reason,
            risk_off=result.risk_off,
            scores=result.scores,
            decisions=decisions,
        )


# ---- helpers ----


def _last(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    return float(series.iloc[-1])


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _day_name(idx: int) -> str:
    return _DAY_NAMES[idx % 7]

"""
Long-term sleeve — All-Weather fixed-weight allocation.

Strategy:
  - Each symbol has a target weight (config: long_term.universe).
  - The sleeve has its own equity budget (`allocation_pct` of total).
  - We rebalance when EITHER condition triggers:
      a) more than `rebalance_freq_days` since last rebalance, OR
      b) any current holding has drifted > `drift_threshold` from its target weight.
  - Rebalance produces a list of buy/sell orders to bring positions back to target.
  - Between rebalances, the sleeve does nothing — that's the whole point.

The sleeve does NOT fetch data. The runner passes in current prices + current
holdings. The sleeve returns target shares per symbol and a list of trades
needed to get there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from core.config import LongTermSleeveConfig


@dataclass
class LongTermDecision:
    """One symbol's piece of a rebalance plan."""

    symbol: str
    target_weight: float
    target_value: float
    target_shares: int
    current_shares: int
    delta_shares: int  # positive → buy, negative → sell
    close_price: float
    reason: str


@dataclass
class RebalanceResult:
    triggered: bool
    reason: str
    decisions: list[LongTermDecision]


class AllWeatherSleeve:
    name = "long_term"

    def __init__(self, cfg: LongTermSleeveConfig) -> None:
        self.cfg = cfg
        if abs(sum(cfg.universe.values()) - 1.0) > 0.01:
            raise ValueError(
                f"long-term target weights must sum to 1.0, got "
                f"{sum(cfg.universe.values()):.4f}: {cfg.universe}"
            )

    # ----- triggers -----

    def should_rebalance(
        self,
        *,
        last_rebalance_ts: datetime | None,
        current_holdings: Mapping[str, int],
        prices: Mapping[str, float],
        sleeve_equity: float,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Decide whether to fire a rebalance THIS cycle and why."""
        now = now or datetime.now(timezone.utc)

        # Time trigger
        if last_rebalance_ts is None:
            return True, "first run — no prior rebalance"
        age = now - last_rebalance_ts
        if age >= timedelta(days=self.cfg.rebalance_freq_days):
            return True, f"calendar — {age.days}d since last rebalance"

        # Drift trigger
        drift = self._max_drift(current_holdings, prices, sleeve_equity)
        if drift is not None and drift[1] > self.cfg.drift_threshold:
            sym, mag = drift
            return True, f"drift — {sym} off target by {mag:.1%} (>{self.cfg.drift_threshold:.0%})"

        return False, "no trigger"

    def _max_drift(
        self,
        holdings: Mapping[str, int],
        prices: Mapping[str, float],
        sleeve_equity: float,
    ) -> tuple[str, float] | None:
        if sleeve_equity <= 0:
            return None
        worst: tuple[str, float] | None = None
        for sym, target_w in self.cfg.universe.items():
            current_value = holdings.get(sym, 0) * prices.get(sym, 0.0)
            current_w = current_value / sleeve_equity
            mag = abs(current_w - target_w)
            if worst is None or mag > worst[1]:
                worst = (sym, mag)
        return worst

    # ----- plan -----

    def plan_rebalance(
        self,
        *,
        prices: Mapping[str, float],
        current_holdings: Mapping[str, int],
        sleeve_equity: float,
    ) -> list[LongTermDecision]:
        """Compute target shares per symbol + delta from current."""
        decisions: list[LongTermDecision] = []
        for sym, weight in self.cfg.universe.items():
            close = prices.get(sym, 0.0)
            if close <= 0:
                # No price → we can't size. Skip but still emit a record.
                decisions.append(
                    LongTermDecision(
                        symbol=sym,
                        target_weight=weight,
                        target_value=0.0,
                        target_shares=0,
                        current_shares=current_holdings.get(sym, 0),
                        delta_shares=0,
                        close_price=0.0,
                        reason="missing price",
                    )
                )
                continue
            target_value = sleeve_equity * weight
            target_shares = int(target_value / close)
            current = current_holdings.get(sym, 0)
            decisions.append(
                LongTermDecision(
                    symbol=sym,
                    target_weight=weight,
                    target_value=target_shares * close,
                    target_shares=target_shares,
                    current_shares=current,
                    delta_shares=target_shares - current,
                    close_price=close,
                    reason=f"target {weight:.1%} of ${sleeve_equity:,.0f}",
                )
            )
        return decisions

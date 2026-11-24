"""Ablation variants: config overrides and a cost rate, nothing else.

Each variant changes only fields of the bot's own `CoreSleeveConfig` (via
`dataclasses.replace`) and the cost rate passed to the simulator. No bot code is
changed or patched. `no_risk_off` works because `_check_risk_off` tests
`macro.vix > cfg.risk_off_vix_threshold`, which is never true for infinity.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from core.config import CoreSleeveConfig

DEFAULT_COST_BPS = 5.0

_WEIGHT_FIELDS = ("momentum_weight", "macro_weight", "trend_weight")


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    overrides: dict = field(default_factory=dict)
    cost_bps: float = DEFAULT_COST_BPS


VARIANTS: list[Variant] = [
    Variant("base", "Live config, 5 bps cost."),
    Variant(
        "momentum_only",
        "Momentum weight 1.0, macro and trend 0.",
        {"momentum_weight": 1.0, "macro_weight": 0.0, "trend_weight": 0.0},
    ),
    Variant(
        "no_macro",
        "Macro weight 0, momentum and trend renormalised proportionally.",
        {"macro_weight": 0.0},
    ),
    Variant(
        "no_risk_off",
        "Risk-off switch disabled (VIX threshold set to infinity).",
        {"risk_off_vix_threshold": float("inf")},
    ),
    Variant("top_3", "Hold the top 3 instead of the top 4.", {"hold_top_n": 3}),
    Variant("top_5", "Hold the top 5 instead of the top 4.", {"hold_top_n": 5}),
    Variant("cost_0", "Live config, 0 bps cost.", cost_bps=0.0),
    Variant("cost_20", "Live config, 20 bps cost.", cost_bps=20.0),
]


def build_cfg(base: CoreSleeveConfig, variant: Variant) -> CoreSleeveConfig:
    """Return `base` with the variant's overrides applied (base is not mutated).

    If only some of the three score weights are overridden, the others are scaled
    proportionally (computed from `base`) so that all three sum to 1.
    """
    overrides = dict(variant.overrides)
    given = [f for f in _WEIGHT_FIELDS if f in overrides]
    if given and len(given) < len(_WEIGHT_FIELDS):
        rest = [f for f in _WEIGHT_FIELDS if f not in overrides]
        old_rest = sum(getattr(base, f) for f in rest)
        new_rest = 1.0 - sum(overrides[f] for f in given)
        for f in rest:
            overrides[f] = getattr(base, f) / old_rest * new_rest
    return dataclasses.replace(base, **overrides)

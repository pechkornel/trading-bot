"""
Composite score for the core (medium-term) sleeve.

For each asset we compute three sub-scores in [-1, 1] and combine them with
configurable weights:

  momentum      — 12-month total return, ranked cross-sectionally vs the
                  universe. The asset with the best 12m return scores +1,
                  worst scores -1, median scores 0.

  macro_alignment — does the current macro regime favor this asset class?
                    Computed from the MacroSnapshot using simple rules per
                    asset bucket (equity, bond, commodity, cash). Range [-1, 1].

  trend_strength — is the asset above its 200-day MA and how convincingly?
                   +1 if comfortably above, -1 if comfortably below.

Final composite score is the weighted sum. Higher = more attractive.

Two design choices worth knowing:
  - Momentum is RELATIVE (rank-based), so the score depends on the universe
    you're ranking against. Adding a new asset shifts every other score.
  - Macro is ABSOLUTE per asset class, so it's stable as you change universe.

The core sleeve picks the top N by composite score and equal-weights them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from core.config import CoreSleeveConfig
from core.macro import MacroSnapshot


# ---- asset classification ----
# Each ETF in the core universe belongs to one of these buckets. Used by the
# macro_alignment scoring rules.

ASSET_BUCKET: dict[str, str] = {
    "SPY": "equity_us_large",
    "IWM": "equity_us_small",
    "EFA": "equity_intl_dev",
    "VWO": "equity_intl_em",
    "QQQ": "equity_us_large",  # treated like SPY for macro purposes
    "TLT": "bond_long",
    "IEF": "bond_inter",
    "AGG": "bond_broad",
    "GLD": "alt_gold",
    "DBC": "alt_commodity",
    "BIL": "cash",
}


@dataclass
class AssetScore:
    symbol: str
    momentum: float  # [-1, 1]
    macro: float  # [-1, 1]
    trend: float  # [-1, 1]
    composite: float  # weighted sum
    bucket: str
    reason: str  # human-readable breakdown


@dataclass
class ScoringResult:
    scores: list[AssetScore]
    risk_off: bool  # True if hard risk-off filter triggered
    risk_off_reason: str | None


# ---- public API ----


def score_universe(
    closes: Mapping[str, pd.Series],
    macro: MacroSnapshot,
    cfg: CoreSleeveConfig,
) -> ScoringResult:
    """
    Score every symbol in cfg.universe.

    `closes` is a dict symbol → daily close series (oldest → newest).
    Must include SPY (for the risk-off filter) and every cfg.universe symbol.

    Returns scores sorted descending by composite, plus the risk-off flag.
    """
    # ---- Risk-off check first ----
    risk_off, risk_off_reason = _check_risk_off(closes, macro, cfg)

    # ---- Cross-sectional momentum ranks ----
    raw_momenta = {}
    for sym in cfg.universe:
        s = closes.get(sym)
        if s is None or len(s) < cfg.momentum_lookback_days + 1:
            raw_momenta[sym] = None
            continue
        cur = float(s.iloc[-1])
        past = float(s.iloc[-cfg.momentum_lookback_days - 1])
        if past <= 0:
            raw_momenta[sym] = None
            continue
        raw_momenta[sym] = (cur - past) / past

    momentum_ranks = _rank_to_signed(raw_momenta)

    # ---- Per-asset scores ----
    out: list[AssetScore] = []
    for sym in cfg.universe:
        bucket = ASSET_BUCKET.get(sym, "unknown")
        mom = momentum_ranks.get(sym, 0.0)
        macro_score = _macro_score(bucket, macro)
        trend_score = _trend_score(closes.get(sym), cfg.trend_ma_days)

        composite = (
            cfg.momentum_weight * mom
            + cfg.macro_weight * macro_score
            + cfg.trend_weight * trend_score
        )
        reason = (
            f"mom={mom:+.2f}×{cfg.momentum_weight} "
            f"macro={macro_score:+.2f}×{cfg.macro_weight} "
            f"trend={trend_score:+.2f}×{cfg.trend_weight} "
            f"= {composite:+.3f}"
        )
        out.append(
            AssetScore(
                symbol=sym,
                momentum=mom,
                macro=macro_score,
                trend=trend_score,
                composite=composite,
                bucket=bucket,
                reason=reason,
            )
        )

    out.sort(key=lambda s: s.composite, reverse=True)
    return ScoringResult(scores=out, risk_off=risk_off, risk_off_reason=risk_off_reason)


def select_holdings(result: ScoringResult, cfg: CoreSleeveConfig) -> dict[str, float]:
    """
    Translate the score result into target weights.

    If risk_off → 1/N split across cfg.risk_off_universe.
    Otherwise → equal-weight top cfg.hold_top_n by composite.
    """
    if result.risk_off:
        names = cfg.risk_off_universe
    else:
        names = [s.symbol for s in result.scores[: cfg.hold_top_n]]

    if not names:
        return {}
    w = 1.0 / len(names)
    return {n: w for n in names}


# ---- macro alignment rules ----


def _macro_score(bucket: str, m: MacroSnapshot) -> float:
    """
    Per-bucket macro alignment in [-1, 1].

    Rules are intentionally simple — these are well-known regime heuristics, not
    optimized parameters. Tune in `core/scoring.py:_macro_score` if you want to
    bias toward a particular asset class.
    """
    if not m.complete and m.vix is None and m.yield_curve is None:
        # Almost no macro data → neutral
        return 0.0

    score = 0.0
    components = 0

    # Yield curve: positive (steepening) favors equities + commodities; inverted favors long bonds
    if m.yield_curve is not None:
        if bucket.startswith("equity") or bucket.startswith("alt_commodity"):
            score += _norm(m.yield_curve, -1.0, 2.0)  # -1 inverted, +1 steep
        elif bucket == "bond_long":
            score += _norm(-m.yield_curve, -1.0, 2.0)  # inversion = bond bid
        components += 1

    # VIX: high VIX favors bonds, gold, cash; hurts equity
    if m.vix is not None:
        # Center on 18, scale by 12: VIX=18 → 0, VIX=30 → +1, VIX=12 → -0.5
        vix_sig = _clip((m.vix - 18.0) / 12.0)
        if bucket.startswith("equity"):
            score += -vix_sig
        elif bucket in ("bond_long", "bond_inter", "bond_broad", "alt_gold", "cash"):
            score += vix_sig
        components += 1

    # Dollar (DXY) trend: weak dollar favors commodities + EM, hurts US large
    if m.dxy_trend_60d is not None:
        dxy_sig = _clip(m.dxy_trend_60d * 10.0)  # 10% move → full signal
        if bucket in ("alt_commodity", "alt_gold", "equity_intl_em", "equity_intl_dev"):
            score += -dxy_sig  # weak dollar good
        elif bucket == "equity_us_large":
            score += dxy_sig  # strong dollar OK for USD-earners
        components += 1

    # Credit spreads (HYG/LQD ratio falling = risk-off)
    if m.credit_ratio_trend_60d is not None:
        credit_sig = _clip(m.credit_ratio_trend_60d * 10.0)
        if bucket.startswith("equity"):
            score += credit_sig  # widening spreads bad for stocks
        elif bucket.startswith("bond_"):
            score += -credit_sig * 0.5  # mild flight-to-safety bid
        components += 1

    if components == 0:
        return 0.0
    return _clip(score / components)


# ---- trend score (price vs MA) ----


def _trend_score(closes: pd.Series | None, ma_days: int) -> float:
    """+1 if comfortably above 200d MA, -1 if comfortably below."""
    if closes is None or len(closes) < ma_days:
        return 0.0
    ma = closes.rolling(ma_days).mean().iloc[-1]
    if pd.isna(ma) or ma <= 0:
        return 0.0
    cur = float(closes.iloc[-1])
    # Distance from MA in % of MA, normalized: ±10% from MA → ±1 score
    return _clip((cur - ma) / ma / 0.10)


# ---- risk-off filter ----


def _check_risk_off(
    closes: Mapping[str, pd.Series],
    macro: MacroSnapshot,
    cfg: CoreSleeveConfig,
) -> tuple[bool, str | None]:
    """Both conditions must hold: SPY < 200d MA AND VIX > threshold."""
    spy = closes.get("SPY")
    if spy is None or len(spy) < cfg.risk_off_spy_ma_days:
        return False, None
    spy_ma = spy.rolling(cfg.risk_off_spy_ma_days).mean().iloc[-1]
    if pd.isna(spy_ma):
        return False, None
    spy_now = float(spy.iloc[-1])
    spy_below = spy_now < float(spy_ma)

    vix_high = macro.vix is not None and macro.vix > cfg.risk_off_vix_threshold

    if spy_below and vix_high:
        return True, (
            f"SPY {spy_now:.2f} < 200d MA {float(spy_ma):.2f} "
            f"AND VIX {macro.vix:.1f} > {cfg.risk_off_vix_threshold}"
        )
    return False, None


# ---- numerical helpers ----


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _norm(x: float, lo: float, hi: float) -> float:
    """Map x in [lo, hi] → [-1, 1] linearly, clipping outside."""
    if hi == lo:
        return 0.0
    return _clip(2 * (x - lo) / (hi - lo) - 1)


def _rank_to_signed(values: dict[str, float | None]) -> dict[str, float]:
    """Cross-sectional rank: best → +1, worst → -1, median → 0."""
    valid = {k: v for k, v in values.items() if v is not None}
    if len(valid) < 2:
        return {k: 0.0 for k in values}
    series = pd.Series(valid).rank(method="average")
    n = len(series)
    signed = 2 * (series - 1) / (n - 1) - 1  # ranks 1..n → -1..+1
    out = {k: 0.0 for k in values}
    for k, v in signed.items():
        out[k] = float(v)
    return out

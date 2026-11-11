"""
Combine the technical model's confidence with the sentiment score into one
'combined_confidence' that the tactical sleeve uses for sizing.

Inputs:
    ta_prediction    — model's class output (0 down, 1 flat-or-up depending on
                       classifier; 2 up for multiclass)
    ta_confidence    — model's max-class probability, ∈ [0, 1]
    sentiment_score  — provider's normalized score, ∈ [-1, 1]   (or None if missing)
    ta_weight        — sleeve config, e.g. 0.7
    sentiment_weight — sleeve config, e.g. 0.3   (must sum to ta_weight + sentiment_weight = 1)

Output:
    combined_confidence — ∈ [0, 1], biased up by bullish sentiment, down by bearish

If sentiment is missing/stale, we fall back to pure TA (no penalty for unknown).
A bullish TA call with bearish sentiment gets damped; a bullish TA with bullish
sentiment gets boosted. Bearish-TA stays bearish — sentiment never flips the sign.
"""

from __future__ import annotations

from dataclasses import dataclass


BULLISH_CLASSES: frozenset[int] = frozenset({1, 2})


@dataclass
class FusedSignal:
    ta_prediction: int
    ta_confidence: float
    sentiment_score: float | None
    combined_confidence: float
    reason: str


def fuse(
    *,
    ta_prediction: int,
    ta_confidence: float,
    sentiment_score: float | None,
    ta_weight: float = 0.70,
    sentiment_weight: float = 0.30,
) -> FusedSignal:
    """
    Combine TA + sentiment.

    The recipe:
      - Map TA confidence to a directional signal in [-1, 1]:
            ta_signal = ta_confidence  if bullish prediction
                      = -ta_confidence if bearish prediction
      - sentiment_signal = sentiment_score (already in [-1, 1])
      - combined_signal = ta_weight * ta_signal + sentiment_weight * sentiment_signal
      - combined_confidence = clamp(combined_signal in same direction as ta_prediction, 0, 1)

    If ta_prediction is bearish (0), combined_confidence is the magnitude of the
    fused negative signal — we never use it to size a long. The caller checks
    `is_bullish` to decide whether to act.
    """
    bullish = ta_prediction in BULLISH_CLASSES
    ta_signal = ta_confidence if bullish else -ta_confidence

    if sentiment_score is None:
        combined_signal = ta_signal
        reason = f"ta-only (no fresh sentiment): ta_signal={ta_signal:+.3f}"
    else:
        # Renormalize weights in case the caller passed something off
        total = ta_weight + sentiment_weight
        tw = ta_weight / total
        sw = sentiment_weight / total
        combined_signal = tw * ta_signal + sw * sentiment_score
        reason = (
            f"ta×{tw:.2f}={ta_signal * tw:+.3f}  "
            f"sent×{sw:.2f}={sentiment_score * sw:+.3f}  "
            f"sum={combined_signal:+.3f}"
        )

    # If we're long, confidence is the (positive) magnitude. If short/flat,
    # confidence is the magnitude of the negative number — caller decides.
    if bullish:
        combined_confidence = max(0.0, min(1.0, combined_signal))
    else:
        combined_confidence = max(0.0, min(1.0, -combined_signal))

    return FusedSignal(
        ta_prediction=ta_prediction,
        ta_confidence=ta_confidence,
        sentiment_score=sentiment_score,
        combined_confidence=combined_confidence,
        reason=reason,
    )

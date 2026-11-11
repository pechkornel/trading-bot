import pytest

from core.signal_fusion import fuse


def test_bullish_with_bullish_sentiment_is_boosted():
    out = fuse(ta_prediction=2, ta_confidence=0.85, sentiment_score=0.40)
    assert out.combined_confidence == pytest.approx(0.715)


def test_bullish_with_bearish_sentiment_is_damped():
    out = fuse(ta_prediction=2, ta_confidence=0.85, sentiment_score=-0.80)
    assert out.combined_confidence == pytest.approx(0.355)


def test_missing_sentiment_falls_back_to_ta_only():
    out = fuse(ta_prediction=2, ta_confidence=0.70, sentiment_score=None)
    assert out.combined_confidence == pytest.approx(0.70)
    assert out.reason.startswith("ta-only")
    assert out.sentiment_score is None


def test_bearish_prediction_never_flips():
    out = fuse(ta_prediction=0, ta_confidence=0.80, sentiment_score=0.90)
    assert out.combined_confidence == pytest.approx(0.29)
    assert out.ta_prediction == 0


def test_clamps_to_zero():
    out = fuse(ta_prediction=2, ta_confidence=0.20, sentiment_score=-1.0)
    assert out.combined_confidence == 0.0


def test_never_exceeds_one():
    out = fuse(ta_prediction=2, ta_confidence=1.0, sentiment_score=1.0)
    assert out.combined_confidence <= 1.0


def test_weights_are_renormalised():
    base = fuse(ta_prediction=2, ta_confidence=0.85, sentiment_score=-0.80)
    odd = fuse(
        ta_prediction=2,
        ta_confidence=0.85,
        sentiment_score=-0.80,
        ta_weight=1.4,
        sentiment_weight=0.6,
    )
    assert odd.combined_confidence == pytest.approx(base.combined_confidence)


def test_class_one_counts_as_bullish():
    out = fuse(ta_prediction=1, ta_confidence=0.85, sentiment_score=0.40)
    assert out.combined_confidence == pytest.approx(0.715)

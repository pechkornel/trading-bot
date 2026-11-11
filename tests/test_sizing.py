import math

import numpy as np
import pandas as pd
import pytest

from core.sizing import PortfolioParams, SymbolSignal, realized_vol, size_portfolio


def sig(symbol="SPY", prediction=2, confidence=0.775, close=100.0, vol=0.005):
    return SymbolSignal(
        symbol=symbol,
        prediction=prediction,
        confidence=confidence,
        close=close,
        atr=1.0,
        realized_vol=vol,
    )


def size_one(signal, equity=100_000.0, **params):
    return size_portfolio([signal], equity, PortfolioParams(**params))[0]


def test_realized_vol_short_series_is_nan():
    closes = pd.Series([100.0, 101.0, 102.0])
    assert math.isnan(realized_vol(closes, lookback=3))


def test_realized_vol_is_population_std_of_last_returns():
    closes = pd.Series([100.0, 102.0, 101.0, 103.0, 104.0, 102.0])
    lookback = 4
    expected = float(np.std(closes.pct_change().dropna().to_numpy()[-lookback:], ddof=0))
    assert realized_vol(closes, lookback) == pytest.approx(expected)


def test_not_bullish_gets_zero_shares():
    d = size_one(sig(prediction=0))
    assert d.target_shares == 0
    assert d.reason == "prediction=0 not bullish"


def test_confidence_below_floor_gets_zero_shares():
    d = size_one(sig(confidence=0.50))
    assert d.target_shares == 0
    assert "floor" in d.reason


def test_signal_weight_ramp():
    d = size_one(sig(confidence=0.775), confidence_floor=0.55)
    assert d.signal_weight == pytest.approx(0.5)


def test_basic_sizing_is_capped_at_max_per_symbol():
    d = size_one(sig())
    assert d.vol_scale == pytest.approx(1.0)
    # raw 50,000 is capped at 30% of equity.
    assert d.target_shares == 300
    assert d.position_value == pytest.approx(30_000.0)
    assert d.reason == "sized"


def test_vol_scale_clamps_to_max_and_min():
    assert size_one(sig(vol=0.0001)).vol_scale == pytest.approx(5.0)
    assert size_one(sig(vol=1.0)).vol_scale == pytest.approx(0.1)


def test_nan_vol_gets_zero_shares():
    d = size_one(sig(vol=float("nan")))
    assert d.target_shares == 0
    assert d.reason == "realized_vol unavailable"


def test_zero_vol_is_floored_and_clamps_to_max_scale():
    # Current behaviour: vol 0 is floored to 1e-6, so the scale hits the max.
    d = size_one(sig(vol=0.0))
    assert d.vol_scale == pytest.approx(5.0)
    assert d.target_shares == 300


def test_rounds_to_zero_when_price_exceeds_capped_value():
    d = size_one(sig(close=50_000.0))
    assert d.target_shares == 0
    assert d.reason == "rounded to 0 shares"


def test_gross_cap_scales_positions_down():
    signals = [sig("A"), sig("B"), sig("C"), sig("D", prediction=0)]
    out = size_portfolio(signals, 100_000.0, PortfolioParams(max_gross_pct=0.5))
    scale = 50_000 / 90_000
    for d in out[:3]:
        assert "gross-capped" in d.reason
        assert d.target_shares == int(30_000 * scale / 100)
    assert sum(d.position_value for d in out) <= 50_000
    # Zero-share decisions are passed through unchanged.
    assert out[3].target_shares == 0
    assert out[3].reason == "prediction=0 not bullish"


def test_output_order_matches_input_order():
    signals = [sig("C"), sig("A"), sig("B")]
    out = size_portfolio(signals, 100_000.0, PortfolioParams())
    assert [d.symbol for d in out] == ["C", "A", "B"]

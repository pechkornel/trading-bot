import pytest

from core.config import CoreSleeveConfig
from core.scoring import (
    ScoringResult,
    _check_risk_off,
    _macro_score,
    _rank_to_signed,
    _trend_score,
    score_universe,
    select_holdings,
)
from tests.conftest import linear_closes, make_macro, series


def small_cfg(**overrides) -> CoreSleeveConfig:
    fields = dict(
        universe=["SPY", "TLT", "GLD"],
        hold_top_n=2,
        momentum_lookback_days=10,
        trend_ma_days=5,
        risk_off_spy_ma_days=5,
        risk_off_universe=["TLT", "IEF"],
    )
    fields.update(overrides)
    return CoreSleeveConfig(**fields)


# ---- _rank_to_signed ----


def test_rank_three_values():
    out = _rank_to_signed({"a": 0.1, "b": 0.5, "c": 0.3})
    assert out == {"a": -1.0, "b": 1.0, "c": 0.0}


def test_rank_ties_are_averaged():
    out = _rank_to_signed({"a": 0.1, "b": 0.1, "c": 0.5})
    assert out["a"] == pytest.approx(out["b"])
    assert out["a"] == pytest.approx(-0.5)
    assert out["c"] == pytest.approx(1.0)


def test_rank_single_valid_value_gives_zeros():
    assert _rank_to_signed({"a": 0.2, "b": None}) == {"a": 0.0, "b": 0.0}


def test_rank_none_entries_get_zero():
    out = _rank_to_signed({"a": 0.1, "b": 0.5, "c": None})
    assert out == {"a": -1.0, "b": 1.0, "c": 0.0}


# ---- momentum via score_universe ----


def test_momentum_best_slope_scores_plus_one():
    cfg = small_cfg()
    closes = {
        "SPY": linear_closes(100, 1.0, 20),
        "TLT": linear_closes(100, 0.5, 20),
        "GLD": linear_closes(100, 0.1, 20),
    }
    result = score_universe(closes, make_macro(), cfg)
    by_sym = {s.symbol: s for s in result.scores}
    assert by_sym["SPY"].momentum == 1.0
    assert by_sym["TLT"].momentum == 0.0
    assert by_sym["GLD"].momentum == -1.0


def test_momentum_short_history_gets_zero():
    cfg = small_cfg()
    closes = {
        "SPY": linear_closes(100, 1.0, 20),
        "TLT": linear_closes(100, 0.5, 20),
        "GLD": linear_closes(100, 2.0, 5),  # shorter than lookback + 1
    }
    result = score_universe(closes, make_macro(), cfg)
    by_sym = {s.symbol: s for s in result.scores}
    assert by_sym["GLD"].momentum == 0.0


# ---- _trend_score ----


def test_trend_score_far_above_ma_is_one():
    # MA = 110, last = 150: well past +10%.
    assert _trend_score(series([100, 100, 100, 100, 150]), 5) == 1.0


def test_trend_score_far_below_ma_is_minus_one():
    assert _trend_score(series([100, 100, 100, 100, 50]), 5) == -1.0


def test_trend_score_is_linear_inside_the_band():
    # MA = 100.2, last = 101: 0.8% above the MA, so 0.8 / 10 of full score.
    closes = series([100, 100, 100, 100, 101])
    assert _trend_score(closes, 5) == pytest.approx((101 - 100.2) / 100.2 / 0.10)


def test_trend_score_short_history_is_zero():
    assert _trend_score(series([100, 101]), 5) == 0.0
    assert _trend_score(None, 5) == 0.0


# ---- _macro_score ----


def test_macro_all_none_is_zero():
    assert _macro_score("equity_us_large", make_macro()) == 0.0


def test_macro_vix_30_hurts_equity_and_helps_long_bonds():
    m = make_macro(vix=30.0)
    assert _macro_score("equity_us_large", m) == pytest.approx(-1.0)
    assert _macro_score("bond_long", m) == pytest.approx(1.0)


def test_macro_steep_yield_curve():
    m = make_macro(yield_curve=2.0)
    assert _macro_score("equity_us_large", m) == pytest.approx(1.0)
    assert _macro_score("bond_long", m) == pytest.approx(-1.0)


def test_macro_cash_with_only_yield_curve_is_zero():
    # No rule matches cash, but the component still counts toward the average.
    assert _macro_score("cash", make_macro(yield_curve=2.0)) == 0.0


# DXY and credit alone only count when the snapshot is complete: an
# incomplete snapshot with no VIX and no yield curve is treated as neutral.


def test_macro_dxy_alone_in_incomplete_snapshot_is_neutral():
    assert _macro_score("alt_commodity", make_macro(dxy_trend_60d=0.05)) == 0.0


def test_macro_strong_dollar_hurts_commodities_and_helps_us_large():
    # A 5% 60-day DXY rise is half the full signal (scaled by 10).
    m = make_macro(dxy_trend_60d=0.05, complete=True)
    assert _macro_score("alt_commodity", m) == pytest.approx(-0.5)
    assert _macro_score("equity_intl_em", m) == pytest.approx(-0.5)
    assert _macro_score("equity_us_large", m) == pytest.approx(0.5)
    assert _macro_score("bond_long", m) == pytest.approx(0.0)


def test_macro_falling_credit_ratio_hurts_equity_and_mildly_helps_bonds():
    # HYG/LQD down 5% over 60 days: -0.5 for equity, half weight and
    # inverted for bonds (+0.25).
    m = make_macro(credit_ratio_trend_60d=-0.05, complete=True)
    assert _macro_score("equity_us_large", m) == pytest.approx(-0.5)
    assert _macro_score("bond_long", m) == pytest.approx(0.25)
    assert _macro_score("alt_gold", m) == pytest.approx(0.0)


# ---- composite ----


def test_composite_is_weighted_sum_and_sorted_descending():
    cfg = small_cfg()
    closes = {
        "SPY": linear_closes(100, 1.0, 20),
        "TLT": linear_closes(100, 0.5, 20),
        "GLD": linear_closes(100, -0.5, 20),
    }
    macro = make_macro(vix=20.0, yield_curve=1.0)
    result = score_universe(closes, macro, cfg)
    for s in result.scores:
        expected = 0.4 * s.momentum + 0.3 * s.macro + 0.3 * s.trend
        assert s.composite == pytest.approx(expected)
    composites = [s.composite for s in result.scores]
    assert composites == sorted(composites, reverse=True)


# ---- _check_risk_off ----


def falling_spy():
    return {"SPY": series([110, 110, 110, 110, 90])}


def test_risk_off_when_spy_below_ma_and_vix_high():
    risk_off, reason = _check_risk_off(falling_spy(), make_macro(vix=30.0), small_cfg())
    assert risk_off is True
    assert reason is not None and "VIX 30.0" in reason


def test_no_risk_off_when_vix_low():
    assert _check_risk_off(falling_spy(), make_macro(vix=20.0), small_cfg()) == (False, None)


def test_no_risk_off_when_spy_above_ma():
    closes = {"SPY": series([90, 90, 90, 90, 110])}
    assert _check_risk_off(closes, make_macro(vix=30.0), small_cfg()) == (False, None)


def test_no_risk_off_when_vix_exactly_at_threshold():
    # The rule is VIX strictly above the threshold (25.0 by default).
    assert _check_risk_off(falling_spy(), make_macro(vix=25.0), small_cfg()) == (False, None)


def test_no_risk_off_when_spy_exactly_at_its_average():
    # The rule is SPY strictly below its moving average.
    closes = {"SPY": series([100, 100, 100, 100, 100])}
    assert _check_risk_off(closes, make_macro(vix=30.0), small_cfg()) == (False, None)


def test_no_risk_off_when_vix_missing():
    assert _check_risk_off(falling_spy(), make_macro(), small_cfg()) == (False, None)


def test_no_risk_off_when_spy_history_too_short():
    closes = {"SPY": series([110, 90])}
    assert _check_risk_off(closes, make_macro(vix=30.0), small_cfg()) == (False, None)


# ---- select_holdings ----


def test_select_holdings_risk_off_uses_bond_bucket():
    result = ScoringResult(scores=[], risk_off=True, risk_off_reason="x")
    assert select_holdings(result, small_cfg()) == {"TLT": 0.5, "IEF": 0.5}


def test_select_holdings_equal_weights_top_n():
    cfg = small_cfg()
    closes = {
        "SPY": linear_closes(100, 1.0, 20),
        "TLT": linear_closes(100, 0.5, 20),
        "GLD": linear_closes(100, -0.5, 20),
    }
    result = score_universe(closes, make_macro(), cfg)
    assert select_holdings(result, cfg) == {"SPY": 0.5, "TLT": 0.5}


def test_select_holdings_empty_scores_gives_empty_dict():
    result = ScoringResult(scores=[], risk_off=False, risk_off_reason=None)
    assert select_holdings(result, small_cfg()) == {}

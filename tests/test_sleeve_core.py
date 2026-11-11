from datetime import datetime, timezone

import pytest

from core.config import CoreSleeveConfig
from core.sleeves.core import CoreSleeve
from tests.conftest import linear_closes, make_macro, series

FRIDAY = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
WEDNESDAY = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


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


def sleeve() -> CoreSleeve:
    return CoreSleeve(small_cfg())


def check(now, *, last=None, holdings=None, prices=None, equity=10_000.0):
    return sleeve().should_evaluate(
        last_rebalance_ts=last,
        current_holdings=holdings or {},
        prices=prices or {},
        sleeve_equity=equity,
        now=now,
    )


def ranked_closes():
    """SPY and TLT rank above GLD; every series ends at 100."""
    return {
        "SPY": linear_closes(81, 1.0, 20),
        "TLT": linear_closes(90.5, 0.5, 20),
        "GLD": linear_closes(109.5, -0.5, 20),
    }


def evaluate(closes, macro=None, holdings=None, equity=10_000.0):
    return sleeve().evaluate(
        closes=closes,
        macro=macro or make_macro(),
        current_holdings=holdings or {},
        sleeve_equity=equity,
        reason="test",
    )


def by_symbol(result):
    return {d.symbol: d for d in result.decisions}


# ---- construction ----


def test_weights_not_summing_to_one_raise():
    cfg = small_cfg(momentum_weight=0.3, macro_weight=0.3, trend_weight=0.3)
    with pytest.raises(ValueError, match="sum to 1.0"):
        CoreSleeve(cfg)


# ---- should_evaluate: calendar ----


def test_friday_without_prior_rebalance_triggers():
    assert check(FRIDAY) == (True, "calendar: Fri close")


def test_same_day_rebalance_is_not_repeated():
    assert check(FRIDAY, last=FRIDAY) == (False, "already rebalanced today")


def test_wednesday_is_not_a_rebalance_day():
    assert check(WEDNESDAY) == (False, "not rebalance day (today=Wed, want=Fri)")


# ---- should_evaluate: drift ----


def test_drift_triggers_on_a_non_rebalance_day():
    ok, reason = check(
        WEDNESDAY,
        holdings={"SPY": 60, "TLT": 40},
        prices={"SPY": 100.0, "TLT": 100.0},
    )
    assert ok is True
    assert reason.startswith("drift: ")


def test_balanced_holdings_do_not_trigger_drift():
    ok, reason = check(
        WEDNESDAY,
        holdings={"SPY": 50, "TLT": 50},
        prices={"SPY": 100.0, "TLT": 100.0},
    )
    assert ok is False
    assert not reason.startswith("drift")


def test_drift_ignores_zero_share_holdings():
    ok, _ = check(
        WEDNESDAY,
        holdings={"SPY": 50, "TLT": 50, "GLD": 0},
        prices={"SPY": 100.0, "TLT": 100.0, "GLD": 100.0},
    )
    assert ok is False


def test_empty_holdings_or_zero_equity_never_drift():
    assert check(WEDNESDAY, holdings={})[0] is False
    ok, _ = check(
        WEDNESDAY,
        holdings={"SPY": 60, "TLT": 40},
        prices={"SPY": 100.0, "TLT": 100.0},
        equity=0.0,
    )
    assert ok is False


# ---- evaluate ----


def test_top_two_get_equal_target_shares():
    result = evaluate(ranked_closes())
    decisions = by_symbol(result)
    assert result.risk_off is False
    assert decisions["SPY"].target_shares == 50
    assert decisions["TLT"].target_shares == 50
    assert decisions["SPY"].delta_shares == 50
    assert "GLD" not in decisions
    assert decisions["SPY"].reason == "top 2 by score"


def test_held_symbol_outside_top_set_is_sold():
    result = evaluate(ranked_closes(), holdings={"GLD": 30})
    gld = by_symbol(result)["GLD"]
    assert gld.target_shares == 0
    assert gld.delta_shares == -30
    assert gld.reason == "out of top set"


def test_symbol_without_close_series_is_skipped():
    result = evaluate(ranked_closes(), holdings={"XYZ": 5})
    assert "XYZ" not in by_symbol(result)


def test_risk_off_moves_to_bond_bucket():
    closes = ranked_closes()
    closes["SPY"] = series([110.0] * 19 + [90.0])
    closes["IEF"] = linear_closes(100, 0.0, 20)
    result = evaluate(closes, macro=make_macro(vix=30.0))
    decisions = by_symbol(result)
    assert result.risk_off is True
    assert decisions["TLT"].reason == "risk-off bond bucket"
    assert decisions["IEF"].reason == "risk-off bond bucket"
    assert decisions["IEF"].target_shares == 50
    assert decisions["IEF"].score is None
    assert decisions["TLT"].score is not None

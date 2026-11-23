from datetime import datetime, timedelta, timezone

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
    assert check(FRIDAY) == (True, "calendar: Fri")


def test_same_day_rebalance_is_not_repeated():
    assert check(FRIDAY, last=FRIDAY) == (False, "already rebalanced today")


def test_earlier_rebalance_on_the_same_eastern_date_blocks_a_repeat():
    # Hourly cycles: both times are Friday in Eastern time (09:30 and 15:00 EST).
    last = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    now = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
    assert check(now, last=last) == (False, "already rebalanced today")


def test_rebalance_on_the_previous_eastern_date_does_not_block():
    previous_week = FRIDAY - timedelta(days=7)
    assert check(FRIDAY, last=previous_week) == (True, "calendar: Fri")


def test_thursday_evening_eastern_is_not_a_rebalance_day():
    now = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)  # Thu 20:00 EST
    assert check(now) == (False, "not rebalance day (today=Thu, want=Fri)")


def test_thursday_evening_eastern_in_summer_is_not_a_rebalance_day():
    now = datetime(2026, 6, 12, 0, 30, tzinfo=timezone.utc)  # Thu 20:30 EDT
    assert check(now) == (False, "not rebalance day (today=Thu, want=Fri)")


def test_friday_evening_eastern_is_a_rebalance_day():
    now = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)  # Fri 21:00 EST
    assert check(now) == (True, "calendar: Fri")


def test_same_eastern_date_blocks_a_repeat_across_utc_midnight():
    last = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)  # Fri 10:00 EST
    now = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)  # Fri 21:00 EST
    assert check(now, last=last) == (False, "already rebalanced today")


def test_rebalance_on_previous_eastern_date_does_not_block_across_utc_date():
    last = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)  # Thu 20:00 EST
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)  # Fri 10:00 EST
    assert check(now, last=last) == (True, "calendar: Fri")


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


def test_drift_does_not_rebalance_twice_on_the_same_eastern_date():
    assert check(
        WEDNESDAY,
        last=WEDNESDAY - timedelta(hours=2),
        holdings={"SPY": 60, "TLT": 40},
        prices={"SPY": 100.0, "TLT": 100.0},
    ) == (False, "already rebalanced today")


def test_drift_triggers_after_a_rebalance_on_the_previous_eastern_date():
    ok, reason = check(
        WEDNESDAY,
        last=WEDNESDAY - timedelta(days=1),
        holdings={"SPY": 60, "TLT": 40},
        prices={"SPY": 100.0, "TLT": 100.0},
    )
    assert ok is True
    assert reason.startswith("drift: ")


def test_underweight_holdings_trigger_drift():
    # Half the sleeve is uninvested: each holding is 25% against a 50% target.
    # Only the absolute distance from 1/N catches this.
    ok, reason = check(
        WEDNESDAY,
        holdings={"SPY": 50, "TLT": 50},
        prices={"SPY": 100.0, "TLT": 100.0},
        equity=20_000.0,
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


# ---- re-entry cooldown ----


def cooldown_eval(holdings, last_exit_ts):
    return by_symbol(
        CoreSleeve(small_cfg(), reentry_cooldown_sec=86400).evaluate(
            closes=ranked_closes(),
            macro=make_macro(),
            current_holdings=holdings,
            sleeve_equity=10_000.0,
            reason="test",
            last_exit_ts=last_exit_ts,
            now=FRIDAY,
        )
    )


def test_recent_exit_blocks_core_buy_back():
    d = cooldown_eval({"SPY": 0, "TLT": 50}, {"SPY": FRIDAY - timedelta(hours=1)})
    assert d["SPY"].target_shares == 0
    assert d["SPY"].delta_shares == 0
    assert d["SPY"].target_weight == 0.0
    assert d["SPY"].reason.startswith("reentry cooldown")
    assert d["TLT"].target_shares == 50
    assert d["TLT"].delta_shares == 0


def test_cooldown_applies_without_a_holdings_row():
    d = cooldown_eval({"TLT": 50}, {"SPY": FRIDAY - timedelta(hours=1)})
    assert d["SPY"].delta_shares == 0
    assert d["SPY"].reason.startswith("reentry cooldown")


def test_held_symbol_top_up_is_not_blocked():
    d = cooldown_eval({"SPY": 30, "TLT": 50}, {"SPY": FRIDAY - timedelta(hours=1)})
    assert d["SPY"].delta_shares == 20


def test_core_buy_back_allowed_after_cooldown():
    d = cooldown_eval({"SPY": 0, "TLT": 50}, {"SPY": FRIDAY - timedelta(days=2)})
    assert d["SPY"].delta_shares == 50


def test_sells_are_never_blocked():
    d = cooldown_eval({"GLD": 30}, {"GLD": FRIDAY - timedelta(hours=1)})
    assert d["GLD"].delta_shares == -30

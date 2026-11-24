"""core.market_hours: the NYSE calendar (2026 data pinned), US Eastern time, market
regimes (including HOLIDAY and early closes) and trading_days_between."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.market_hours import (
    MarketRegime,
    market_state,
    nyse_calendar,
    should_run_entries,
    should_run_exits,
    to_eastern,
    trading_days_between,
)


def _close_utc(day: str) -> datetime:
    return nyse_calendar().session_close(pd.Timestamp(day)).to_pydatetime()


def _open_utc(day: str) -> datetime:
    return nyse_calendar().session_open(pd.Timestamp(day)).to_pydatetime()


@pytest.mark.parametrize(
    "day",
    [
        "2026-04-03",  # Good Friday
        "2026-07-03",  # Independence Day observed
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
    ],
)
def test_2026_full_closures_are_not_sessions(day):
    assert not nyse_calendar().is_session(pd.Timestamp(day))


def test_day_before_observed_independence_day_is_a_full_day():
    assert nyse_calendar().is_session(pd.Timestamp("2026-07-02"))
    assert _close_utc("2026-07-02") == datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("day", ["2026-11-27", "2026-12-24"])
def test_2026_early_closes_are_13_00_eastern(day):
    y, m, d = (int(p) for p in day.split("-"))
    assert _close_utc(day) == datetime(y, m, d, 18, 0, tzinfo=timezone.utc)


def test_regular_session_is_09_30_to_16_00_eastern():
    assert _open_utc("2026-11-13") == datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    assert _close_utc("2026-11-13") == datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)


def test_calendar_reaches_the_end_of_the_year_after_next():
    last = nyse_calendar().sessions[-1]
    assert last >= pd.Timestamp(year=datetime.now(timezone.utc).year + 2, month=12, day=24)


def test_calendar_is_built_once_per_year():
    assert nyse_calendar() is nyse_calendar()


def test_calendar_window_moves_forward_after_new_year(monkeypatch):
    # A long-running process must get a calendar that still reaches two years
    # ahead after 1 January, not the one cached in the previous year.
    import core.market_hours as mh

    def frozen_at(moment: datetime):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        return FrozenDatetime

    monkeypatch.setattr(mh, "datetime", frozen_at(datetime(2026, 12, 31, 23, tzinfo=timezone.utc)))
    before = nyse_calendar().sessions[-1]
    monkeypatch.setattr(mh, "datetime", frozen_at(datetime(2027, 1, 1, 1, tzinfo=timezone.utc)))
    after = nyse_calendar().sessions[-1]

    assert before.year == 2028
    assert after.year == 2029


# ---- to_eastern at the 2026 DST transitions ----


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # Spring forward 2026-03-08: 07:00 UTC is 02:00 EST, clocks jump to 03:00 EDT.
        (datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc), datetime(2026, 3, 8, 1, 30)),
        (datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), datetime(2026, 3, 8, 3, 0)),
        (datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc), datetime(2026, 3, 8, 4, 30)),
        # Fall back 2026-11-01: 06:00 UTC is 02:00 EDT, clocks return to 01:00 EST.
        (datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, 1, 30)),
        (datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 1, 0)),
        (datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, 1, 30)),
        (datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, 2, 30)),
    ],
)
def test_to_eastern_at_dst_transitions(utc, expected):
    result = to_eastern(utc)
    assert result == expected
    assert result.tzinfo is None


def test_to_eastern_naive_input_is_utc():
    assert to_eastern(datetime(2026, 3, 8, 6, 30)) == datetime(2026, 3, 8, 1, 30)
    assert to_eastern(datetime(2026, 7, 1, 12, 0)) == datetime(2026, 7, 1, 8, 0)


def test_to_eastern_aware_non_utc_input():
    plus_two = timezone(timedelta(hours=2))
    assert to_eastern(datetime(2026, 11, 1, 8, 30, tzinfo=plus_two)) == datetime(2026, 11, 1, 1, 30)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now",
    [
        _utc(2026, 11, 26, 15),  # Thanksgiving 10:00 ET
        _utc(2026, 11, 26, 12),  # Thanksgiving 07:00 ET, not PRE_OPEN
        _utc(2026, 11, 26, 23),  # Thanksgiving evening, not AFTER_CLOSE
        _utc(2026, 4, 3, 15),  # Good Friday
        _utc(2026, 7, 3, 14),  # Independence Day observed
        _utc(2026, 12, 25, 15),  # Christmas
    ],
)
def test_weekday_holiday_is_holiday_regime(now):
    ms = market_state(now)
    assert ms.regime == MarketRegime.HOLIDAY
    assert ms.is_open is False
    assert ms.is_weekend is False
    assert ms.minutes_to_close is None
    assert ms.minutes_to_open > 0


def test_thanksgiving_minutes_to_open():
    assert market_state(_utc(2026, 11, 26, 15)).minutes_to_open == 1410


def test_early_close_open_until_real_close():
    ms = market_state(_utc(2026, 11, 27, 17, 30))  # Fri 12:30 ET
    assert ms.regime == MarketRegime.OPEN
    assert ms.minutes_to_close == 30
    assert ms.minutes_to_open is None


def test_early_close_after_close_starts_at_13():
    ms = market_state(_utc(2026, 11, 27, 18, 30))  # Fri 13:30 ET
    assert ms.regime == MarketRegime.AFTER_CLOSE
    assert ms.minutes_to_open == 4080


def test_christmas_eve_early_close_boundary():
    last = market_state(_utc(2026, 12, 24, 17, 59))
    assert last.regime == MarketRegime.OPEN
    assert last.minutes_to_close == 1
    assert market_state(_utc(2026, 12, 24, 18, 0)).regime == MarketRegime.AFTER_CLOSE


def test_eve_of_holiday_after_close_counts_to_next_session():
    ms = market_state(_utc(2026, 11, 25, 22))  # Wed 17:00 ET
    assert ms.regime == MarketRegime.AFTER_CLOSE
    assert ms.minutes_to_open == 2430


def test_minutes_to_open_is_real_elapsed_across_dst_weekends():
    assert market_state(_utc(2026, 3, 7, 17)).minutes_to_open == 2670
    assert market_state(_utc(2026, 10, 31, 16)).minutes_to_open == 2790


def test_normal_friday_regression():
    open_ms = market_state(_utc(2026, 11, 13, 15))
    assert open_ms.regime == MarketRegime.OPEN
    assert open_ms.minutes_to_close == 360
    pre = market_state(_utc(2026, 11, 13, 14))
    assert pre.regime == MarketRegime.PRE_OPEN
    assert pre.minutes_to_open == 30


def test_weekend_stays_weekend():
    ms = market_state(_utc(2026, 11, 14, 15))
    assert ms.regime == MarketRegime.WEEKEND
    assert ms.is_weekend is True


def test_holiday_gating_entries_off_exits_on():
    ms = market_state(_utc(2026, 11, 26, 15))
    assert should_run_entries(ms) is False
    assert should_run_exits(ms) is True


# ---- trading_days_between ----


@pytest.mark.parametrize(
    "start, end, expected",
    [
        # Ten sessions, Fri 2026-10-30 to Fri 2026-11-13 (close counted at 21:00 UTC).
        (_utc(2026, 10, 30, 14), _utc(2026, 11, 13, 12), 10),
        (_utc(2026, 11, 2, 15, 30), _utc(2026, 11, 13, 12), 9),
        # Thanksgiving and the weekend are skipped, the 11-27 early close counts.
        (_utc(2026, 11, 25, 15), _utc(2026, 11, 30, 15), 2),
        # Christmas Eve early close counts, 12-25 and the weekend do not.
        (_utc(2026, 12, 23, 15), _utc(2026, 12, 28, 15), 2),
        # Good Friday 2026-04-03 is skipped.
        (_utc(2026, 4, 2, 15), _utc(2026, 4, 6, 15), 1),
        # Independence Day observed on Friday 2026-07-03.
        (_utc(2026, 7, 2, 15), _utc(2026, 7, 7, 15), 2),
    ],
)
def test_trading_days_between_around_holidays(start, end, expected):
    assert trading_days_between(start, end) == expected


def test_trading_days_between_close_is_inclusive():
    start = _utc(2026, 11, 13, 15)
    assert trading_days_between(start, _utc(2026, 11, 13, 20, 59)) == 0
    assert trading_days_between(start, _utc(2026, 11, 13, 21)) == 1


def test_trading_days_between_early_close_boundary():
    start = _utc(2026, 11, 27, 15)
    assert trading_days_between(start, _utc(2026, 11, 27, 17, 59)) == 0
    assert trading_days_between(start, _utc(2026, 11, 27, 18)) == 1


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(days=-3)])
def test_trading_days_between_end_not_after_start_is_zero(delta):
    start = _utc(2026, 11, 13, 15)
    assert trading_days_between(start, start + delta) == 0


def test_trading_days_between_naive_inputs_are_utc():
    start, end = _utc(2026, 10, 30, 14), _utc(2026, 11, 13, 12)
    naive = trading_days_between(start.replace(tzinfo=None), end.replace(tzinfo=None))
    assert naive == trading_days_between(start, end) == 10
    assert trading_days_between(start.replace(tzinfo=None), end) == 10

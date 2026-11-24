"""NYSE calendar data pinned for 2026, read through core.market_hours.nyse_calendar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.market_hours import nyse_calendar, to_eastern


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

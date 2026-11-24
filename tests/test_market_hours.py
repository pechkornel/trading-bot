"""NYSE calendar data pinned for 2026, read through core.market_hours.nyse_calendar."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from core.market_hours import nyse_calendar


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

"""Tests for the re-entry cooldown helpers."""

from datetime import datetime, timedelta, timezone

from core.sleeves.cooldown import cooldown_remaining, format_cooldown

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
DAY = 86400


def test_none_exit_time_is_free():
    assert cooldown_remaining(None, NOW, DAY) is None


def test_zero_cooldown_is_free():
    assert cooldown_remaining(NOW - timedelta(hours=1), NOW, 0) is None


def test_negative_cooldown_is_free():
    assert cooldown_remaining(NOW - timedelta(hours=1), NOW, -5) is None


def test_exit_one_hour_ago_has_23_hours_left():
    left = cooldown_remaining(NOW - timedelta(hours=1), NOW, DAY)
    assert left == timedelta(hours=23)


def test_exit_exactly_at_the_cooldown_is_free():
    assert cooldown_remaining(NOW - timedelta(seconds=DAY), NOW, DAY) is None


def test_exit_two_days_ago_is_free():
    assert cooldown_remaining(NOW - timedelta(days=2), NOW, DAY) is None


def test_naive_exit_ts_is_treated_as_utc():
    aware = NOW - timedelta(hours=1)
    naive = aware.replace(tzinfo=None)
    assert cooldown_remaining(naive, NOW, DAY) == cooldown_remaining(aware, NOW, DAY)


def test_naive_now_is_treated_as_utc():
    exit_ts = NOW - timedelta(hours=1)
    assert cooldown_remaining(exit_ts, NOW.replace(tzinfo=None), DAY) == timedelta(hours=23)


def test_format_cooldown():
    assert format_cooldown(timedelta(hours=23)) == "reentry cooldown: 23.0h left"

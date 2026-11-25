"""Tests for the pure keeper session policy (fixed aware datetimes, no I/O except the state file)."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.auth_types import (
    BadCredentials,
    GatewayUnreachable,
    ManualStepRequired,
    NotPaperAccount,
    SecondFactorRequired,
)
from core.config import KeeperConfig
from live.keeper_policy import (
    ActionKind,
    KeeperState,
    Observation,
    backoff_delay_sec,
    clear_lockout,
    decide,
    load_state,
    record_login_failure,
    record_login_success,
    record_submit,
    save_state,
)

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
CFG = KeeperConfig()


def ny(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=NY)


# 2025-06-10 is a Tuesday, 2025-06-14 a Saturday.
TUE_NOON = ny(2025, 6, 10, 12, 0)


def fresh_session(now=TUE_NOON, age_sec=3600):
    return KeeperState(
        last_login_at=now - timedelta(seconds=age_sec),
        authenticated_since=now - timedelta(seconds=age_sec),
    )


def lost(now, cfg=CFG, state=None):
    return decide(state or fresh_session(now), Observation.NOT_AUTHENTICATED, now, cfg)


def test_authenticated_gives_tickle():
    action = decide(fresh_session(), Observation.AUTHENTICATED, TUE_NOON, CFG)
    assert action.kind is ActionKind.TICKLE


def test_drop_in_nightly_gap_waits_until_gap_end_then_logs_in():
    now = ny(2025, 6, 11, 0, 50)
    action = lost(now)
    assert action.kind is ActionKind.WAIT
    assert action.until == ny(2025, 6, 11, 1, 30)
    assert lost(ny(2025, 6, 11, 1, 30), state=fresh_session(now)).kind is ActionKind.LOGIN


def test_drop_on_saturday_waits_until_sunday_midnight():
    now = ny(2025, 6, 14, 10, 15)
    action = lost(now)
    assert action.kind is ActionKind.WAIT
    assert action.until == ny(2025, 6, 15, 0, 0)
    assert lost(ny(2025, 6, 15, 0, 0), state=fresh_session(now)).kind is ActionKind.LOGIN


def test_saturday_end_2359_has_no_hole_before_sunday():
    now = ny(2025, 6, 14, 23, 59, 30)
    action = lost(now)
    assert action.kind is ActionKind.WAIT
    assert action.until == ny(2025, 6, 15, 0, 0)


def test_saturday_start_is_inclusive():
    assert lost(ny(2025, 6, 14, 0, 0)).kind is ActionKind.WAIT


def test_friday_late_is_not_maintenance():
    assert lost(ny(2025, 6, 13, 23, 59, 59)).kind is ActionKind.LOGIN


def test_custom_maintenance_end_is_exclusive():
    cfg = KeeperConfig(maintenance_start="02:00", maintenance_end="06:00")
    assert lost(ny(2025, 6, 14, 5, 59, 59), cfg).until == ny(2025, 6, 14, 6, 0)
    assert lost(ny(2025, 6, 14, 6, 0), cfg).kind is ActionKind.LOGIN
    assert lost(ny(2025, 6, 14, 1, 59, 59), cfg).kind is ActionKind.LOGIN
    assert lost(ny(2025, 6, 14, 2, 0), cfg).kind is ActionKind.WAIT


def test_nightly_gap_edges_are_exact():
    assert lost(ny(2025, 6, 11, 0, 44, 59)).kind is ActionKind.LOGIN
    assert lost(ny(2025, 6, 11, 0, 45)).kind is ActionKind.WAIT
    assert lost(ny(2025, 6, 11, 1, 29, 59)).kind is ActionKind.WAIT
    assert lost(ny(2025, 6, 11, 1, 30)).kind is ActionKind.LOGIN


def test_dst_spring_forward_night():
    # 2025-03-09: clocks jump from 02:00 to 03:00 in New York. The gap is before it.
    now = ny(2025, 3, 9, 1, 0)
    action = lost(now)
    assert action.kind is ActionKind.WAIT
    assert action.until == ny(2025, 3, 9, 1, 30)
    assert lost(ny(2025, 3, 9, 3, 0)).kind is ActionKind.LOGIN


def test_dst_fall_back_night_handles_both_01_10():
    # 2025-11-02: 01:00 to 02:00 happens twice. Both 01:10 readings are inside the gap.
    first = ny(2025, 11, 2, 1, 10)
    second = first.replace(fold=1)
    for now in (first, second):
        action = lost(now)
        assert action.kind is ActionKind.WAIT
        assert action.until.astimezone(UTC) > now.astimezone(UTC)
        assert action.until.astimezone(UTC) - now.astimezone(UTC) == timedelta(minutes=20)
    done = ny(2025, 11, 2, 1, 30).replace(fold=1)
    assert lost(done).kind is ActionKind.LOGIN


def test_aware_datetime_in_other_zone_is_converted():
    utc_now = ny(2025, 6, 11, 0, 50).astimezone(UTC)
    action = lost(utc_now)
    assert action.kind is ActionKind.WAIT
    assert action.until == ny(2025, 6, 11, 1, 30)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        decide(KeeperState(), Observation.AUTHENTICATED, datetime(2025, 6, 10, 12, 0), CFG)


def test_drop_after_24h_logs_in_at_once_and_is_expected():
    action = lost(TUE_NOON, state=fresh_session(age_sec=86400 + 5))
    assert action.kind is ActionKind.LOGIN
    assert action.expected is True


def test_early_drop_logs_in_but_is_not_expected():
    action = lost(TUE_NOON, state=fresh_session(age_sec=3600))
    assert action.kind is ActionKind.LOGIN
    assert action.expected is False


def test_backoff_sequence_is_capped():
    seq = [backoff_delay_sec(n, CFG) for n in range(1, 8)]
    assert seq == [60, 120, 240, 480, 960, 1800, 1800]
    assert backoff_delay_sec(500, CFG) == 1800
    assert backoff_delay_sec(0, CFG) == 0


def test_backoff_waits_after_a_failed_attempt():
    state = fresh_session()
    record_submit(state, TUE_NOON)
    record_login_failure(state, GatewayUnreachable("x"), TUE_NOON, CFG)
    assert state.locked_out_reason is None
    action = lost(TUE_NOON + timedelta(seconds=30), state=state)
    assert action.kind is ActionKind.WAIT
    assert action.until == TUE_NOON + timedelta(seconds=60)
    assert lost(TUE_NOON + timedelta(seconds=60), state=state).kind is ActionKind.LOGIN


def test_three_submitted_failures_lock_out():
    state = fresh_session()
    for i in range(3):
        t = TUE_NOON + timedelta(hours=i)
        record_submit(state, t)
        record_login_failure(state, GatewayUnreachable("x"), t, CFG)
    assert state.locked_out_reason
    action = lost(TUE_NOON + timedelta(hours=10), state=state)
    assert action.kind is ActionKind.LOCKED_OUT


def test_decide_counts_only_failures_inside_the_window():
    now = TUE_NOON
    old = [now - timedelta(hours=30), now - timedelta(hours=26), now - timedelta(hours=25)]
    state = KeeperState(failed_attempts=list(old), consecutive_failures=3)
    assert lost(now, state=state).kind is ActionKind.LOGIN
    state.failed_attempts = [now - timedelta(hours=2), now - timedelta(hours=1), now]
    assert lost(now, state=state).kind is ActionKind.LOCKED_OUT


def test_old_failures_do_not_count_when_recording():
    state = fresh_session()
    for t in (TUE_NOON - timedelta(hours=40), TUE_NOON - timedelta(hours=39)):
        record_submit(state, t)
    record_submit(state, TUE_NOON)
    record_login_failure(state, GatewayUnreachable("x"), TUE_NOON, CFG)
    assert state.locked_out_reason is None


@pytest.mark.parametrize(
    "exc",
    [BadCredentials("x"), SecondFactorRequired("x"), ManualStepRequired("x"), NotPaperAccount("x")],
)
def test_fatal_errors_lock_out_after_one_attempt(exc):
    state = fresh_session()
    record_submit(state, TUE_NOON)
    record_login_failure(state, exc, TUE_NOON, CFG)
    assert state.locked_out_reason
    assert type(exc).__name__ in state.locked_out_reason
    action = lost(TUE_NOON + timedelta(days=1), state=state)
    assert action.kind is ActionKind.LOCKED_OUT


def test_lockout_reason_does_not_leak_exception_text():
    state = KeeperState()
    record_login_failure(state, BadCredentials("hunter2"), TUE_NOON, CFG)
    assert "hunter2" not in state.locked_out_reason


def test_gateway_down_never_logs_in():
    for state in (KeeperState(), fresh_session(), fresh_session(age_sec=999999)):
        for now in (TUE_NOON, ny(2025, 6, 14, 3, 0), ny(2025, 6, 11, 0, 50)):
            action = decide(state, Observation.GATEWAY_DOWN, now, CFG)
            assert action.kind is ActionKind.WAIT


def test_gateway_down_with_lockout_is_not_login_either():
    state = KeeperState(locked_out_reason="x")
    assert decide(state, Observation.GATEWAY_DOWN, TUE_NOON, CFG).kind is not ActionKind.LOGIN


def test_success_resets_counters():
    state = fresh_session()
    record_submit(state, TUE_NOON)
    record_login_failure(state, GatewayUnreachable("x"), TUE_NOON, CFG)
    later = TUE_NOON + timedelta(minutes=5)
    record_submit(state, later)
    record_login_success(state, later)
    assert state.failed_attempts == []
    assert state.consecutive_failures == 0
    assert state.last_login_at == later
    assert state.authenticated_since == later


def test_clear_lockout_returns_fresh_state():
    state = KeeperState(locked_out_reason="x", consecutive_failures=2)
    cleared = clear_lockout(state)
    assert cleared.locked_out_reason is None
    assert cleared.consecutive_failures == 0
    assert cleared.failed_attempts == []


def test_state_round_trips(tmp_path):
    path = tmp_path / "logs" / "keeper_state.json"
    state = KeeperState(
        last_login_at=TUE_NOON,
        authenticated_since=TUE_NOON,
        failed_attempts=[TUE_NOON, TUE_NOON + timedelta(seconds=5)],
        locked_out_reason="BadCredentials",
        last_alert_at={"session_lost": TUE_NOON},
        consecutive_failures=2,
        last_attempt_at=TUE_NOON,
    )
    save_state(path, state)
    loaded, warning = load_state(path)
    assert warning is None
    assert loaded == state
    json.loads(path.read_text())


def test_state_file_mode_600_and_no_temp_left(tmp_path):
    path = tmp_path / "keeper_state.json"
    save_state(path, KeeperState())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    save_state(path, KeeperState(consecutive_failures=1))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["keeper_state.json"]


def test_missing_state_file_is_fresh_and_unlocked(tmp_path):
    state, warning = load_state(tmp_path / "nope.json")
    assert state == KeeperState()
    assert warning is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"failed_attempts": "x"}',
        '{"failed_attempts": ["not a date"]}',
        '{"consecutive_failures": "many"}',
        '{"last_login_at": "2025-06-10T12:00:00"}',
    ],
)
def test_corrupt_state_file_is_locked_out(tmp_path, content):
    path = tmp_path / "keeper_state.json"
    path.write_text(content)
    state, warning = load_state(path)
    assert warning
    assert state.locked_out_reason
    assert "reset-lockout" in state.locked_out_reason
    assert lost(TUE_NOON, state=state).kind is ActionKind.LOCKED_OUT


def test_unreadable_state_file_is_locked_out(tmp_path):
    state, warning = load_state(tmp_path)  # a directory, not a file
    assert warning
    assert state.locked_out_reason


def test_alert_cooldown():
    from live.keeper_policy import mark_alert, should_alert

    state = KeeperState()
    assert should_alert(state, "k", TUE_NOON, 3600)
    mark_alert(state, "k", TUE_NOON)
    assert not should_alert(state, "k", TUE_NOON + timedelta(seconds=3599), 3600)
    assert should_alert(state, "k", TUE_NOON + timedelta(seconds=3600), 3600)

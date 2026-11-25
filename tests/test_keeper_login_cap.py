"""Login cap per rolling window, short session backoff, and the persisted submit list."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import config
from core.config import KeeperConfig
from live.keeper_policy import (
    ActionKind,
    KeeperState,
    Observation,
    decide,
    load_state,
    record_login_success,
    record_submit,
    save_state,
)
from tests.test_cpgw_keeper import Env

NY = ZoneInfo("America/New_York")
# 2025-06-10 is a Tuesday, outside every no-login window.
TUE_NOON = datetime(2025, 6, 10, 12, 0, tzinfo=NY)
CFG = KeeperConfig()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---- config ----


def test_new_keys_have_defaults_and_ship_in_yaml():
    assert KeeperConfig().max_logins_per_window == 6
    assert KeeperConfig().min_session_sec == 600
    shipped = config.load_keeper_config()
    assert shipped.max_logins_per_window == 6
    assert shipped.min_session_sec == 600


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_logins_per_window", 0),
        ("max_logins_per_window", True),
        ("max_logins_per_window", 2.5),
        ("max_logins_per_window", "6"),
        ("min_session_sec", 0),
        ("min_session_sec", -1),
        ("min_session_sec", False),
    ],
)
def test_bad_new_values_name_the_key(key, value):
    with pytest.raises(ValueError, match=f"keeper.{key}"):
        KeeperConfig(**{key: value})


# ---- the cap in decide ----


def test_cap_reached_gives_locked_out_even_after_successes():
    state = KeeperState()
    for i in range(6):
        t = TUE_NOON - timedelta(hours=1) + timedelta(minutes=i * 11)
        record_submit(state, t)
        record_login_success(state, t)
    assert state.failed_attempts == []  # success cleared the failure list
    action = decide(state, Observation.NOT_AUTHENTICATED, TUE_NOON, CFG)
    assert action.kind is ActionKind.LOCKED_OUT
    assert "6" in action.reason
    assert "24 h" in action.reason


def test_cap_not_reached_with_five_submits():
    state = KeeperState()
    for i in range(5):
        record_submit(state, TUE_NOON - timedelta(hours=2) + timedelta(minutes=i * 11))
    state.failed_attempts = []  # as after successful logins
    action = decide(state, Observation.NOT_AUTHENTICATED, TUE_NOON, CFG)
    assert action.kind is not ActionKind.LOCKED_OUT


def test_cap_rolls_off_after_the_window():
    state = KeeperState()
    for i in range(6):
        record_submit(state, TUE_NOON - timedelta(hours=1) + timedelta(minutes=i))
    later = TUE_NOON + timedelta(hours=25)
    action = decide(state, Observation.NOT_AUTHENTICATED, later, CFG)
    assert action.kind is not ActionKind.LOCKED_OUT


def test_cap_does_not_stop_tickling_or_gateway_down_waits():
    state = KeeperState()
    for i in range(6):
        record_submit(state, TUE_NOON - timedelta(minutes=i))
    assert decide(state, Observation.AUTHENTICATED, TUE_NOON, CFG).kind is ActionKind.TICKLE
    assert decide(state, Observation.GATEWAY_DOWN, TUE_NOON, CFG).kind is ActionKind.WAIT


# ---- short sessions ----


def test_short_session_before_the_next_login_applies_backoff():
    state = KeeperState(last_login_at=TUE_NOON, authenticated_since=TUE_NOON)
    relogin = TUE_NOON + timedelta(minutes=5)
    record_login_success(state, relogin, CFG)
    assert state.consecutive_failures == 1
    assert state.last_attempt_at == relogin
    soon = relogin + timedelta(seconds=30)
    action = decide(state, Observation.NOT_AUTHENTICATED, soon, CFG)
    assert action.kind is ActionKind.WAIT
    assert action.reason == "backoff"


def test_second_short_session_backs_off_longer():
    state = KeeperState(last_login_at=TUE_NOON, authenticated_since=TUE_NOON)
    t1 = TUE_NOON + timedelta(minutes=5)
    record_login_success(state, t1, CFG)
    t2 = t1 + timedelta(minutes=2)
    record_login_success(state, t2, CFG)
    assert state.consecutive_failures == 2


def test_long_session_resets_counters():
    state = KeeperState(
        last_login_at=TUE_NOON, authenticated_since=TUE_NOON, consecutive_failures=2
    )
    record_login_success(state, TUE_NOON + timedelta(minutes=11), CFG)
    assert state.consecutive_failures == 0
    assert state.last_attempt_at is None


def test_first_login_ever_is_not_a_short_session():
    state = KeeperState()
    record_login_success(state, TUE_NOON, CFG)
    assert state.consecutive_failures == 0


# ---- persistence ----


def test_submit_times_round_trip(tmp_path):
    path = tmp_path / "keeper_state.json"
    state = KeeperState(submit_times=[TUE_NOON, TUE_NOON + timedelta(minutes=3)])
    save_state(path, state)
    loaded, warning = load_state(path)
    assert warning is None
    assert loaded.submit_times == state.submit_times
    assert "submit_times" in json.loads(path.read_text())


def test_old_state_file_without_the_field_still_loads(tmp_path):
    path = tmp_path / "keeper_state.json"
    path.write_text(json.dumps({"failed_attempts": [], "consecutive_failures": 1}))
    loaded, warning = load_state(path)
    assert warning is None
    assert loaded.submit_times == []
    assert loaded.consecutive_failures == 1


@pytest.mark.parametrize("bad", ['"x"', '["not a date"]', "[null]"])
def test_bad_submit_times_is_a_corrupt_file(tmp_path, bad):
    path = tmp_path / "keeper_state.json"
    path.write_text('{"submit_times": ' + bad + "}")
    loaded, warning = load_state(path)
    assert warning
    assert loaded.locked_out_reason


# ---- keeper wiring ----


def test_keeper_stops_logging_in_at_the_cap_and_alerts(env):
    keeper = env.keeper()
    for _ in range(6):
        env.client.authenticated = False  # the session dropped again
        keeper.tick()
        env.clock.advance(minutes=11)  # long enough to not count as a short session
    assert env.login_calls == 6
    env.client.authenticated = False
    keeper.tick()
    assert env.login_calls == 6
    alerts = [n for n in env.notifier.sent if n["key"].startswith("lockout:")]
    assert len(alerts) == 1
    assert "locked out" in alerts[0]["text"]
    assert "6" in alerts[0]["text"]
    events = [m for _, _, m in env.storage.events if "locked out" in m]
    assert events

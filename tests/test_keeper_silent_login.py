"""A private login() that ignores on_submit must still be counted by the keeper."""

from __future__ import annotations

import pytest

from core.auth_types import GatewayUnreachable, LoginSetupError, LoginTimeout
from tests.test_cpgw_keeper import FAKE_COOKIE, Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _run(env, calls, ticks=40):
    k = env.keeper()
    k.start()
    for _ in range(ticks):
        k.tick()
        env.clock.advance(seconds=1801)
    return k


def test_login_that_ignores_on_submit_and_times_out_locks_out_after_three(env):
    calls = []

    def silent_login(creds, *, port, **kw):
        calls.append(1)
        raise LoginTimeout("timed out after the submit")

    env.login = silent_login
    _run(env, calls)
    st = env.state()
    assert len(calls) == 3
    assert st.locked_out_reason
    assert len(st.submit_times) == 3


def test_login_that_ignores_on_submit_and_succeeds_still_records_a_submit(env):
    def silent_login(creds, *, port, **kw):
        env.client.authenticated = True
        return FAKE_COOKIE

    env.login = silent_login
    k = env.keeper()
    k.start()
    k.tick()
    assert len(env.state().submit_times) == 1


def test_warn_about_missing_submit_report_once_per_process(env):
    def silent_login(creds, *, port, **kw):
        raise LoginTimeout("x")

    env.login = silent_login
    _run(env, [], ticks=6)
    warns = [m for lvl, _, m in env.storage.events if "does not report submits" in m]
    assert len(warns) == 1


@pytest.mark.parametrize("exc", [LoginSetupError("setup"), GatewayUnreachable("down")])
def test_errors_that_guarantee_no_submit_are_not_counted(env, exc):
    def silent_login(creds, *, port, **kw):
        raise exc

    env.login = silent_login
    k = env.keeper()
    k.start()
    k.tick()
    assert env.state().submit_times == []


def test_not_implemented_stub_is_not_counted(env):
    def stub(creds, *, port, **kw):
        raise NotImplementedError

    env.login = stub
    k = env.keeper()
    k.start()
    k.tick()
    assert env.state().submit_times == []


def test_login_that_reports_the_submit_is_counted_once(env):
    k = env.keeper()
    k.start()
    k.tick()
    assert len(env.state().submit_times) == 1
    assert not [m for _, _, m in env.storage.events if "does not report submits" in m]

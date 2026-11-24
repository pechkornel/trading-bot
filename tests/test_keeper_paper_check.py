"""The paper check retries, and only a real non-DU account id means "not paper"."""

from __future__ import annotations

import pytest

from core.cpgw_client import CPGWError
from tests.test_cpgw_keeper import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def script_accounts(env, items):
    """list_accounts pops scripted items (exception or list); the last one repeats."""
    queue = list(items)

    def list_accounts():
        env.client.calls.append("list_accounts")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    env.client.list_accounts = list_accounts


def make_keeper(env):
    keeper = env.keeper()
    keeper.sleeps = []
    keeper._sleep = keeper.sleeps.append
    return keeper


def paper_alerts(env):
    return [n for n in env.notifier.sent if n["key"] == "paper_check_unsure"]


def test_warm_up_runs_before_the_accounts_call(env):
    keeper = make_keeper(env)
    keeper.tick()
    calls = env.client.calls
    assert calls.index("bootstrap") < calls.index("list_accounts")


def test_transient_error_then_paper_account_is_fine(env):
    script_accounts(env, [CPGWError("boom"), [{"id": "DU1234567"}]])
    keeper = make_keeper(env)
    keeper.tick()
    assert "logout" not in env.client.calls
    assert env.state().locked_out_reason is None
    assert env.session_file.exists()
    assert keeper.sleeps == [2.0]


def test_transient_empty_list_then_paper_account_is_fine(env):
    script_accounts(env, [[], [], [{"id": "DU1234567"}]])
    keeper = make_keeper(env)
    keeper.tick()
    assert "logout" not in env.client.calls
    assert env.state().locked_out_reason is None


def test_real_non_paper_account_still_logs_out_and_locks_out(env):
    script_accounts(env, [CPGWError("boom"), [{"id": "U7654321"}]])
    keeper = make_keeper(env)
    keeper.tick()
    assert "logout" in env.client.calls
    assert env.state().locked_out_reason
    assert not env.session_file.exists()


@pytest.mark.parametrize("answer", [CPGWError("boom"), []])
def test_persistent_failure_does_not_log_out_or_lock_out_and_alerts_once(env, answer):
    script_accounts(env, [answer])
    keeper = make_keeper(env)
    keeper.tick()
    for _ in range(3):
        env.clock.advance(seconds=60)
        keeper.tick()
    assert "logout" not in env.client.calls
    assert env.state().locked_out_reason is None
    assert env.login_calls == 1
    assert len(paper_alerts(env)) == 1
    assert env.client.calls.count("list_accounts") >= 3 * 4


def test_persistent_failure_on_an_adopted_session_does_not_log_out(env):
    env.client.authenticated = True
    script_accounts(env, [CPGWError("boom")])
    keeper = make_keeper(env)
    keeper.start()
    keeper.tick()
    assert "logout" not in env.client.calls
    assert env.state().locked_out_reason is None
    assert env.login_calls == 0
    assert len(paper_alerts(env)) == 1


def test_a_later_tick_verifies_the_session_after_the_gateway_recovers(env):
    script_accounts(env, [CPGWError("boom")])
    keeper = make_keeper(env)
    keeper.tick()
    assert not keeper._session_verified
    script_accounts(env, [[{"id": "DU1234567"}]])
    keeper.tick()
    assert keeper._session_verified
    assert env.login_calls == 1

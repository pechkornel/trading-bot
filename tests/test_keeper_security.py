"""Regression tests for the Phase 1 security review findings (keeper side)."""

from __future__ import annotations

import pytest

from core.config import KeeperConfig
from core.cpgw_client import CPGWError, NotAuthenticatedError
from live.cpgw_keeper import Keeper
from tests.test_cpgw_keeper import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


class FakeProbe:
    """Cookie-less client: answers auth_status with a scripted result."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0
        self.closed = False

    def auth_status(self):
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def close(self):
        self.closed = True


def keeper_with_probe(env, answer, cfg=None):
    probe = FakeProbe(answer)
    keeper = Keeper(
        once=False,
        keeper_cfg=cfg or KeeperConfig(),
        login_fn=env.login,
        client_factory=lambda cookie, port: env.client,
        probe_factory=lambda port: probe,
        notifier=env.notifier,
        storage=env.storage,
        clock=env.clock,
        state_path=env.state_path,
        session_file=env.session_file,
        sleep=lambda s: None,
        install_signals=False,
    )
    return keeper, probe


def test_stale_cookie_401_but_cookieless_check_authenticated_means_no_login(env):
    keeper, probe = keeper_with_probe(env, {"authenticated": True})
    keeper.tick()  # the stored cookie gets 401
    assert probe.calls == 1
    assert env.login_calls == 0


@pytest.mark.parametrize(
    "answer",
    [CPGWError("boom"), NotAuthenticatedError("401"), None, {"connected": True}, "junk"],
)
def test_inconclusive_cookieless_check_means_no_submit(env, answer):
    keeper, probe = keeper_with_probe(env, answer)
    keeper.tick()
    assert probe.calls == 1
    assert env.login_calls == 0
    assert env.state().failed_attempts == []


def test_explicit_not_authenticated_cookieless_check_allows_login(env):
    keeper, probe = keeper_with_probe(env, {"authenticated": False})
    keeper.tick()
    assert env.login_calls == 1
    assert probe.closed

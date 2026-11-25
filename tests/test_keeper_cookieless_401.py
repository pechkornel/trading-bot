"""keeper.cookieless_401_means_logged_out: how a cookie-less 401 is read."""

from __future__ import annotations

import pytest

from core import config
from core.config import KeeperConfig
from core.cpgw_client import NotAuthenticatedError
from tests.test_cpgw_keeper import Env
from tests.test_keeper_security import keeper_with_probe


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_default_is_true_and_ships_in_yaml():
    assert KeeperConfig().cookieless_401_means_logged_out is True
    assert config.load_keeper_config().cookieless_401_means_logged_out is True


@pytest.mark.parametrize("value", ["false", "no", 0, 1, None, "true"])
def test_value_must_be_a_real_boolean(value):
    with pytest.raises(ValueError, match="keeper.cookieless_401_means_logged_out"):
        KeeperConfig(cookieless_401_means_logged_out=value)


def test_true_lets_a_cookieless_401_allow_login(env):
    keeper, probe = keeper_with_probe(
        env, NotAuthenticatedError("401"), KeeperConfig(cookieless_401_means_logged_out=True)
    )
    keeper.tick()
    assert env.login_calls == 1


def test_false_makes_a_cookieless_401_unsure(env):
    keeper, probe = keeper_with_probe(
        env, NotAuthenticatedError("401"), KeeperConfig(cookieless_401_means_logged_out=False)
    )
    for _ in range(12):
        keeper.tick()
    assert env.login_calls == 0
    assert env.state().submit_times == []
    warns = [m for lvl, _, m in env.storage.events if lvl == "warn" and "no clear answer" in m]
    assert len(warns) == 1
    assert [n for n in env.notifier.sent if n["key"] == "gateway_unsure"]


def test_false_still_allows_login_on_an_explicit_authenticated_false(env):
    keeper, probe = keeper_with_probe(
        env, {"authenticated": False}, KeeperConfig(cookieless_401_means_logged_out=False)
    )
    keeper.tick()
    assert env.login_calls == 1

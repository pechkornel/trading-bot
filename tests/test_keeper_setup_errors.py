"""Permanent setup problems raise LoginSetupError, which locks the keeper out once."""

from __future__ import annotations

import dataclasses
import sys

import pytest

from core import gateway_login
from core.auth_types import IBKRAuthError, LoginSetupError
from live.keeper_policy import FATAL_LOGIN_ERRORS
from tests.test_cpgw_keeper import Env
from tests.test_gateway_login import FakePage, config, creds, run


def test_login_setup_error_is_a_fatal_auth_error():
    assert issubclass(LoginSetupError, IBKRAuthError)
    assert LoginSetupError in FATAL_LOGIN_ERRORS


def test_missing_ca_file_raises_setup_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CPGW_CA_FILE", str(tmp_path / "missing.pem"))
    cfg = gateway_login.GatewayLoginConfig.from_env()
    with pytest.raises(LoginSetupError):
        gateway_login.login_via_gateway_page(creds(), port=5000, config=cfg)


def test_invalid_pem_raises_setup_error():
    with pytest.raises(LoginSetupError):
        gateway_login.spki_pin_from_pem(b"not a cert")


def test_non_local_url_raises_setup_error():
    cfg = dataclasses.replace(config(), base_url="https://example.com")
    with pytest.raises(LoginSetupError):
        run(FakePage(marker="success"), cfg=cfg)


def test_playwright_missing_raises_setup_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(LoginSetupError):
        with gateway_login.playwright_page(config(), "pin"):
            pass


def test_keeper_locks_out_once_with_a_clear_setup_message(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.login_script = [LoginSetupError("CPGW_CA_FILE must name the gateway certificate file")]
    keeper = env.keeper()
    keeper.tick()
    keeper.tick()
    assert env.login_calls == 1
    reason = env.state().locked_out_reason
    assert reason and "setup" in reason
    alerts = [n for n in env.notifier.sent if "setup" in n["text"]]
    assert len(alerts) == 1
    text = alerts[0]["text"]
    assert "not touched" in text
    assert "CPGW_CA_FILE" in text
    assert "--reset-lockout" in text

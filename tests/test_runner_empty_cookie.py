"""An empty cookie in the session file is valid: the gateway holds the session itself."""

from __future__ import annotations

import pytest

import live.runner as runner_mod
import scripts.train_symbol as train_mod


class Stop(Exception):
    pass


class FakeClient:
    def __init__(self, cookie="", port=5000):
        self.cookie = cookie
        self.port = port
        self.account_id = None


def test_runner_accepts_an_empty_cookie(monkeypatch):
    monkeypatch.setattr(runner_mod, "read_session", lambda: {"cookie": "", "account_id": "DU1"})
    monkeypatch.setattr(runner_mod, "CPGWClient", FakeClient)
    client = runner_mod._client_from_session(5000)
    assert client.cookie == ""
    assert client.account_id == "DU1"


def test_runner_still_refuses_a_missing_session_file(monkeypatch):
    monkeypatch.setattr(runner_mod, "read_session", lambda: None)
    with pytest.raises(RuntimeError, match="No CPGW session"):
        runner_mod._client_from_session(5000)


def test_train_accepts_an_empty_cookie(monkeypatch):
    monkeypatch.setattr(train_mod, "read_session", lambda: {"cookie": ""})

    def boom(*a, **kw):
        raise Stop

    monkeypatch.setattr(train_mod, "CPGWClient", boom)
    with pytest.raises(Stop):
        train_mod.main(["--symbol", "SPY"])


def test_train_still_refuses_a_missing_session_file(monkeypatch, capsys):
    monkeypatch.setattr(train_mod, "read_session", lambda: None)
    assert train_mod.main(["--symbol", "SPY"]) == 1
    assert "No CPGW session" in capsys.readouterr().out

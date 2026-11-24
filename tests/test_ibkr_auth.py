"""The private login module is optional: the stub is used only when it is missing."""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from core import ibkr_auth
from core.config import ROOT

PRIVATE = "core.ibkr_auth_private"
FAKE_PASSWORD = "not-a-real-password"


@pytest.fixture
def reload_auth(monkeypatch):
    def _reload(private):
        monkeypatch.setitem(sys.modules, PRIVATE, private)
        return importlib.reload(ibkr_auth)

    yield _reload
    monkeypatch.delitem(sys.modules, PRIVATE, raising=False)
    importlib.reload(ibkr_auth)


def _fake_private(**attrs) -> types.ModuleType:
    module = types.ModuleType(PRIVATE)
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def test_stub_is_used_when_private_module_is_missing(reload_auth, capsys):
    # None in sys.modules makes the import fail like a missing file.
    auth = reload_auth(None)
    assert auth.AUTH_BACKEND == "stub"
    assert auth.login is auth._stub_login
    creds = auth.IbkrCredentials(username="u", password=FAKE_PASSWORD)
    with pytest.raises(NotImplementedError):
        auth.login(creds, port=5000)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_private_login_is_used_and_public_errors_are_kept(reload_auth, capsys):
    calls = []

    def private_login(creds, *, port, **kwargs):
        calls.append((creds.username, port))
        return "cookie"

    auth = reload_auth(_fake_private(login=private_login))
    assert auth.AUTH_BACKEND == "private"
    assert auth.login is private_login
    assert issubclass(auth.MaxLoginAttempts, auth.IBKRAuthError)
    creds = auth.IbkrCredentials(username="u", password=FAKE_PASSWORD)
    assert auth.login(creds, port=5000) == "cookie"
    assert calls == [("u", 5000)]
    captured = capsys.readouterr()
    assert FAKE_PASSWORD not in captured.out + captured.err
    assert captured.out == ""
    assert captured.err == ""


def test_private_exception_classes_replace_the_public_ones(reload_auth):
    class PrivateError(RuntimeError):
        pass

    class PrivateMax(PrivateError):
        pass

    auth = reload_auth(
        _fake_private(
            login=lambda creds, *, port, **kwargs: "c",
            IBKRAuthError=PrivateError,
            MaxLoginAttempts=PrivateMax,
        )
    )
    assert auth.IBKRAuthError is PrivateError
    assert auth.MaxLoginAttempts is PrivateMax


def test_missing_dependency_of_private_module_is_not_hidden(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == PRIVATE:
            raise ModuleNotFoundError("No module named 'other'", name="other")
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(importlib, "import_module", fake_import)
        with pytest.raises(ModuleNotFoundError):
            importlib.reload(ibkr_auth)
    importlib.reload(ibkr_auth)


def test_private_module_without_login_raises(reload_auth):
    with pytest.raises(AttributeError):
        reload_auth(_fake_private())


def test_private_file_is_git_ignored():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "core/ibkr_auth_private.py" in [line.strip() for line in lines]

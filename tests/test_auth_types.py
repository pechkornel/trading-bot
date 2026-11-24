"""Auth types live in a dependency free module and never leak the password."""

from __future__ import annotations

import subprocess
import sys

import pytest

from core import auth_types, ibkr_auth
from core.config import ROOT

FAKE_PASSWORD = "not-a-real-password"
FAKE_USER = "fakeuser123"

NEW_ERRORS = [
    "BadCredentials",
    "SecondFactorRequired",
    "ManualStepRequired",
    "GatewayUnreachable",
    "LoginTimeout",
    "NotPaperAccount",
]


def _creds() -> auth_types.IbkrCredentials:
    return auth_types.IbkrCredentials(username=FAKE_USER, password=FAKE_PASSWORD)


def test_repr_hides_password_and_full_username():
    text = repr(_creds())
    assert FAKE_PASSWORD not in text
    assert FAKE_USER not in text
    assert "fa***" in text


def test_str_hides_password_and_full_username():
    text = str(_creds())
    assert FAKE_PASSWORD not in text
    assert FAKE_USER not in text
    assert "fa***" in text


def test_short_username_is_still_masked():
    text = repr(auth_types.IbkrCredentials(username="a", password=FAKE_PASSWORD))
    assert "a***" in text
    assert FAKE_PASSWORD not in text


def test_credentials_keep_fields_and_default_to_paper():
    creds = _creds()
    assert creds.username == FAKE_USER
    assert creds.password == FAKE_PASSWORD
    assert creds.is_paper is True


@pytest.mark.parametrize("name", NEW_ERRORS)
def test_new_errors_subclass_ibkr_auth_error(name):
    assert issubclass(getattr(auth_types, name), auth_types.IBKRAuthError)


def test_max_login_attempts_subclasses_ibkr_auth_error():
    assert issubclass(auth_types.MaxLoginAttempts, auth_types.IBKRAuthError)


def test_loader_reexports_the_same_objects():
    # The stub is active in tests: there is no private module in the repo.
    assert ibkr_auth.AUTH_BACKEND == "stub"
    assert ibkr_auth.IBKRAuthError is auth_types.IBKRAuthError
    assert ibkr_auth.MaxLoginAttempts is auth_types.MaxLoginAttempts
    assert ibkr_auth.IbkrCredentials is auth_types.IbkrCredentials


def test_auth_types_imports_nothing_from_core():
    code = (
        "import sys, core.auth_types\n"
        "bad = sorted(m for m in sys.modules "
        "if m.startswith('core.') and m != 'core.auth_types')\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""

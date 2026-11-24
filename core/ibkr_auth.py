"""
Broker login (not included in the public version).

The private version of this module performs the automated login to the Interactive
Brokers Client Portal Gateway. It is left out of the public repository on purpose:
it ports a third-party implementation of IBKR's login protocol that is not mine to
publish. The names below keep the rest of the code importable.

To run the bot yourself, log in to the Client Portal Gateway manually in the browser
(https://localhost:5000) and pass the resulting session to the runners, or plug in
your own implementation of `login()`.

Private login module: if a file `core/ibkr_auth_private.py` exists, it is loaded at
the bottom of this module and its `login` replaces the stub. The file is git-ignored
and must never be committed. Contract:

- It must define `login(creds, *, port, **kwargs) -> str`, returning the session cookie.
- It may define `IbkrCredentials`, `IBKRAuthError` and `MaxLoginAttempts`. If it does,
  those replace the public ones. It may also import them from `core.ibkr_auth`.
- Only a missing `core.ibkr_auth_private` falls back to the stub. If the file exists but
  fails to import, the error is raised. A file without `login` raises AttributeError.

`AUTH_BACKEND` is "private" when the private module is active, else "stub". The loader
prints and logs nothing.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass


class IBKRAuthError(RuntimeError):
    """Raised when the login fails."""


class MaxLoginAttempts(IBKRAuthError):
    """Raised after too many failed login attempts."""


@dataclass(frozen=True)
class IbkrCredentials:
    username: str
    password: str
    is_paper: bool = True


def _stub_login(creds: IbkrCredentials, *args, **kwargs):
    raise NotImplementedError(
        "Automated IBKR login is not part of the public version. "
        "Log in to the Client Portal Gateway manually, see the module docstring."
    )


login = _stub_login
AUTH_BACKEND = "stub"

try:
    _private = importlib.import_module("core.ibkr_auth_private")
except ModuleNotFoundError as exc:
    if exc.name != "core.ibkr_auth_private":
        raise
else:
    login = _private.login
    IbkrCredentials = getattr(_private, "IbkrCredentials", IbkrCredentials)
    IBKRAuthError = getattr(_private, "IBKRAuthError", IBKRAuthError)
    MaxLoginAttempts = getattr(_private, "MaxLoginAttempts", MaxLoginAttempts)
    AUTH_BACKEND = "private"

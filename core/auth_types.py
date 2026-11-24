"""Auth types shared by the login loader and the login flow.

This module imports nothing from `core` on purpose. `core.ibkr_auth` loads the private
login module, which imports `core.gateway_login`, which needs these names. Keeping them
here breaks that cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class IBKRAuthError(RuntimeError):
    """Raised when the login fails."""


class MaxLoginAttempts(IBKRAuthError):
    """Raised after too many failed login attempts."""


class BadCredentials(IBKRAuthError):
    """The gateway rejected the username or password."""


class SecondFactorRequired(IBKRAuthError):
    """The login needs a second factor that cannot be supplied headless."""


class ManualStepRequired(IBKRAuthError):
    """The login page asks for a step that needs a person."""


class GatewayUnreachable(IBKRAuthError):
    """The gateway login page could not be reached."""


class LoginTimeout(IBKRAuthError):
    """The login did not finish in time."""


class NotPaperAccount(IBKRAuthError):
    """The logged in account is not a paper account."""


def _mask(username: str) -> str:
    return f"{username[:2]}***"


@dataclass(frozen=True)
class IbkrCredentials:
    username: str
    password: str = field(repr=False)
    is_paper: bool = True

    def __repr__(self) -> str:
        return f"IbkrCredentials(username={_mask(self.username)!r}, is_paper={self.is_paper!r})"

    __str__ = __repr__

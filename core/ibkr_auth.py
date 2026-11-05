"""
Broker login (not included in the public version).

The private version of this module performs the automated login to the Interactive
Brokers Client Portal Gateway. It is left out of the public repository on purpose:
it ports a third-party implementation of IBKR's login protocol that is not mine to
publish. The names below keep the rest of the code importable.

To run the bot yourself, log in to the Client Portal Gateway manually in the browser
(https://localhost:5000) and pass the resulting session to the runners, or plug in
your own implementation of `login()`.
"""

from __future__ import annotations

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


def login(creds: IbkrCredentials, *args, **kwargs):
    raise NotImplementedError(
        "Automated IBKR login is not part of the public version. "
        "Log in to the Client Portal Gateway manually, see the module docstring."
    )

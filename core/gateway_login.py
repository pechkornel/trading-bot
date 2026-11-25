"""Headless login through the gateway's own login page (paper accounts only).

The generic flow lives here so it is tracked and tested. The git-ignored
`core/ibkr_auth_private.py` is only thin wiring, see `deploy/ibkr_auth_private.py.example`.

Rules this module keeps:

- Headless Chromium only (Playwright's headless shell). No X server, no headed mode.
- TLS is never switched off. The browser accepts exactly one public key, the SPKI pin of
  the gateway certificate named by `CPGW_CA_FILE`. Without that file the login refuses
  to run before any browser starts.
- Playwright is imported inside `playwright_page`, so importing this module is cheap and
  works in containers that have no Playwright.
- Credentials only reach `LoginPage.fill` for the two credential fields. Every error text
  is a fixed string. Errors from the page are never chained, because their text could
  hold a filled value.
- The browser only talks to the local gateway origin. Other requests are aborted.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from core.auth_types import (
    BadCredentials,
    GatewayUnreachable,
    IbkrCredentials,
    LoginSetupError,
    LoginTimeout,
    ManualStepRequired,
    NotPaperAccount,
    SecondFactorRequired,
)

log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LoginPage(Protocol):
    def goto(self, url: str) -> None: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def is_checked(self, selector: str) -> bool: ...

    def wait_for_first(self, markers: dict[str, str], timeout_sec: float) -> str | None: ...


# The second-factor answer fields of the gateway login page (card, SMS code, response
# code, temporary code) and the passkey and OTP choice buttons. All of them are in the
# page from the start but hidden; only visible elements count as a marker.
_SECOND_FACTOR_FIELDS = (
    "#xyz-field-bronze-response, #xyz-field-silver-response, #xyz-field-gold-response, "
    "#xyz-field-temp-response, .xyz-passkey-secure-start, .xyz-otp-select-text"
)


def _default_markers() -> dict[str, str]:
    # Checked against the real gateway login page on the server, except the two texts
    # that only appear after a submit. Only visible elements count (see wait_for_first).
    return {
        "success": "text=Client login succeeds",
        "bad_credentials": "text=Invalid username password combination",
        "second_factor": _SECOND_FACTOR_FIELDS,
    }


@dataclass(frozen=True)
class GatewayLoginConfig:
    base_url: str = ""  # empty means https://127.0.0.1:<port>
    cert_file: str | None = None
    username_selector: str = "input[name='username']"
    password_selector: str = "input[name='password']"
    # The page has six submit buttons; only this one is visible on the first screen.
    submit_selector: str = "button.xyz-button-login"
    # The "Trading Mode" switch starts on Live. The flow clicks its label, then requires
    # the hidden checkbox to be checked (Paper) before it types anything. An empty click
    # selector skips this step.
    paper_switch_click_selector: str = "label[for='toggle1']"
    paper_switch_input_selector: str = "#toggle1"
    markers: dict[str, str] = field(default_factory=_default_markers)
    page_timeout_sec: float = 60.0
    api_timeout_sec: float = 30.0

    @classmethod
    def from_env(cls) -> GatewayLoginConfig:
        cert = os.environ.get("CPGW_CA_FILE", "").strip()
        return cls(cert_file=cert or None)


def spki_pin_from_pem(pem: bytes) -> str:
    """Base64 SHA-256 of the certificate's SubjectPublicKeyInfo (Chromium's SPKI pin)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(pem)
        der = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except Exception:
        der = None
    if der is None:
        raise LoginSetupError("the gateway certificate file is not a valid PEM certificate")
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def _require_local(base_url: str) -> None:
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        host = None
    if host not in LOCAL_HOSTS:
        raise LoginSetupError("the gateway login url must be a local address")


def _attempt(fn: Callable[..., object], *args: object) -> str | None:
    """Run one page step. Return an error kind instead of raising, so nothing is chained."""
    try:
        fn(*args)
    except TimeoutError:
        return "timeout"
    except OSError:
        return "unreachable"
    except Exception:
        return "other"
    return None


def _raise_for(kind: str, *, before_submit: bool) -> None:
    if kind == "timeout":
        raise LoginTimeout("the gateway login page timed out")
    if kind == "unreachable" and before_submit:
        raise GatewayUnreachable("the gateway login page could not be reached")
    raise ManualStepRequired("the gateway login page needs a manual step")


def _paper_selected(page: LoginPage, config: GatewayLoginConfig) -> bool:
    result: list[bool] = []
    kind = _attempt(
        lambda: result.append(bool(page.is_checked(config.paper_switch_input_selector)))
    )
    if kind:
        _raise_for(kind, before_submit=True)
    return result[0]


def run_login_flow(
    page: LoginPage,
    creds: IbkrCredentials,
    config: GatewayLoginConfig,
    *,
    on_submit: Callable[[], None],
    is_authenticated: Callable[[], bool],
) -> None:
    if not creds.is_paper:
        raise NotPaperAccount("only paper accounts may log in")
    base_url = config.base_url or "https://127.0.0.1:5000"
    _require_local(base_url)

    kind = _attempt(page.goto, base_url.rstrip("/") + "/")
    if kind:
        _raise_for(kind, before_submit=True)

    switch = config.paper_switch_click_selector
    if switch and not _paper_selected(page, config):
        kind = _attempt(page.click, switch)
        if kind:
            _raise_for(kind, before_submit=True)
        if not _paper_selected(page, config):
            raise ManualStepRequired("the gateway login page did not switch to paper mode")

    for selector, value in (
        (config.username_selector, creds.username),
        (config.password_selector, creds.password),
    ):
        kind = _attempt(page.fill, selector, value)
        if kind:
            _raise_for(kind, before_submit=True)

    # Check the switch once more right before the submit.
    if switch and not _paper_selected(page, config):
        raise ManualStepRequired("the gateway login page did not switch to paper mode")

    on_submit()

    kind = _attempt(page.click, config.submit_selector)
    if kind:
        _raise_for(kind, before_submit=False)

    outcome: list[str | None] = []

    def wait() -> None:
        outcome.append(page.wait_for_first(config.markers, config.page_timeout_sec))

    kind = _attempt(wait)
    if kind:
        _raise_for(kind, before_submit=False)
    marker = outcome[0]

    if marker == "bad_credentials":
        raise BadCredentials("the gateway rejected the username or password")
    if marker == "second_factor":
        raise SecondFactorRequired("the gateway asks for a second factor")
    if is_authenticated():
        return
    raise ManualStepRequired("the gateway login page needs a manual step")


class _PlaywrightPage:
    """Adapter from a Playwright page to `LoginPage`. Errors are mapped to builtins."""

    def __init__(self, page, context, origin: str, timeout_sec: float) -> None:
        self._page = page
        self._context = context
        self._origin = origin
        self._timeout_ms = int(timeout_sec * 1000)

    def goto(self, url: str) -> None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception as exc:
            text = str(exc)
            if "ERR_CONNECTION_REFUSED" in text or "ERR_CONNECTION_RESET" in text:
                raise ConnectionRefusedError("refused") from None
            if type(exc).__name__ == "TimeoutError":
                raise TimeoutError("timeout") from None
            raise RuntimeError("navigation failed") from None

    def fill(self, selector: str, value: str) -> None:
        try:
            self._page.fill(selector, value, timeout=self._timeout_ms)
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise TimeoutError("timeout") from None
            raise RuntimeError("fill failed") from None

    def click(self, selector: str) -> None:
        try:
            self._page.click(selector, timeout=self._timeout_ms)
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise TimeoutError("timeout") from None
            raise RuntimeError("click failed") from None

    def is_checked(self, selector: str) -> bool:
        try:
            return bool(self._page.is_checked(selector, timeout=self._timeout_ms))
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise TimeoutError("timeout") from None
            raise RuntimeError("check failed") from None

    def wait_for_first(self, markers: dict[str, str], timeout_sec: float) -> str | None:
        # Only visible elements count: the login page holds hidden second-factor and
        # error sections from the start, which must not read as an outcome.
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            for name, selector in markers.items():
                try:
                    loc = self._page.locator(selector)
                    if any(loc.nth(i).is_visible() for i in range(loc.count())):
                        return name
                except Exception:
                    raise RuntimeError("marker check failed") from None
            self._page.wait_for_timeout(250)
        return None

    def cookie_header(self) -> str:
        cookies = self._context.cookies(self._origin)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


@contextlib.contextmanager
def playwright_page(config: GatewayLoginConfig, pin: str) -> Iterator[_PlaywrightPage]:
    """Launch headless Chromium pinned to one certificate key. Always closes everything."""
    try:
        from playwright.sync_api import sync_playwright  # lazy: not installed everywhere
    except ImportError:
        raise LoginSetupError("playwright is not installed (see requirements-login.txt)") from None

    # An empty base_url means the default local gateway, as in run_login_flow. Without
    # this the allowed origin would be "://" and every request, the first goto included,
    # would be blocked (the step 12 probe passes the config as login_config() returns it).
    base_url = config.base_url or "https://127.0.0.1:5000"
    _require_local(base_url)
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"

    def route(route, request) -> None:
        host = urlsplit(request.url)
        if f"{host.scheme}://{host.netloc}" == origin:
            route.continue_()
        else:
            log.warning("blocked request to host %s", host.hostname)
            route.abort()

    with sync_playwright() as pw:
        browser = None
        context = None
        try:
            # The sandbox is off on purpose (ADR 0003): Docker's default seccomp profile
            # blocks the user namespaces it needs. Set explicitly so a library default
            # change cannot flip it silently. Pinned by a test.
            browser = pw.chromium.launch(
                headless=True,
                chromium_sandbox=False,
                args=[
                    f"--ignore-certificate-errors-spki-list={pin}",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(service_workers="block", accept_downloads=False)
            context.route("**/*", route)
            page = context.new_page()
            yield _PlaywrightPage(page, context, origin, config.page_timeout_sec)
        finally:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.close()


def login_via_gateway_page(
    creds: IbkrCredentials,
    *,
    port: int,
    config: GatewayLoginConfig | None = None,
    on_submit: Callable[[], None] | None = None,
) -> str:
    """Log in through the gateway's login page and return its cookies as a header string.

    The result may be empty: the gateway holds the session itself.
    """
    config = config or GatewayLoginConfig.from_env()
    if not creds.is_paper:
        raise NotPaperAccount("only paper accounts may log in")
    cert = config.cert_file
    if not cert or not Path(cert).is_file():
        raise LoginSetupError("CPGW_CA_FILE must name the gateway certificate file")
    pin = spki_pin_from_pem(Path(cert).read_bytes())
    if not config.base_url:
        config = replace(config, base_url=f"https://127.0.0.1:{port}")
    _require_local(config.base_url)

    from core.cpgw_client import CPGWClient, CPGWError

    with playwright_page(config, pin) as pw_page:

        def is_authenticated() -> bool:
            deadline = time.monotonic() + config.api_timeout_sec
            while True:
                client = CPGWClient(
                    pw_page.cookie_header(), port=port, host="127.0.0.1", ca_file=config.cert_file
                )
                try:
                    if client.auth_status().get("authenticated"):
                        return True
                except CPGWError:
                    pass
                finally:
                    client.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(2.0)

        run_login_flow(
            pw_page,
            creds,
            config,
            on_submit=on_submit or (lambda: None),
            is_authenticated=is_authenticated,
        )
        return pw_page.cookie_header()

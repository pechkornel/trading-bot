"""The login's own authentication check must verify the gateway certificate."""

from __future__ import annotations

import contextlib

from core import cpgw_client, gateway_login
from tests.test_gateway_login import CERT, FakePage, config, creds


def test_is_authenticated_client_gets_the_ca_file(monkeypatch):
    seen: dict = {}

    class RecordingClient:
        def __init__(self, cookie, **kwargs):
            seen.update(kwargs)

        def auth_status(self):
            return {"authenticated": True}

        def close(self):
            pass

    class CookiePage(FakePage):
        def cookie_header(self):
            return "c=1"

    @contextlib.contextmanager
    def fake_playwright_page(cfg, pin):
        yield CookiePage()

    def fake_flow(page, creds_, cfg, *, on_submit, is_authenticated):
        assert is_authenticated() is True

    monkeypatch.setattr(cpgw_client, "CPGWClient", RecordingClient)
    monkeypatch.setattr(gateway_login, "playwright_page", fake_playwright_page)
    monkeypatch.setattr(gateway_login, "run_login_flow", fake_flow)

    gateway_login.login_via_gateway_page(creds(), port=5000, config=config())
    assert seen["ca_file"] == str(CERT)

"""Tests for the generic headless gateway login flow. No browser, no network.

`tests/fixtures/gateway_cert.pem` is a throwaway self-signed certificate (public part
only, no private key) made once with:
    openssl req -x509 -newkey rsa:2048 -nodes -keyout <discarded> -out gateway_cert.pem
        -days 36500 -subj "/CN=test-gateway" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import datetime
import hashlib
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from core import gateway_login
from core.auth_types import (
    BadCredentials,
    GatewayUnreachable,
    IBKRAuthError,
    IbkrCredentials,
    LoginTimeout,
    ManualStepRequired,
    NotPaperAccount,
    SecondFactorRequired,
)
from core.gateway_login import GatewayLoginConfig, run_login_flow, spki_pin_from_pem

ROOT = Path(__file__).resolve().parent.parent
CERT = ROOT / "tests" / "fixtures" / "gateway_cert.pem"
FAKE_USER = "paperuser-x1"
FAKE_PASSWORD = "not-a-real-password-7f3a"


class FakePage:
    def __init__(self, marker=None, fail=None):
        self.calls: list[tuple] = []
        self.marker = marker
        self.fail = fail or {}

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    def goto(self, url):
        self.calls.append(("goto", url))
        self._maybe_fail("goto")

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))
        self._maybe_fail("fill")

    def click(self, selector):
        self.calls.append(("click", selector))
        self._maybe_fail("click")

    def wait_for_first(self, markers, timeout_sec):
        self.calls.append(("wait", tuple(markers), timeout_sec))
        self._maybe_fail("wait")
        return self.marker


def creds(is_paper=True):
    return IbkrCredentials(FAKE_USER, FAKE_PASSWORD, is_paper)


def config():
    return GatewayLoginConfig(base_url="https://127.0.0.1:5000", cert_file=str(CERT))


def run(page, *, authed=True, on_submit=None, cfg=None, c=None):
    submits = []
    run_login_flow(
        page,
        c or creds(),
        cfg or config(),
        on_submit=on_submit or (lambda: submits.append(1)),
        is_authenticated=lambda: authed,
    )
    return submits


def test_success_requires_api_authenticated():
    assert run(FakePage(marker="success"), authed=True) == [1]


def test_success_marker_with_unauthenticated_api_is_manual_step():
    with pytest.raises(ManualStepRequired):
        run(FakePage(marker="success"), authed=False)


def test_no_marker_but_api_authenticated_is_success():
    run(FakePage(marker=None), authed=True)


def test_no_marker_and_not_authenticated_is_manual_step():
    with pytest.raises(ManualStepRequired):
        run(FakePage(marker=None), authed=False)


def test_unknown_marker_is_manual_step():
    with pytest.raises(ManualStepRequired):
        run(FakePage(marker="captcha"), authed=False)


def test_bad_credentials_marker():
    with pytest.raises(BadCredentials):
        run(FakePage(marker="bad_credentials"), authed=False)


def test_second_factor_marker():
    with pytest.raises(SecondFactorRequired):
        run(FakePage(marker="second_factor"), authed=False)


def test_on_submit_once_and_before_click():
    page = FakePage(marker="success")
    order = []
    orig_click = page.click

    def click(selector):
        order.append("click")
        orig_click(selector)

    page.click = click
    run(page, on_submit=lambda: order.append("submit"))
    assert order == ["submit", "click"]


def test_unreachable_before_form_means_no_submit():
    submits = []
    page = FakePage(fail={"goto": ConnectionRefusedError("refused")})
    with pytest.raises(GatewayUnreachable):
        run(page, on_submit=lambda: submits.append(1))
    assert submits == []
    assert not [c for c in page.calls if c[0] == "click"]


def test_timeout_before_submit_is_login_timeout():
    submits = []
    page = FakePage(fail={"fill": TimeoutError("slow")})
    with pytest.raises(LoginTimeout):
        run(page, on_submit=lambda: submits.append(1))
    assert submits == []


def test_other_error_before_submit_is_manual_step():
    with pytest.raises(ManualStepRequired):
        run(FakePage(fail={"goto": RuntimeError("boom")}))


def test_password_only_goes_to_password_field():
    cfg = config()
    page = FakePage(marker="success")
    run(page, cfg=cfg)
    fills = [c for c in page.calls if c[0] == "fill"]
    assert ("fill", cfg.username_selector, FAKE_USER) in fills
    assert ("fill", cfg.password_selector, FAKE_PASSWORD) in fills
    for call in page.calls:
        if call[0] != "fill":
            assert FAKE_PASSWORD not in repr(call)
            assert FAKE_USER not in repr(call)


def _collect(exc):
    return [str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(exc.args)]


@pytest.mark.parametrize("where", ["goto", "fill", "click", "wait"])
@pytest.mark.parametrize("leak", [FAKE_PASSWORD, FAKE_USER])
def test_errors_never_carry_credentials(where, leak, capsys, caplog):
    page = FakePage(fail={where: RuntimeError(f"failed with {leak} inside")})
    with pytest.raises(IBKRAuthError) as info:
        run(page, authed=False)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    for text in _collect(info.value):
        assert FAKE_PASSWORD not in text
        assert FAKE_USER not in text
    out = capsys.readouterr()
    everything = out.out + out.err + caplog.text
    assert FAKE_PASSWORD not in everything
    assert FAKE_USER not in everything


@pytest.mark.parametrize("marker", ["bad_credentials", "second_factor", "success", None])
def test_outcome_errors_have_no_credentials(marker):
    with pytest.raises(IBKRAuthError) as info:
        run(FakePage(marker=marker), authed=False)
    for text in _collect(info.value):
        assert FAKE_PASSWORD not in text
        assert FAKE_USER not in text


def test_non_paper_account_refused_before_any_page_call():
    page = FakePage(marker="success")
    with pytest.raises(NotPaperAccount):
        run(page, c=creds(is_paper=False))
    assert page.calls == []


def test_non_local_base_url_refused():
    page = FakePage(marker="success")
    cfg = dataclasses.replace(config(), base_url="https://example.com")
    with pytest.raises(IBKRAuthError):
        run(page, cfg=cfg)
    assert page.calls == []


def test_spki_pin_matches_independent_computation():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    pem = CERT.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    expected = base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert spki_pin_from_pem(pem) == expected


def test_spki_pin_changes_with_another_key():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    other = cert.public_bytes(serialization.Encoding.PEM)
    assert spki_pin_from_pem(other) != spki_pin_from_pem(CERT.read_bytes())


def test_spki_pin_rejects_garbage_with_fixed_text():
    with pytest.raises(IBKRAuthError):
        spki_pin_from_pem(b"not a cert")


@pytest.fixture
def no_browser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("browser must not start")

    monkeypatch.setattr(gateway_login, "playwright_page", boom)


def test_from_env_without_ca_file_fails_before_browser(no_browser):
    cfg = GatewayLoginConfig.from_env()
    assert cfg.cert_file is None
    with pytest.raises(IBKRAuthError):
        gateway_login.login_via_gateway_page(creds(), port=5000, config=cfg)


def test_missing_ca_file_fails_before_browser(no_browser, monkeypatch, tmp_path):
    monkeypatch.setenv("CPGW_CA_FILE", str(tmp_path / "missing.pem"))
    cfg = GatewayLoginConfig.from_env()
    with pytest.raises(IBKRAuthError):
        gateway_login.login_via_gateway_page(creds(), port=5000, config=cfg)


def test_default_config_fails_closed(no_browser):
    with pytest.raises(IBKRAuthError):
        gateway_login.login_via_gateway_page(creds(), port=5000)


def test_non_paper_fails_before_browser(no_browser):
    with pytest.raises(NotPaperAccount):
        gateway_login.login_via_gateway_page(creds(False), port=5000, config=config())


def test_no_blanket_tls_bypass_or_headed_mode_in_source():
    src = (ROOT / "core" / "gateway_login.py").read_text()
    assert "ignore_https_errors" not in src.replace("never ignore_https_errors", "")
    assert "headless=False" not in src
    assert "xvfb" not in src.lower()


def test_import_does_not_load_playwright():
    code = "import sys, core.gateway_login; sys.exit(1 if 'playwright' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_example_file_defines_login_with_documented_signature():
    tree = ast.parse((ROOT / "deploy" / "ibkr_auth_private.py.example").read_text())
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "login"]
    assert len(funcs) == 1
    args = funcs[0].args
    assert [a.arg for a in args.args] == ["creds"]
    assert [a.arg for a in args.kwonlyargs] == ["port"]
    assert args.kwarg is not None


def test_requirements_login_pins_playwright_and_keeps_it_out_of_base():
    login_reqs = (ROOT / "requirements-login.txt").read_text()
    assert "-r requirements.txt" in login_reqs
    assert "playwright==" in login_reqs
    assert "playwright" not in (ROOT / "requirements.txt").read_text()


def _pinned_playwright() -> str:
    for line in (ROOT / "requirements-login.txt").read_text().splitlines():
        if line.startswith("playwright=="):
            return line.split("==", 1)[1].strip()
    raise AssertionError("playwright is not pinned with ==")


def test_playwright_pin_is_exact_and_not_stale():
    version = _pinned_playwright()
    parts = version.split(".")
    assert all(p.isdigit() for p in parts), version
    # 1.55.0 was the pin before the sandbox review; require a newer minor.
    assert (int(parts[0]), int(parts[1])) >= (1, 64)


def test_docs_name_the_pinned_playwright_version():
    version = _pinned_playwright()
    for rel in ("deploy/README.md", "docs/adr/0003-headless-gateway-login-and-file-secrets.md"):
        text = (ROOT / rel).read_text()
        assert f"Playwright {version}" in text or f"playwright=={version}" in text, rel


def test_launch_sets_chromium_sandbox_explicitly_off(monkeypatch):
    """Pins the decision in ADR 0003: the sandbox is off on purpose, never by library default.

    A fake playwright.sync_api records the launch arguments, so no browser is needed.
    """
    import contextlib
    import types

    seen = {}

    class FakeChromium:
        def launch(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop after launch")

    class FakePw:
        chromium = FakeChromium()

    @contextlib.contextmanager
    def fake_sync_playwright():
        yield FakePw()

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    with pytest.raises(RuntimeError, match="stop after launch"):
        with gateway_login.playwright_page(config(), "pin"):
            pass

    assert seen["chromium_sandbox"] is False
    assert seen["headless"] is True
    assert "--ignore-certificate-errors-spki-list=pin" in seen["args"]
    assert not any("sandbox" in a for a in seen["args"])

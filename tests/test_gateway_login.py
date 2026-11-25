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


PAPER_LABEL = "label[for='toggle1']"
PAPER_INPUT = "#toggle1"


class FakePage:
    """The login page as seen on the server: the paper switch starts on Live."""

    def __init__(self, marker=None, fail=None, paper=False, switch_works=True):
        self.calls: list[tuple] = []
        self.marker = marker
        self.fail = fail or {}
        self.paper = paper
        self.switch_works = switch_works

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
        if selector == PAPER_LABEL:
            self._maybe_fail("paper_click")
            if self.switch_works:
                self.paper = not self.paper
            return
        self._maybe_fail("click")

    def is_checked(self, selector):
        self.calls.append(("is_checked", selector))
        self._maybe_fail("is_checked")
        return self.paper if selector == PAPER_INPUT else False

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
    # paper=True: no switch click, so only the login click is recorded.
    page = FakePage(marker="success", paper=True)
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


def test_example_file_defines_login_config_and_login_uses_it():
    tree = ast.parse((ROOT / "deploy" / "ibkr_auth_private.py.example").read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "login_config" in funcs
    args = funcs["login_config"].args
    assert not args.args and not args.kwonlyargs
    called = {
        n.func.id
        for n in ast.walk(funcs["login"])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "login_config" in called


def test_readme_probe_uses_login_config_from_the_private_module():
    text = (ROOT / "deploy" / "README.md").read_text()
    assert "from core import ibkr_auth_private" in text
    assert "login_config" in text


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


def _fake_playwright_capturing_route(monkeypatch, seen):
    import contextlib
    import types

    class FakeContext:
        def route(self, pattern, handler):
            seen["route"] = handler

        def new_page(self):
            return object()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, **kwargs):
            seen["launched"] = True
            return FakeBrowser()

    class FakePw:
        chromium = FakeChromium()

    @contextlib.contextmanager
    def fake_sync_playwright():
        yield FakePw()

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)


class _FakeRoute:
    def __init__(self):
        self.result = None

    def continue_(self):
        self.result = "continue"

    def abort(self):
        self.result = "abort"


class _FakeRequest:
    def __init__(self, url):
        self.url = url


def test_empty_base_url_allows_the_default_local_origin(monkeypatch):
    # The step 12 probe passes a config with base_url "", like the example login_config().
    seen = {}
    _fake_playwright_capturing_route(monkeypatch, seen)
    from dataclasses import replace

    cfg = replace(config(), base_url="")
    with gateway_login.playwright_page(cfg, "pin"):
        pass
    for url, expected in (
        ("https://127.0.0.1:5000/sso/Login", "continue"),
        ("https://example.com/x", "abort"),
    ):
        r = _FakeRoute()
        seen["route"](r, _FakeRequest(url))
        assert r.result == expected, url


def test_playwright_page_refuses_a_non_local_base_url_before_launch(monkeypatch):
    seen = {}
    _fake_playwright_capturing_route(monkeypatch, seen)
    from dataclasses import replace

    cfg = replace(config(), base_url="https://gateway.example.com:5000")
    with pytest.raises(gateway_login.LoginSetupError):
        with gateway_login.playwright_page(cfg, "pin"):
            pass
    assert "launched" not in seen


# --- Paper switch and visible-only markers (seen on the server, 2026-11-25) -----------


def _names(page):
    return [c[0] if c[0] != "click" else f"click:{c[1]}" for c in page.calls]


def test_defaults_match_the_real_login_page():
    cfg = GatewayLoginConfig()
    assert cfg.paper_switch_click_selector == PAPER_LABEL
    assert cfg.paper_switch_input_selector == PAPER_INPUT
    # Only one of the six submit buttons on the page is visible: the main login button.
    assert cfg.submit_selector == "button.xyz-button-login"
    # The hidden second-factor sections hold the word "Challenge" before any submit.
    assert "Challenge" not in cfg.markers["second_factor"]
    for field in ("#xyz-field-silver-response", "#xyz-field-bronze-response"):
        assert field in cfg.markers["second_factor"]


def test_flow_switches_to_paper_before_typing_and_submitting():
    page = FakePage(marker="success", paper=False)
    order = []
    run(page, on_submit=lambda: order.append(len(page.calls)))
    names = _names(page)
    switch = names.index(f"click:{PAPER_LABEL}")
    first_fill = names.index("fill")
    assert names[0] == "goto"
    assert switch < first_fill
    assert page.paper is True
    # checked again right before the submit
    assert names[order[0] - 1] == "is_checked"
    assert names.index("click:button.xyz-button-login") >= order[0]


def test_flow_leaves_an_already_selected_paper_switch_alone():
    page = FakePage(marker="success", paper=True)
    run(page)
    assert f"click:{PAPER_LABEL}" not in _names(page)
    assert page.paper is True


def test_switch_that_does_not_flip_stops_before_any_credential_or_submit():
    page = FakePage(marker="success", paper=False, switch_works=False)
    submits = []
    with pytest.raises(ManualStepRequired) as err:
        run(page, on_submit=lambda: submits.append(1))
    assert submits == []
    assert not any(c[0] == "fill" for c in page.calls)
    assert "paper" in str(err.value)


@pytest.mark.parametrize("where", ["paper_click", "is_checked"])
def test_switch_errors_stop_before_any_credential_or_submit(where):
    page = FakePage(marker="success", fail={where: RuntimeError(FAKE_PASSWORD)})
    submits = []
    with pytest.raises(IBKRAuthError) as err:
        run(page, on_submit=lambda: submits.append(1))
    assert submits == []
    assert not any(c[0] == "fill" for c in page.calls)
    assert FAKE_PASSWORD not in str(err.value)


def test_empty_switch_selector_skips_the_switch_step():
    page = FakePage(marker="success", paper=False)
    cfg = dataclasses.replace(config(), paper_switch_click_selector="")
    run(page, cfg=cfg)
    assert not any(c[0] == "is_checked" for c in page.calls)


class _Loc:
    def __init__(self, visible):
        self._visible = visible

    def count(self):
        return len(self._visible)

    def nth(self, i):
        loc = _Loc([])
        loc.is_visible = lambda: self._visible[i]
        return loc


class _RawPage:
    def __init__(self, elements):
        self.elements = elements  # selector -> list of visibility flags
        self.waits = 0

    def locator(self, selector):
        return _Loc(self.elements.get(selector, []))

    def wait_for_timeout(self, ms):
        self.waits += 1


def _adapter(elements):
    return gateway_login._PlaywrightPage(_RawPage(elements), None, "https://127.0.0.1:5000", 1.0)


def test_markers_ignore_hidden_elements():
    # Five hidden "second factor" elements exist on the page before any submit.
    page = _adapter({"2fa": [False] * 5})
    assert page.wait_for_first({"second_factor": "2fa"}, timeout_sec=0.3) is None


def test_markers_report_the_first_visible_one():
    page = _adapter({"2fa": [False, False], "ok": [False, True]})
    assert page.wait_for_first({"second_factor": "2fa", "success": "ok"}, 1.0) == "success"

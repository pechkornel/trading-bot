"""CPGWClient TLS verification: unchanged without CPGW_CA_FILE, pinned with it."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from core import cpgw_client
from core.cpgw_client import CPGWClient, CPGWError

FIXTURE_CERT = Path(__file__).parent / "fixtures" / "gateway_cert.pem"


@pytest.fixture
def captured(monkeypatch):
    """Capture the kwargs passed to httpx.Client without any network."""
    seen: dict = {}
    real = httpx.Client

    def fake_client(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(cpgw_client.httpx, "Client", fake_client)
    return seen


def test_without_ca_file_verify_stays_off(captured, monkeypatch):
    monkeypatch.delenv("CPGW_CA_FILE", raising=False)
    CPGWClient("c=1")
    assert captured["verify"] is False


def test_empty_ca_file_env_keeps_verify_off(captured, monkeypatch):
    monkeypatch.setenv("CPGW_CA_FILE", "  ")
    CPGWClient("c=1")
    assert captured["verify"] is False


def test_env_ca_file_gives_checking_context(captured, monkeypatch):
    monkeypatch.setenv("CPGW_CA_FILE", str(FIXTURE_CERT))
    CPGWClient("c=1")
    ctx = captured["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_argument_overrides_env(captured, monkeypatch):
    monkeypatch.setenv("CPGW_CA_FILE", "/nonexistent/ignored.pem")
    CPGWClient("c=1", ca_file=str(FIXTURE_CERT))
    assert isinstance(captured["verify"], ssl.SSLContext)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_works_for_both_local_host_names(captured, monkeypatch, host):
    monkeypatch.setenv("CPGW_CA_FILE", str(FIXTURE_CERT))
    client = CPGWClient("c=1", host=host)
    assert client._client.base_url.host == host
    assert captured["verify"].check_hostname is True


def test_missing_ca_file_raises_without_leaking_path_content(monkeypatch):
    monkeypatch.setenv("CPGW_CA_FILE", "/nonexistent/secret-dir/gw.pem")
    with pytest.raises(CPGWError) as exc:
        CPGWClient("c=1")
    assert "secret-dir" not in str(exc.value)
    assert "CPGW_CA_FILE" in str(exc.value)


def test_unparseable_ca_file_raises_cpgw_error(tmp_path, monkeypatch):
    bad = tmp_path / "bad.pem"
    bad.write_text("TOPSECRETCONTENT not a certificate")
    monkeypatch.setenv("CPGW_CA_FILE", str(bad))
    with pytest.raises(CPGWError) as exc:
        CPGWClient("c=1")
    assert "TOPSECRETCONTENT" not in str(exc.value)

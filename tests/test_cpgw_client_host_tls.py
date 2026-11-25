"""A non-loopback gateway host needs a CA file; loopback keeps the unverified fallback."""

from __future__ import annotations

import pytest

from core.cpgw_client import CPGWClient, CPGWError


@pytest.mark.parametrize("host", ["gateway.example.com", "192.168.1.5", "10.0.0.2", "::2"])
def test_non_loopback_host_without_ca_file_is_refused(monkeypatch, host):
    monkeypatch.delenv("CPGW_CA_FILE", raising=False)
    with pytest.raises(CPGWError, match="CPGW_CA_FILE"):
        CPGWClient("c=1", host=host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_host_keeps_the_unverified_fallback(monkeypatch, host):
    monkeypatch.delenv("CPGW_CA_FILE", raising=False)
    client = CPGWClient("c=1", host=host)
    client.close()

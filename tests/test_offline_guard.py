"""The conftest network guard must stay active: these tests fail if it is removed."""

import socket
from urllib.request import urlopen

import pytest


def test_dns_lookup_is_blocked():
    with pytest.raises(RuntimeError, match="network access in tests"):
        socket.getaddrinfo("example.com", 443)


def test_socket_connect_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match="network access in tests"):
            s.connect(("127.0.0.1", 9))
        with pytest.raises(RuntimeError, match="network access in tests"):
            s.connect_ex(("127.0.0.1", 9))


def test_create_connection_is_blocked():
    with pytest.raises(RuntimeError, match="network access in tests"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)


def test_urlopen_is_blocked():
    with pytest.raises(RuntimeError, match="network access in tests"):
        urlopen("https://example.com", timeout=1)

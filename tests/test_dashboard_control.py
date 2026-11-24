"""Control page: starting equity as the fallback and the last net liquidation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from live.storage import Storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "bot.sqlite")
    monkeypatch.setattr(dash, "_storage", lambda: storage)
    monkeypatch.setattr(dash, "list_models", lambda: [])
    monkeypatch.setattr(dash.procmgr, "list_processes", lambda: [])
    return TestClient(dash.app), storage


def test_control_page_shows_starting_equity_as_fallback(client):
    c, _ = client
    html = c.get("/control").text
    assert "Starting equity $100000 (fallback)" in html
    assert "last net liquidation" not in html


def test_control_page_shows_last_net_liquidation(client):
    c, storage = client
    storage.log_equity(120000.0)
    storage.log_equity(123456.78)
    html = c.get("/control").text
    assert "Starting equity $100000 (fallback)" in html
    assert "last net liquidation $123456.78 at " in html

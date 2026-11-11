"""Shared test setup: offline guard, .env isolation and small factories."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pandas as pd
import pytest

from core.macro import MacroSnapshot

_ENV_KEYS = (
    "PAPER",
    "IB_PORT",
    "IB_HOST",
    "IB_CLIENT_ID",
    "DRY_RUN",
    "ALPHA_VANTAGE_API_KEY",
)


def _blocked(*_args, **_kwargs):
    raise RuntimeError("network access in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly on any socket connect or DNS lookup."""
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never read a developer's .env or leak its values into os.environ."""
    monkeypatch.setattr("core.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Results must not depend on the shell environment."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_macro(**overrides) -> MacroSnapshot:
    """A MacroSnapshot with every reading None unless overridden."""
    fields = dict(
        yield_curve=None,
        yield_curve_trend_60d=None,
        fed_funds=None,
        fed_funds_trend_60d=None,
        vix=None,
        vix_60d_mean=None,
        dxy=None,
        dxy_trend_60d=None,
        credit_ratio=None,
        credit_ratio_trend_60d=None,
        as_of=datetime(2026, 11, 13, tzinfo=timezone.utc),
        complete=False,
    )
    fields.update(overrides)
    return MacroSnapshot(**fields)


def series(values) -> pd.Series:
    return pd.Series(list(values), dtype=float)


def linear_closes(start: float, step: float, n: int) -> pd.Series:
    return series(start + step * i for i in range(n))

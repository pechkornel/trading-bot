"""--live needs a paper account id (keeper prefix) in the session file. Dry run is unchanged."""

from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

import live.portfolio_runner as pr
import live.runner as rn
from core.config import (
    CoreSleeveConfig,
    PortfolioConfig,
    SentimentSatelliteConfig,
    TacticalSleeveConfig,
)
from live.account_guard import require_paper_account_for_live


class FakeStorage:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def log_event(self, *args, **kwargs):
        self.events.append(args)


class FakeClient:
    def __init__(self, cookie="", port=5000):
        self.cookie = cookie
        self.port = port
        self.account_id = None


@pytest.fixture(autouse=True)
def no_signal_handlers(monkeypatch):
    monkeypatch.setattr(signal, "signal", lambda *a, **kw: None)


# ---- the guard itself ----


def test_guard_allows_a_paper_account_with_live():
    require_paper_account_for_live("DU1234567", dry_run=False)


def test_guard_ignores_the_account_in_dry_run():
    require_paper_account_for_live("U7654321", dry_run=True)
    require_paper_account_for_live(None, dry_run=True)


@pytest.mark.parametrize("account", ["U7654321", "", None, 123])
def test_guard_refuses_other_accounts_with_live_and_masks_the_id(account):
    with pytest.raises(RuntimeError) as info:
        require_paper_account_for_live(account, dry_run=False)
    text = str(info.value)
    assert "--live" in text
    if isinstance(account, str) and account:
        assert account not in text
        assert "***21" in text


def test_guard_uses_the_configured_prefix():
    require_paper_account_for_live("XX1", dry_run=False, prefix="XX")
    with pytest.raises(RuntimeError):
        require_paper_account_for_live("DU1", dry_run=False, prefix="XX")


# ---- portfolio runner ----


def make_portfolio(dry_run: bool) -> pr.PortfolioRunner:
    r = pr.PortfolioRunner.__new__(pr.PortfolioRunner)
    r.dry_run = dry_run
    r.storage = FakeStorage()
    r.app_cfg = SimpleNamespace(ib=SimpleNamespace(port=5000))
    r.portfolio_cfg = PortfolioConfig()
    r.core_cfg = CoreSleeveConfig()
    r.sent_cfg = SentimentSatelliteConfig()
    r.tact_cfg = TacticalSleeveConfig()
    r.interval = 60
    r._warn_unmanaged_sentiment_holdings = lambda: None
    return r


def test_portfolio_live_with_paper_account_starts(monkeypatch):
    monkeypatch.setattr(
        pr, "read_session", lambda: {"cookie": "", "account_id": "DU1234567", "updated_at": 1}
    )
    monkeypatch.setattr(pr, "CPGWClient", FakeClient)
    r = make_portfolio(dry_run=False)
    r.start()
    assert r.client.account_id == "DU1234567"


def test_portfolio_live_with_other_account_raises_before_a_client_exists(monkeypatch):
    monkeypatch.setattr(
        pr, "read_session", lambda: {"cookie": "", "account_id": "U7654321", "updated_at": 1}
    )
    monkeypatch.setattr(pr, "CPGWClient", FakeClient)
    r = make_portfolio(dry_run=False)
    r.client = None
    with pytest.raises(RuntimeError, match="--live"):
        r.start()
    assert r.client is None


def test_portfolio_live_without_account_id_raises(monkeypatch):
    monkeypatch.setattr(pr, "read_session", lambda: {"cookie": "", "updated_at": 1})
    monkeypatch.setattr(pr, "CPGWClient", FakeClient)
    r = make_portfolio(dry_run=False)
    with pytest.raises(RuntimeError, match="--live"):
        r.start()


def test_portfolio_dry_run_with_other_account_still_starts(monkeypatch):
    monkeypatch.setattr(
        pr, "read_session", lambda: {"cookie": "", "account_id": "U7654321", "updated_at": 1}
    )
    monkeypatch.setattr(pr, "CPGWClient", FakeClient)
    r = make_portfolio(dry_run=True)
    r.start()
    assert r.client.account_id == "U7654321"


# ---- single symbol runner ----


def make_single(dry_run: bool) -> rn.Runner:
    r = rn.Runner.__new__(rn.Runner)
    r.dry_run = dry_run
    r.symbol = "SPY"
    r.model_name = "m"
    r.storage = FakeStorage()
    r.app_cfg = SimpleNamespace(ib=SimpleNamespace(port=5000))
    r.client = None
    return r


def test_runner_live_with_paper_account_starts(monkeypatch):
    monkeypatch.setattr(rn, "read_session", lambda: {"cookie": "", "account_id": "DU1234567"})
    monkeypatch.setattr(rn, "CPGWClient", FakeClient)
    r = make_single(dry_run=False)
    r.start()
    assert r.client.account_id == "DU1234567"


def test_runner_live_with_other_account_raises(monkeypatch):
    monkeypatch.setattr(rn, "read_session", lambda: {"cookie": "", "account_id": "U7654321"})
    monkeypatch.setattr(rn, "CPGWClient", FakeClient)
    r = make_single(dry_run=False)
    with pytest.raises(RuntimeError, match="--live"):
        r.start()
    assert r.client is None


def test_runner_dry_run_with_other_account_still_starts(monkeypatch):
    monkeypatch.setattr(rn, "read_session", lambda: {"cookie": "", "account_id": "U7654321"})
    monkeypatch.setattr(rn, "CPGWClient", FakeClient)
    r = make_single(dry_run=True)
    r.start()
    assert r.client.account_id == "U7654321"


@pytest.mark.parametrize("prefix", ["", "   ", None])
def test_guard_never_fails_open_on_an_empty_prefix(monkeypatch, prefix):
    # startswith("") is always true; an empty or blank prefix must refuse, not allow.
    import live.account_guard as guard

    if prefix is None:
        monkeypatch.setattr(
            guard, "load_keeper_config", lambda: SimpleNamespace(paper_account_prefix="")
        )
    with pytest.raises(RuntimeError, match="--live refused"):
        guard.require_paper_account_for_live("U1234567", dry_run=False, prefix=prefix)

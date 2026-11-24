"""Sentiment satellite research status: config validation and runner wiring. Offline."""

import pytest

from core.config import (
    CoreSleeveConfig,
    PortfolioConfig,
    SentimentSatelliteConfig,
    SymbolConfig,
    TacticalSleeveConfig,
    load_phase8_sleeves,
)
from live import portfolio_runner as pr
from live.portfolio_runner import PortfolioRunner
from live.storage import Storage


def test_research_with_allocation_raises():
    with pytest.raises(ValueError, match="allocation_pct"):
        SentimentSatelliteConfig(status="research", allocation_pct=0.2)


def test_unknown_status_raises():
    with pytest.raises(ValueError, match="status"):
        SentimentSatelliteConfig(status="paused")


@pytest.mark.parametrize("bad", ["0.2", True, None])
def test_non_numeric_allocation_raises(bad):
    with pytest.raises(ValueError, match="allocation_pct"):
        SentimentSatelliteConfig(allocation_pct=bad)


def test_allocation_out_of_range_raises():
    with pytest.raises(ValueError, match="allocation_pct"):
        SentimentSatelliteConfig(allocation_pct=1.5)


def test_tradeable_rules():
    assert SentimentSatelliteConfig(enabled=True, allocation_pct=0.1).tradeable
    assert not SentimentSatelliteConfig(enabled=False, allocation_pct=0.1).tradeable
    assert not SentimentSatelliteConfig(enabled=True, allocation_pct=0.0).tradeable
    assert not SentimentSatelliteConfig(enabled=True, status="research", allocation_pct=0).tradeable


def test_shipped_yaml_satellite_is_not_tradeable():
    _core, sent, _tact = load_phase8_sleeves()
    assert sent.status == "research"
    assert sent.allocation_pct == 0
    assert not sent.tradeable


def make_runner(tmp_path, sent_cfg) -> PortfolioRunner:
    r = PortfolioRunner.__new__(PortfolioRunner)
    r.storage = Storage(tmp_path / "bot.sqlite")
    r.core_cfg = CoreSleeveConfig()
    r.sent_cfg = sent_cfg
    r.tact_cfg = TacticalSleeveConfig()
    r.portfolio_cfg = PortfolioConfig()
    return r


def research_cfg() -> SentimentSatelliteConfig:
    return SentimentSatelliteConfig(
        enabled=False, allocation_pct=0.0, status="research", universe=["NVDA", "TSLA"]
    )


def test_runner_init_builds_no_satellite(tmp_path, monkeypatch):
    class FakeIb:
        port = 5000

    class FakeApp:
        ib = FakeIb()

    monkeypatch.setattr(pr, "load_app_config", lambda: FakeApp())
    monkeypatch.setattr(pr, "assert_paper_mode", lambda cfg: None)
    monkeypatch.setattr(pr, "load_portfolio", lambda: PortfolioConfig())
    monkeypatch.setattr(
        pr,
        "load_phase8_sleeves",
        lambda: (CoreSleeveConfig(), research_cfg(), TacticalSleeveConfig()),
    )
    monkeypatch.setattr(pr, "Storage", lambda: Storage(tmp_path / "bot.sqlite"))
    monkeypatch.setattr(pr, "load_symbol", lambda sym: SymbolConfig(symbol=sym))
    r = PortfolioRunner(dry_run=True, once=True, interval_override=None)
    assert r.satellite is None
    assert "NVDA" not in r.symbols
    assert "TSLA" not in r.symbols


def test_required_symbols_skip_research_satellite(tmp_path):
    r = make_runner(tmp_path, research_cfg())
    syms = r._all_required_symbols()
    assert "SPY" in syms
    assert not {"NVDA", "TSLA"} & syms


def test_required_symbols_include_active_satellite(tmp_path):
    cfg = SentimentSatelliteConfig(enabled=True, allocation_pct=0.1, universe=["NVDA"])
    r = make_runner(tmp_path, cfg)
    assert "NVDA" in r._all_required_symbols()


def test_leftover_holdings_one_warn_no_orders(tmp_path):
    r = make_runner(tmp_path, research_cfg())
    for sym in ("NVDA", "TSLA"):
        r.storage.upsert_holding(sleeve="sentiment", symbol=sym, shares=5, avg_cost=100.0)
    r._warn_unmanaged_sentiment_holdings()
    warns = [e for e in r.storage.recent_events() if e["level"] == "warn"]
    assert len(warns) == 1
    assert warns[0]["message"] == "2 simulated sentiment holdings are not managed"
    assert len(r.storage.get_holdings("sentiment")) == 2
    with r.storage._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_no_leftover_holdings_no_warn(tmp_path):
    r = make_runner(tmp_path, research_cfg())
    r._warn_unmanaged_sentiment_holdings()
    assert r.storage.recent_events() == []

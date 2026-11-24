"""Runner wiring: cooldown and one clock per cycle. Offline, dry run only."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.config import (
    CoreSleeveConfig,
    PortfolioConfig,
    SentimentConfig,
    SentimentSatelliteConfig,
    SymbolConfig,
)
from core.cpgw_client import NotAuthenticatedError
from core.market_hours import market_state, to_eastern
from core.sleeves.core import CoreSleeve
from core.sleeves.sentiment_satellite import SentimentSatelliteSleeve
from live.portfolio_runner import PortfolioRunner
from live.storage import Storage
from tests.conftest import linear_closes, make_macro
from tests.test_sleeve_core import ranked_closes, small_cfg

DAY = 86400
FRIDAY_OPEN = datetime(2026, 11, 13, 15, tzinfo=timezone.utc)  # Fri 10:00 EST


class FakeClient:
    account_id = "DU0000000"

    def __init__(self, closes: dict[str, pd.Series], summary=None, exc=None) -> None:
        self.closes = closes
        self.summary = summary
        self.exc = exc

    def account_summary(self):
        # Default raises, so tests that do not set a summary size from starting_equity.
        if self.exc is not None:
            raise self.exc
        if self.summary is None:
            raise RuntimeError("no summary configured")
        return self.summary

    def get_historical_data(self, symbol, bar=None, period=None):
        return pd.DataFrame({"close": list(self.closes[symbol])})


class FakeMacro:
    def snapshot(self):
        return make_macro()


def make_runner(tmp_path, closes, *, core_cfg=None, sent_cfg=None) -> PortfolioRunner:
    r = PortfolioRunner.__new__(PortfolioRunner)
    r.storage = Storage(tmp_path / "bot.sqlite")
    r.dry_run = True
    r.client = FakeClient(closes)
    r.symbols = {
        s: SymbolConfig(symbol=s, bar_size="1 day", history_duration="2 Y") for s in closes
    }
    r.portfolio_cfg = PortfolioConfig(reentry_cooldown_sec=DAY)
    r.sentiment_cfg = SentimentConfig()
    r.macro_fetcher = FakeMacro()
    r.core_cfg = core_cfg or small_cfg()
    r.core = CoreSleeve(r.core_cfg, reentry_cooldown_sec=DAY)
    r.sent_cfg = sent_cfg or SentimentSatelliteConfig(enabled=True, universe=["AAPL"])
    r.satellite = SentimentSatelliteSleeve(r.sent_cfg, reentry_cooldown_sec=DAY)
    return r


def log_sell(storage, sleeve, symbol):
    storage.log_order(
        sleeve=sleeve,
        symbol=symbol,
        action="SELL",
        quantity=10,
        entry_price=100.0,
        tp_price=None,
        stop_price=None,
        parent_id=None,
        tp_id=None,
        stop_id=None,
        dry_run=True,
        status="logged",
    )


def test_satellite_cycle_respects_cooldown(tmp_path):
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.storage.log_sentiment(symbol="AAPL", source="test", score=0.9, article_count=10)
    log_sell(r.storage, "sentiment", "AAPL")
    ms = market_state(FRIDAY_OPEN)

    r._cycle_satellite("c1", 100_000.0, ms, datetime.now(timezone.utc))

    buys = [o for o in r.storage.recent_orders() if o["action"] == "BUY"]
    assert buys == []
    assert r.storage.get_holdings("sentiment") == []


def test_core_cycle_respects_cooldown(tmp_path):
    r = make_runner(tmp_path, ranked_closes())
    r.storage.upsert_holding(sleeve="core", symbol="SPY", shares=0.0, avg_cost=100.0)
    log_sell(r.storage, "core", "SPY")
    sold_at = (FRIDAY_OPEN - timedelta(hours=1)).isoformat()
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        c.execute("UPDATE orders SET ts = ? WHERE sleeve = 'core'", (sold_at,))
    ms = market_state(FRIDAY_OPEN)
    total_equity = 10_000.0 / r.core_cfg.allocation_pct

    r._cycle_core("c1", total_equity, ms, FRIDAY_OPEN)

    buys = [o for o in r.storage.recent_orders() if o["action"] == "BUY" and o["symbol"] == "SPY"]
    assert buys == []
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        reasons = [row[0] for row in c.execute("SELECT reason FROM targets WHERE symbol = 'SPY'")]
    assert any("reentry cooldown" in x for x in reasons)


def freeze_runner_clock(monkeypatch, frozen: datetime) -> None:
    """Make `datetime.now()` inside live.portfolio_runner return `frozen`.

    Only the runner module is patched. `market_state()` called without an
    argument still reads the real clock, so a cycle that stops passing its
    own `now` to it is caught.
    """
    import live.portfolio_runner as pr

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(pr, "datetime", FrozenDatetime)
    monkeypatch.setattr(pr, "read_session", lambda: None)


def test_cycle_uses_one_clock_for_market_state_and_both_sleeves(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    seen = {}
    r._cycle_satellite = lambda cid, eq, ms, now: seen.update(sat=(ms, now))
    r._cycle_core = lambda cid, eq, ms, now: seen.update(core=(ms, now))

    r._cycle()

    sat_ms, sat_now = seen["sat"]
    core_ms, core_now = seen["core"]
    assert sat_now == core_now == FRIDAY_OPEN
    assert sat_ms is core_ms
    assert sat_ms.eastern_now == to_eastern(FRIDAY_OPEN)


def test_cycle_runs_the_core_calendar_trigger_and_cooldown_on_its_own_clock(tmp_path, monkeypatch):
    # The frozen clock is a Friday; the real clock usually is not. If _cycle
    # stopped passing its clock down, the calendar trigger would not fire.
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.satellite = None
    r.storage.upsert_holding(sleeve="core", symbol="SPY", shares=0.0, avg_cost=100.0)
    log_sell(r.storage, "core", "SPY")
    sold_at = (FRIDAY_OPEN - timedelta(hours=1)).isoformat()
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        c.execute("UPDATE orders SET ts = ? WHERE sleeve = 'core'", (sold_at,))
    r.portfolio_cfg = PortfolioConfig(
        starting_equity=10_000.0 / r.core_cfg.allocation_pct, reentry_cooldown_sec=DAY
    )

    r._cycle()

    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        reasons = [row[0] for row in c.execute("SELECT reason FROM rebalances")]
        spy = [row[0] for row in c.execute("SELECT reason FROM targets WHERE symbol = 'SPY'")]
    assert any("calendar: Fri" in x for x in reasons)
    assert any("reentry cooldown" in x for x in spy)
    buys = [o for o in r.storage.recent_orders() if o["action"] == "BUY" and o["symbol"] == "SPY"]
    assert buys == []


def test_cycle_runs_the_satellite_cooldown_on_its_own_clock(tmp_path, monkeypatch):
    # The SELL is 1 hour before the frozen clock, which is in the past. On the
    # cycle's clock the cooldown has 23.0h left. On the real clock it ended
    # long ago, so if the cycle's clock stopped reaching the satellite sleeve,
    # AAPL would be bought again.
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.core = None
    # Sentiment freshness is checked in Storage against the real clock, so
    # this row (written now) is fresh.
    r.storage.log_sentiment(symbol="AAPL", source="test", score=0.9, article_count=10)
    log_sell(r.storage, "sentiment", "AAPL")
    sold_at = (FRIDAY_OPEN - timedelta(hours=1)).isoformat()
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        c.execute("UPDATE orders SET ts = ? WHERE sleeve = 'sentiment'", (sold_at,))

    r._cycle()

    buys = [o for o in r.storage.recent_orders() if o["action"] == "BUY"]
    assert buys == []
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        events = [row[0] for row in c.execute("SELECT message FROM events")]
    assert any("AAPL skip: reentry cooldown: 23.0h left" in m for m in events)


def test_runner_passes_cooldown_to_sleeves(tmp_path, monkeypatch):
    import live.portfolio_runner as pr

    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "5000")
    pcfg = PortfolioConfig(reentry_cooldown_sec=12345)
    core_cfg = CoreSleeveConfig(enabled=True)
    sent_cfg = SentimentSatelliteConfig(enabled=True)
    tact = type("T", (), {"enabled": False, "universe": []})()
    monkeypatch.setattr(pr, "load_portfolio", lambda: pcfg)
    monkeypatch.setattr(pr, "load_phase8_sleeves", lambda: (core_cfg, sent_cfg, tact))
    monkeypatch.setattr(pr, "load_symbol", lambda s: SymbolConfig(symbol=s))
    monkeypatch.setattr(pr, "Storage", lambda: Storage(tmp_path / "bot.sqlite"))

    r = PortfolioRunner(dry_run=True, once=True, interval_override=None)

    assert r.core.reentry_cooldown_sec == 12345
    assert r.satellite.reentry_cooldown_sec == 12345


THANKSGIVING = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)  # Thu 10:00 EST, closed


def test_core_cycle_skips_on_a_holiday(tmp_path):
    r = make_runner(tmp_path, ranked_closes())
    ms = market_state(THANKSGIVING)

    r._cycle_core("c1", 100_000.0, ms, THANKSGIVING)

    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        events = [row[0] for row in c.execute("SELECT message FROM events")]
        targets = c.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    assert any("skip (holiday" in m for m in events)
    assert targets == 0
    assert r.storage.recent_orders() == []


def test_satellite_cycle_opens_nothing_on_a_holiday(tmp_path):
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.storage.log_sentiment(symbol="AAPL", source="test", score=0.9, article_count=10)
    ms = market_state(THANKSGIVING)

    r._cycle_satellite("c1", 100_000.0, ms, THANKSGIVING)

    buys = [o for o in r.storage.recent_orders() if o["action"] == "BUY"]
    assert buys == []
    assert r.storage.get_holdings("sentiment") == []


def test_cycle_labels_a_holiday_in_the_market_note(tmp_path, monkeypatch):
    # With no sleeves enabled, _cycle logs its market note. On Thanksgiving it
    # must say HOLIDAY, not fall through to AFTER-CLOSE.
    freeze_runner_clock(monkeypatch, THANKSGIVING)
    r = make_runner(tmp_path, ranked_closes())
    r.core = None
    r.satellite = None

    r._cycle()

    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        events = [row[0] for row in c.execute("SELECT message FROM events")]
    notes = [m for m in events if "no sleeves enabled" in m]
    assert len(notes) == 1
    assert "HOLIDAY (next open in" in notes[0]
    assert "AFTER-CLOSE" not in notes[0]


def nl_summary(amount, currency="USD"):
    return {"netliquidation": {"amount": amount, "currency": currency, "isNull": False}}


def equity_events(storage):
    return [e for e in storage.recent_events(500) if e["message"].find("equity:") >= 0]


def equity_rows(tmp_path):
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        return [row[0] for row in c.execute("SELECT equity FROM equity")]


def capture_sleeve_equity(r):
    seen = {}
    r._cycle_satellite = lambda cid, eq, ms, now: seen.update(sat=eq)
    r._cycle_core = lambda cid, eq, ms, now: seen.update(core=eq)
    return seen


def test_cycle_passes_gateway_equity_to_both_sleeves_and_logs_it(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.client.summary = nl_summary(123_456.78)
    seen = capture_sleeve_equity(r)

    r._cycle()

    assert seen == {"sat": 123_456.78, "core": 123_456.78}
    events = equity_events(r.storage)
    assert len(events) == 1
    assert events[0]["level"] == "info"
    assert "equity: net liquidation 123,456.78 USD (gateway)" in events[0]["message"]
    assert equity_rows(tmp_path) == [123_456.78]


def test_cycle_falls_back_when_gateway_session_expired(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.client.exc = NotAuthenticatedError("401 for /v1/api/portfolio/DU0000000/summary")
    seen = capture_sleeve_equity(r)

    r._cycle()

    start = r.portfolio_cfg.starting_equity
    assert seen == {"sat": start, "core": start}
    events = equity_events(r.storage)
    assert len(events) == 1
    assert events[0]["level"] == "warn"
    assert "fallback: gateway error" in events[0]["message"]
    assert "DU0000000" not in events[0]["message"]
    assert equity_rows(tmp_path) == []


def test_cycle_falls_back_on_out_of_range_value(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.client.summary = nl_summary(1_000_000.0)
    seen = capture_sleeve_equity(r)

    r._cycle()

    start = r.portfolio_cfg.starting_equity
    assert seen == {"sat": start, "core": start}
    events = equity_events(r.storage)
    assert events[0]["level"] == "warn"
    assert "outside" in events[0]["message"]
    assert equity_rows(tmp_path) == []


def test_cycle_sizes_real_budgets_from_gateway_equity(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    closes = ranked_closes()
    closes["AAPL"] = linear_closes(100.0, 0.0, 5)
    r = make_runner(tmp_path, closes)
    net_liq = 10_000.0 / r.core_cfg.allocation_pct
    r.client.summary = nl_summary(net_liq)
    r.portfolio_cfg = PortfolioConfig(starting_equity=20_000.0, reentry_cooldown_sec=DAY)
    r.storage.upsert_holding(sleeve="core", symbol="SPY", shares=0.0, avg_cost=100.0)
    log_sell(r.storage, "core", "SPY")
    sold_at = (FRIDAY_OPEN - timedelta(hours=1)).isoformat()
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        c.execute("UPDATE orders SET ts = ? WHERE sleeve = 'core'", (sold_at,))
    r.storage.log_sentiment(symbol="AAPL", source="test", score=0.9, article_count=10)
    sat_seen = {}
    real_evaluate = r.satellite.evaluate

    def wrapped(**kw):
        sat_seen["sleeve_equity"] = kw["sleeve_equity"]
        return real_evaluate(**kw)

    r.satellite.evaluate = wrapped

    r._cycle()

    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        before = [row[0] for row in c.execute("SELECT equity_before FROM rebalances")]
    assert before == [pytest.approx(10_000.0)]
    assert sat_seen["sleeve_equity"] == pytest.approx(r.sent_cfg.allocation_pct * net_liq)


def test_cycle_logs_exactly_one_equity_event(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.client.summary = nl_summary(100_000.0)

    r._cycle()

    assert len(equity_events(r.storage)) == 1
    assert len(equity_rows(tmp_path)) == 1


def test_cycle_with_gateway_equity_stays_dry_run(tmp_path, monkeypatch):
    freeze_runner_clock(monkeypatch, FRIDAY_OPEN)
    r = make_runner(tmp_path, ranked_closes())
    r.client.summary = nl_summary(100_000.0)

    r._cycle()

    assert r.dry_run is True
    with sqlite3.connect(tmp_path / "bot.sqlite") as c:
        live_orders = c.execute("SELECT COUNT(*) FROM orders WHERE dry_run = 0").fetchone()[0]
    assert live_orders == 0

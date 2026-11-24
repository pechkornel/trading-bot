"""Portfolio runner alerts: orders, errors and the daily summary. Fake notifier, no network."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from core.config import load_app_config
from core.sleeves.core import CoreDecision
from live import portfolio_runner as pr
from live.storage import Storage
from live.telegram_notifier import TelegramConfig, TelegramNotifier
from tests.conftest import linear_closes
from tests.test_portfolio_runner import make_runner

COOKIE = "x-sess-uuid=abcdef123456; JSESSIONID=zzz"


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, kind, text, *, dedupe_key=None, cooldown_sec=None) -> bool:
        self.sent.append((kind, text))
        return True


def runner_with_notifier(tmp_path, *, dry_run=True):
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.dry_run = dry_run
    r.notifier = FakeNotifier()
    return r


def decision(symbol="SPY", delta=10, price=100.0) -> CoreDecision:
    d = CoreDecision.__new__(CoreDecision)
    d.symbol = symbol
    d.delta_shares = delta
    d.target_shares = max(delta, 0)
    d.current_shares = 0
    d.close_price = price
    d.target_weight = 0.1
    d.target_value = price * abs(delta)
    d.reason = "test"
    d.score = None
    return d


def test_dry_run_core_orders_send_one_tagged_message_each(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._execute_core_leg(decision("SPY", 10), "c1")
    r._execute_core_leg(decision("QQQ", -5), "c1")
    kinds = [k for k, _ in r.notifier.sent]
    assert kinds == ["order", "order"]
    assert all(t.startswith("[DRY-RUN]") for _, t in r.notifier.sent)
    assert "BUY 10 SPY" in r.notifier.sent[0][1]
    assert "SELL 5 QQQ" in r.notifier.sent[1][1]


def test_dry_run_satellite_order_is_notified(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._exec_satellite_leg(
        symbol="AAPL", action="BUY", quantity=3, price=100.0, note="n", drop_holding=False
    )
    assert len(r.notifier.sent) == 1
    assert r.notifier.sent[0][1].startswith("[DRY-RUN]")


class BoomClient:
    account_id = "DU1234567"

    def conid_for_stock(self, symbol):
        return 1

    def build_limit_order(self, **kw):
        return kw

    def place_orders(self, orders):
        raise RuntimeError(f"gateway said no for {self.account_id} cookie={COOKIE} " + "x" * 2000)


def test_failed_placement_sends_redacted_truncated_failed_message(tmp_path):
    r = runner_with_notifier(tmp_path, dry_run=False)
    r.client = BoomClient()
    r._cookie = COOKIE
    r._execute_core_leg(decision("SPY", 10), "c1")
    assert len(r.notifier.sent) == 1
    kind, text = r.notifier.sent[0]
    assert kind == "order"
    assert text.startswith("[PAPER] FAILED")
    assert "DU1234567" not in text
    assert "abcdef123456" not in text
    assert len(text) < 600


def test_placed_order_is_tagged_paper(tmp_path):
    class Ticket:
        order_id = "77"

    class OkClient:
        account_id = "DU1234567"

        def conid_for_stock(self, s):
            return 1

        def build_limit_order(self, **kw):
            return kw

        def place_orders(self, orders):
            return [Ticket()]

    r = runner_with_notifier(tmp_path, dry_run=False)
    r.client = OkClient()
    r._execute_core_leg(decision("SPY", 10), "c1")
    assert r.notifier.sent[0][1].startswith("[PAPER] ")
    assert "FAILED" not in r.notifier.sent[0][1]


def test_notifier_exception_never_breaks_order_logging(tmp_path):
    class Raising:
        def send(self, *a, **k):
            raise RuntimeError("telegram down")

    r = runner_with_notifier(tmp_path)
    r.notifier = Raising()
    r._execute_core_leg(decision("SPY", 10), "c1")
    assert len(r.storage.recent_orders()) == 1


# 17:00 ET on Fri 2026-11-13 is 22:00 UTC (EST).
AT_1700 = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)
AT_1659 = datetime(2026, 11, 13, 21, 59, tzinfo=timezone.utc)
NEXT_DAY_1700 = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)


def test_summary_not_sent_before_1700_eastern(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._maybe_send_daily_summary(AT_1659)
    assert r.notifier.sent == []


def test_summary_sent_once_per_eastern_date(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._maybe_send_daily_summary(AT_1700)
    r._maybe_send_daily_summary(AT_1700.replace(minute=30))
    assert [k for k, _ in r.notifier.sent] == ["summary"]
    r._maybe_send_daily_summary(NEXT_DAY_1700)
    assert [k for k, _ in r.notifier.sent] == ["summary", "summary"]


def test_summary_not_resent_after_restart(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._maybe_send_daily_summary(AT_1700)
    r2 = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r2.notifier = FakeNotifier()
    r2._maybe_send_daily_summary(AT_1700.replace(minute=45))
    assert r2.notifier.sent == []


def test_failed_summary_send_is_retried_next_cycle(tmp_path):
    r = runner_with_notifier(tmp_path)

    class Down(FakeNotifier):
        def send(self, *a, **k):
            return False

    r.notifier = Down()
    r._maybe_send_daily_summary(AT_1700)
    r.notifier = FakeNotifier()
    r._maybe_send_daily_summary(AT_1700.replace(minute=10))
    assert len(r.notifier.sent) == 1


def test_summary_has_counts_and_no_secrets(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._cookie = COOKIE
    r._execute_core_leg(decision("SPY", 10), "c1")
    r.storage.log_event("error", "core", "something broke")
    r.storage.log_event("error", "sentiment_health", "premium endpoint")
    r.notifier.sent.clear()
    now = datetime.now(timezone.utc)
    # Any instant today in Eastern time at or after 17:00: use 23:59 ET of the real date.
    from core.market_hours import to_eastern

    eastern = to_eastern(now)
    late = eastern.replace(hour=23, minute=59)
    # Convert back to UTC for the call (offset is whatever the zone has today).
    offset = eastern - now.replace(tzinfo=None)
    late_utc = (late - offset).replace(tzinfo=timezone.utc)
    r._maybe_send_daily_summary(late_utc)
    text = r.notifier.sent[0][1]
    assert "logged: 1" in text
    assert "errors: 2" in text
    assert "dry_run: True" in text
    assert "premium endpoint" in text
    assert "abcdef123456" not in text
    assert "DU0000000" not in text


def test_summary_without_sentiment_events_says_so(tmp_path):
    r = runner_with_notifier(tmp_path)
    r._maybe_send_daily_summary(AT_1700)
    assert "no sentiment_health events" in r.notifier.sent[0][1]


def test_cycle_error_sends_one_deduped_alert(tmp_path):
    r = runner_with_notifier(tmp_path)
    r.once = True
    r.stopped = False
    requests: list[httpx.Request] = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={"ok": True})

    r.notifier = TelegramNotifier(
        TelegramConfig(token="1:tok", chat_id="9"),
        source="portfolio",
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_000_000.0,
        state_path=tmp_path / "tg.json",
    )

    def boom():
        raise RuntimeError(f"bad thing for DU1234567 {COOKIE}")

    r.client = BoomClient()
    r._cookie = COOKIE
    r._cycle = boom
    r.loop()
    r.loop()
    assert len(requests) == 1
    body = requests[0].content.decode()
    assert "DU1234567" not in body
    assert "abcdef123456" not in body


def test_cycle_error_alert_kind_is_error(tmp_path):
    r = runner_with_notifier(tmp_path)
    r.once = True
    r.stopped = False

    def boom():
        raise RuntimeError("kaput")

    r._cycle = boom
    r.loop()
    assert r.notifier.sent[0][0] == "error"
    assert "kaput" in r.notifier.sent[0][1]


def test_start_accepts_empty_cookie_when_session_file_exists(tmp_path, monkeypatch):
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.interval = 60
    r.once = True
    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "5000")
    r.app_cfg = load_app_config()
    r.tact_cfg = SimpleNamespace(enabled=False)
    monkeypatch.setattr(pr, "read_session", lambda: {"cookie": "", "account_id": "DU1234567"})
    monkeypatch.setattr(pr.signal, "signal", lambda *a: None)
    r.start()
    assert r.client is not None
    assert r.client.account_id == "DU1234567"


def test_start_without_session_file_still_raises(tmp_path, monkeypatch):
    r = make_runner(tmp_path, {"AAPL": linear_closes(100.0, 0.0, 5)})
    r.interval = 60
    r.once = True
    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "5000")
    r.app_cfg = load_app_config()
    r.tact_cfg = SimpleNamespace(enabled=False)
    monkeypatch.setattr(pr, "read_session", lambda: None)
    with pytest.raises(RuntimeError, match="No CPGW session"):
        r.start()


def test_storage_event_helpers(tmp_path):
    s = Storage(tmp_path / "e.sqlite")
    assert s.has_event("telegram", "daily_summary 2026-11-13") is False
    assert s.last_event("sentiment_health") is None
    s.log_event("error", "sentiment_health", "first")
    s.log_event("error", "sentiment_health", "second")
    s.log_event("info", "telegram", "daily_summary 2026-11-13")
    assert s.has_event("telegram", "daily_summary 2026-11-13") is True
    assert s.last_event("sentiment_health")["message"] == "second"


def test_flag_defaults_unchanged():
    assert pr.parse_args([]).dry_run is True
    assert pr.parse_args(["--live"]).dry_run is False

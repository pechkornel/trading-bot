import httpx
import pytest

from core.config import SentimentConfig
from core.sentiment import SentimentDataUnavailable, SentimentReading, StubProvider
from live import sentiment_worker
from live.sentiment_worker import SentimentWorker
from live.storage import Storage
from live.telegram_notifier import TelegramConfig, TelegramNotifier


class FakeProvider:
    name = "alpha_vantage"

    def __init__(self, readings=None, error=None):
        self.readings = readings
        self.error = error
        self.calls = 0

    def fetch_batch(self, symbols):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.readings


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, kind, text, *, dedupe_key=None, cooldown_sec=None):
        self.sent.append((kind, text, dedupe_key, cooldown_sec))
        return True


def good():
    return [SentimentReading("SPY", 0.2, 0.5, 5, "alpha_vantage")]


def zeros():
    return [SentimentReading("SPY", 0.0, 0.0, 0, "alpha_vantage")]


def _cfg():
    return SentimentConfig(provider="alpha_vantage", symbols=["SPY"])


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "bot.sqlite")


def make_worker(storage, provider, notifier=None):
    return SentimentWorker(
        interval_override=None,
        once=True,
        cfg=_cfg(),
        provider=provider,
        storage=storage,
        notifier=notifier or FakeNotifier(),
    )


def rows(storage):
    with storage._conn() as c:
        return c.execute("SELECT * FROM sentiment").fetchall()


def health_events(storage):
    return [e for e in storage.recent_events(50) if e["source"] == "sentiment_health"]


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider(error=SentimentDataUnavailable("premium endpoint")),
        FakeProvider(readings=[]),
        FakeProvider(readings=zeros()),
    ],
)
def test_failure_logs_error_event_alerts_and_writes_no_rows(storage, provider):
    notifier = FakeNotifier()
    worker = make_worker(storage, provider, notifier)
    worker.loop()
    assert rows(storage) == []
    events = health_events(storage)
    assert len(events) == 1
    assert events[0]["level"] == "error"
    assert len(notifier.sent) == 1
    kind, _text, key, cooldown = notifier.sent[0]
    assert kind == "health"
    assert key.startswith("sentiment_health:")
    assert cooldown == 86400


def test_success_writes_rows_and_no_alert(storage):
    notifier = FakeNotifier()
    make_worker(storage, FakeProvider(readings=good()), notifier).loop()
    assert len(rows(storage)) == 1
    assert health_events(storage) == []
    assert notifier.sent == []


def test_stub_provider_still_writes_rows(storage):
    worker = SentimentWorker(
        interval_override=None,
        once=True,
        cfg=SentimentConfig(provider="stub", symbols=["SPY"]),
        provider=StubProvider(),
        storage=storage,
        notifier=FakeNotifier(),
    )
    worker.loop()
    assert len(rows(storage)) == 1
    assert health_events(storage) == []


def test_real_notifier_alerts_once_per_day_per_reason(tmp_path, storage):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(200, json={"ok": True})

    now = [1_000_000.0]
    notifier = TelegramNotifier(
        TelegramConfig(token="123:abc", chat_id="42"),
        source="sentiment",
        storage=storage,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
        state_path=tmp_path / "tg.json",
    )
    worker = make_worker(
        storage, FakeProvider(error=SentimentDataUnavailable("premium endpoint")), notifier
    )
    worker._tick()
    worker._tick()
    assert len(posts) == 1
    now[0] += 86400 + 1
    worker._tick()
    assert len(posts) == 2
    worker.provider = FakeProvider(readings=[])
    worker._tick()
    assert len(posts) == 3


def test_check_returns_2_on_failure_and_0_on_success(storage):
    bad = FakeProvider(error=SentimentDataUnavailable("premium endpoint"))
    notifier = FakeNotifier()
    code = sentiment_worker.main(
        ["--check"], provider=bad, storage=storage, notifier=notifier, cfg=_cfg()
    )
    assert code == 2
    assert len(health_events(storage)) == 1
    assert len(notifier.sent) == 1
    assert rows(storage) == []

    ok = FakeProvider(readings=good())
    code = sentiment_worker.main(
        ["--check"], provider=ok, storage=storage, notifier=FakeNotifier(), cfg=_cfg()
    )
    assert code == 0
    assert rows(storage) == []


def test_check_stub_exits_0_with_message(storage, capsys):
    code = sentiment_worker.main(
        ["--check"],
        provider=StubProvider(),
        storage=storage,
        notifier=FakeNotifier(),
        cfg=SentimentConfig(provider="stub", symbols=["SPY"]),
    )
    assert code == 0
    assert "stub provider, nothing to check" in capsys.readouterr().out


def test_check_with_missing_key_exits_2(storage, monkeypatch, capsys):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    code = sentiment_worker.main(["--check"], storage=storage, notifier=FakeNotifier(), cfg=_cfg())
    assert code == 2
    assert "ALPHA_VANTAGE_API_KEY" in capsys.readouterr().out

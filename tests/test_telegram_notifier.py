"""Telegram notifier: disabled mode, request shape, redaction, dedupe, cap, state."""

from __future__ import annotations

import json

import httpx

from live import telegram_notifier as tn
from live.telegram_notifier import TelegramConfig, TelegramNotifier

TOKEN = "123456:ABC-secretTokenValue_xyz"
CHAT = "987654321"


class FakeStorage:
    def __init__(self):
        self.events: list[tuple[str, str, str]] = []

    def log_event(self, level, source, message):
        self.events.append((level, source, message))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(tmp_path, *, handler=None, clock=None, storage=None, config=None, name="t"):
    requests: list[httpx.Request] = []

    def wrapped(request):
        requests.append(request)
        if handler is None:
            return httpx.Response(200, json={"ok": True})
        return handler(request)

    notifier = TelegramNotifier(
        config or TelegramConfig(token=TOKEN, chat_id=CHAT),
        source=name,
        storage=storage,
        transport=httpx.MockTransport(wrapped),
        clock=clock or Clock(),
        state_path=tmp_path / f"state_{name}.json",
    )
    return notifier, requests


def fields_of(request: httpx.Request) -> dict:
    body = request.content.decode()
    if body.startswith("{"):
        return json.loads(body)
    return dict(httpx.QueryParams(body))


def test_from_env_reads_variables(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    cfg = TelegramConfig.from_env()
    assert cfg.enabled
    assert TOKEN not in repr(cfg)


def test_disabled_without_token_or_chat(tmp_path):
    storage = FakeStorage()
    for cfg in (
        TelegramConfig(token="", chat_id=CHAT),
        TelegramConfig(token=TOKEN, chat_id=""),
        TelegramConfig.from_env(),
    ):
        notifier, requests = make(tmp_path, config=cfg, storage=storage)
        assert notifier.send("error", "boom") is False
        assert notifier.send("error", "boom again") is False
        assert requests == []
    infos = [e for e in storage.events if e[0] == "info"]
    assert len(infos) == 3  # one per notifier, not one per send


def test_request_shape(tmp_path):
    notifier, requests = make(tmp_path)
    assert notifier.send("order", "BUY SPY x 1") is True
    (req,) = requests
    assert req.method == "POST"
    assert req.url.host == "api.telegram.org"
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    fields = fields_of(req)
    assert fields["chat_id"] == CHAT
    assert "BUY SPY x 1" in fields["text"]
    assert fields["text"].startswith("[tradingbot paper]")
    assert "parse_mode" not in fields


def test_token_never_leaks_on_transport_error(tmp_path, capsys):
    storage = FakeStorage()

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    notifier, _ = make(tmp_path, handler=boom, storage=storage)
    result = notifier.send("error", "something broke")
    assert result is False
    out = capsys.readouterr()
    blob = " ".join(m for _, _, m in storage.events) + out.out + out.err + repr(result)
    assert TOKEN not in blob
    assert "secretTokenValue" not in blob
    assert any(level == "warn" for level, _, _ in storage.events)


def test_token_never_leaks_on_http_error_status(tmp_path, capsys):
    storage = FakeStorage()

    def bad(request):
        return httpx.Response(401, json={"ok": False, "description": f"bad {request.url}"})

    notifier, _ = make(tmp_path, handler=bad, storage=storage)
    assert notifier.send("error", "x") is False
    out = capsys.readouterr()
    blob = " ".join(m for _, _, m in storage.events) + out.out + out.err
    assert TOKEN not in blob
    assert any(level == "warn" for level, _, _ in storage.events)


def test_token_in_text_is_redacted(tmp_path):
    notifier, requests = make(tmp_path)
    notifier.send("error", f"failed calling https://api.telegram.org/bot{TOKEN}/x with {TOKEN}")
    assert TOKEN not in requests[0].content.decode()


def test_dedupe_inside_cooldown_then_after(tmp_path):
    clock = Clock()
    notifier, requests = make(tmp_path, clock=clock)
    assert notifier.send("session", "lost", dedupe_key="session_lost", cooldown_sec=3600)
    clock.now += 100
    assert notifier.send("session", "lost", dedupe_key="session_lost", cooldown_sec=3600) is False
    assert len(requests) == 1
    clock.now += 3600
    assert notifier.send("session", "lost", dedupe_key="session_lost", cooldown_sec=3600)
    assert len(requests) == 2


def test_error_default_dedupe_uses_first_80_chars(tmp_path):
    clock = Clock()
    notifier, requests = make(tmp_path, clock=clock)
    head = "x" * 80
    assert notifier.send("error", head + "one")
    assert notifier.send("error", head + "two") is False
    assert len(requests) == 1
    clock.now += 901
    assert notifier.send("error", head + "three")


def test_global_cap_and_suppressed_count(tmp_path):
    clock = Clock()
    notifier, requests = make(tmp_path, clock=clock)
    for i in range(20):
        assert notifier.send("order", f"order {i}", dedupe_key=f"o{i}")
    assert notifier.send("order", "order 20", dedupe_key="o20") is False
    assert notifier.send("order", "order 21", dedupe_key="o21") is False
    assert len(requests) == 20
    clock.now += 3601
    assert notifier.send("order", "order 22", dedupe_key="o22")
    assert "(2 alerts suppressed)" in fields_of(requests[-1])["text"]


def test_state_survives_new_instance(tmp_path):
    clock = Clock()
    n1, _ = make(tmp_path, clock=clock)
    assert n1.send("session", "lost", dedupe_key="k", cooldown_sec=3600)
    n2, r2 = make(tmp_path, clock=clock)
    assert n2.send("session", "lost", dedupe_key="k", cooldown_sec=3600) is False
    assert r2 == []


def test_corrupt_state_file_does_not_raise(tmp_path):
    (tmp_path / "state_t.json").write_text("{not json")
    notifier, requests = make(tmp_path)
    assert notifier.send("summary", "hello") is True
    assert len(requests) == 1


def test_long_text_is_truncated(tmp_path):
    notifier, requests = make(tmp_path)
    notifier.send("summary", "a" * 10_000)
    assert len(fields_of(requests[0])["text"]) <= tn.MAX_TEXT_CHARS == 3500


def test_cli_test_calls_sender_once(capsys):
    calls = []

    class Fake:
        def send(self, kind, text, **kw):
            calls.append((kind, text))
            return True

    rc = tn.main(["--test"], notifier_factory=lambda: Fake())
    assert rc == 0
    assert len(calls) == 1
    assert TOKEN not in capsys.readouterr().out


def test_cli_test_exit_code_when_not_sent():
    class Fake:
        def send(self, kind, text, **kw):
            return False

    assert tn.main(["--test"], notifier_factory=lambda: Fake()) == 1

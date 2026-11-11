import json
from pathlib import Path
from urllib.error import URLError

import pytest

from core import sentiment
from core.sentiment import AlphaVantageProvider, StubProvider, get_provider

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_news_sentiment.json"
SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL"]


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeUrlopen:
    """Stands in for urlopen and records every Request it receives."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def patch_urlopen(monkeypatch, payload=None, error=None) -> FakeUrlopen:
    body = None if payload is None else json.dumps(payload).encode()
    fake = FakeUrlopen(body=body, error=error)
    monkeypatch.setattr(sentiment, "urlopen", fake)
    return fake


def provider() -> AlphaVantageProvider:
    return AlphaVantageProvider(api_key="test-key")


# ---- stub and factory ----


def test_stub_returns_neutral_readings():
    readings = StubProvider().fetch_batch(["SPY", "AAPL"])
    assert [r.symbol for r in readings] == ["SPY", "AAPL"]
    for r in readings:
        assert r.score == 0.0
        assert r.article_count == 0
        assert r.source == "stub"


def test_get_provider_stub():
    assert isinstance(get_provider("stub"), StubProvider)


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown sentiment provider"):
        get_provider("nope")


def test_get_provider_alpha_vantage_without_key_raises():
    with pytest.raises(ValueError, match="ALPHA_VANTAGE_API_KEY"):
        get_provider("alpha_vantage")


def test_alpha_vantage_reads_key_from_env(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key")
    assert AlphaVantageProvider().api_key == "test-key"


# ---- Alpha Vantage ----


def test_empty_symbol_list_makes_no_request(monkeypatch):
    fake = patch_urlopen(monkeypatch, payload={})
    assert provider().fetch_batch([]) == []
    assert fake.requests == []


def test_fixture_parsing(monkeypatch):
    fake = patch_urlopen(monkeypatch, payload=json.loads(FIXTURE.read_text()))
    readings = provider().fetch_batch(SYMBOLS)

    assert [r.symbol for r in readings] == SYMBOLS
    by_sym = {r.symbol: r for r in readings}

    assert by_sym["AAPL"].score == pytest.approx(0.52)
    assert by_sym["AAPL"].article_count == 2
    assert by_sym["AAPL"].confidence == pytest.approx(0.1)

    assert by_sym["MSFT"].score == pytest.approx(-0.2)
    assert by_sym["MSFT"].article_count == 2
    assert by_sym["MSFT"].confidence == pytest.approx(0.1)

    assert by_sym["NVDA"].score == 0.0
    assert by_sym["NVDA"].article_count == 0
    assert by_sym["NVDA"].confidence == 0.0

    # Raw 0.8 times 2 is clamped to 1.0.
    assert by_sym["GOOGL"].score == 1.0
    assert by_sym["GOOGL"].article_count == 1
    assert by_sym["GOOGL"].confidence == pytest.approx(0.05)

    for r in readings:
        assert r.source == "alpha_vantage"
        assert r.raw_json is None

    assert len(fake.requests) == 1
    url = fake.requests[0].full_url
    assert "function=NEWS_SENTIMENT" in url
    assert "tickers=AAPL%2CMSFT%2CNVDA%2CGOOGL" in url
    assert "apikey=test-key" in url


@pytest.mark.parametrize("payload", [{"Information": "rate limit"}, {"Note": "slow down"}])
def test_throttle_payload_raises(monkeypatch, payload):
    patch_urlopen(monkeypatch, payload=payload)
    with pytest.raises(RuntimeError, match="throttled"):
        provider().fetch_batch(SYMBOLS)


def test_transport_error_raises_runtime_error(monkeypatch):
    patch_urlopen(monkeypatch, error=URLError("down"))
    with pytest.raises(RuntimeError, match="request failed"):
        provider().fetch_batch(SYMBOLS)


def test_null_feed_gives_zero_readings(monkeypatch):
    patch_urlopen(monkeypatch, payload={"feed": None})
    readings = provider().fetch_batch(SYMBOLS)
    assert len(readings) == len(SYMBOLS)
    assert all(r.score == 0.0 and r.article_count == 0 for r in readings)


@pytest.mark.parametrize("raw, expected", [(0.25, 0.5), (0.8, 1.0), (-0.9, -1.0)])
def test_normalize(raw, expected):
    assert AlphaVantageProvider._normalize(raw) == pytest.approx(expected)

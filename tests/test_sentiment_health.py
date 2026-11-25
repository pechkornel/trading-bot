from urllib.error import URLError

import pytest

from core.sentiment import AlphaVantageProvider, SentimentDataUnavailable, SentimentReading
from core.sentiment_health import HealthResult, check_readings, classify_error
from tests.test_sentiment import SYMBOLS, patch_urlopen

KEY = "SECRETKEY123"


def av_provider() -> AlphaVantageProvider:
    return AlphaVantageProvider(api_key=KEY)


def reading(symbol="SPY", count=3, source="alpha_vantage") -> SentimentReading:
    return SentimentReading(symbol, 0.1, 0.5, count, source)


PREMIUM = (
    "Thank you for using Alpha Vantage! This is a premium endpoint. "
    "You may subscribe to any of the premium plans to instantly unlock all premium endpoints"
)


@pytest.mark.parametrize(
    "payload",
    [
        {"Information": PREMIUM},
        {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 25/day"},
    ],
)
def test_premium_and_note_payloads_raise_unavailable_without_key(monkeypatch, payload):
    patch_urlopen(monkeypatch, payload=payload)
    with pytest.raises(SentimentDataUnavailable) as exc:
        av_provider().fetch_batch(SYMBOLS)
    text = str(exc.value)
    assert "apikey" not in text.lower()
    assert KEY not in text
    assert "http" not in text.lower()
    assert isinstance(exc.value, RuntimeError)


def test_payload_text_containing_the_key_is_scrubbed(monkeypatch):
    patch_urlopen(monkeypatch, payload={"Information": f"bad call apikey={KEY} here"})
    with pytest.raises(SentimentDataUnavailable) as exc:
        av_provider().fetch_batch(SYMBOLS)
    assert KEY not in str(exc.value)
    assert "apikey" not in str(exc.value).lower()


def test_transport_error_never_carries_the_key(monkeypatch):
    url = f"https://www.alphavantage.co/query?apikey={KEY}"
    patch_urlopen(monkeypatch, error=URLError(f"cannot reach {url}"))
    with pytest.raises(RuntimeError) as exc:
        av_provider().fetch_batch(SYMBOLS)
    assert KEY not in str(exc.value)
    assert "apikey" not in str(exc.value).lower()


def test_classify_error_is_not_ok_and_scrubs_the_key():
    res = classify_error(SentimentDataUnavailable(f"premium apikey={KEY}"))
    assert isinstance(res, HealthResult)
    assert not res.ok
    assert KEY not in res.reason
    assert res.reason


def test_empty_feed_fails():
    res = check_readings("alpha_vantage", [])
    assert not res.ok
    assert "no readings" in res.reason


def test_all_zero_article_counts_fail():
    res = check_readings("alpha_vantage", [reading("SPY", 0), reading("QQQ", 0)])
    assert not res.ok
    assert "no articles" in res.reason


def test_some_articles_pass():
    assert check_readings("alpha_vantage", [reading("SPY", 0), reading("QQQ", 4)]).ok


def test_stub_passes_with_its_message():
    res = check_readings("stub", [reading("SPY", 0, "stub")])
    assert res == HealthResult(ok=True, reason="stub provider, nothing to check")
    assert check_readings("stub", []).ok

"""Health check for the sentiment data source.

The check is about the provider, not about the sentiment sleeve: it runs even
when the sleeve is off, so a dead or premium-only feed is noticed either way.

A feed is unhealthy when the provider raised (premium or throttle notice,
transport error), returned no readings, or every reading has zero articles.
Messages never contain the API key or the request URL.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.sentiment import SentimentReading, scrub_secrets

STUB_REASON = "stub provider, nothing to check"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    reason: str


def check_readings(provider_name: str, readings: list[SentimentReading]) -> HealthResult:
    if provider_name == "stub":
        return HealthResult(ok=True, reason=STUB_REASON)
    if not readings:
        return HealthResult(ok=False, reason=f"{provider_name} returned no readings")
    if all(r.article_count == 0 for r in readings):
        return HealthResult(ok=False, reason=f"{provider_name} returned no articles for any symbol")
    total = sum(r.article_count for r in readings)
    return HealthResult(ok=True, reason=f"{provider_name} returned {total} articles")


def classify_error(exc: BaseException) -> HealthResult:
    """Turn a provider exception into a failed result with a key-free reason."""
    text = scrub_secrets(str(exc)) or type(exc).__name__
    return HealthResult(ok=False, reason=text[:300])

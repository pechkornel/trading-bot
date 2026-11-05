"""
Sentiment provider interface + backends.

Backends:
  - StubProvider          — returns 0.0 for every symbol (no network, for testing)
  - AlphaVantageProvider  — Alpha Vantage News & Sentiment API, free tier
                            (25 calls/day, 5/min). One batch call covers all symbols.

The sentiment worker (live/sentiment_worker.py) calls `fetch_batch()` periodically
and writes results to SQLite. The portfolio runner reads from SQLite — never calls
the API itself.

Output range: score ∈ [-1.0, 1.0]   (-1 very bearish, +1 very bullish, 0 neutral)
              confidence ∈ [0.0, 1.0]   (provider's own confidence, or 1/N where N=articles)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


@dataclass
class SentimentReading:
    symbol: str
    score: float            # [-1, 1]
    confidence: float       # [0, 1]
    article_count: int
    source: str             # "stub", "alpha_vantage", etc.
    raw_json: str | None = None


class SentimentProvider(Protocol):
    """One method: fetch a batch of readings for a list of symbols."""

    name: str

    def fetch_batch(self, symbols: list[str]) -> list[SentimentReading]:
        ...


# ---------- Stub ----------

class StubProvider:
    name = "stub"

    def fetch_batch(self, symbols: list[str]) -> list[SentimentReading]:
        return [
            SentimentReading(symbol=s, score=0.0, confidence=0.0,
                             article_count=0, source=self.name)
            for s in symbols
        ]


# ---------- Alpha Vantage ----------

class AlphaVantageProvider:
    """
    Alpha Vantage News & Sentiment API.

    Endpoint:
        https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=SPY,AAPL&apikey=...

    Response shape (relevant fields):
        {
          "items": "50",
          "feed": [
            {
              "title": "...",
              "overall_sentiment_score": 0.142,
              "overall_sentiment_label": "Somewhat-Bullish",
              "ticker_sentiment": [
                {"ticker": "SPY", "ticker_sentiment_score": "0.213",
                 "ticker_sentiment_label": "Somewhat-Bullish",
                 "relevance_score": "0.876"},
                ...
              ]
            },
            ...
          ]
        }

    We aggregate per ticker: weighted mean of `ticker_sentiment_score` by
    `relevance_score`, capped to [-1, 1] (API already returns ~[-0.5, 0.5] but
    we treat that as raw and normalize by dividing by 0.5 — see _normalize).
    """

    name = "alpha_vantage"
    BASE_URL = "https://www.alphavantage.co/query"
    TIMEOUT_SEC = 30.0

    def __init__(self, api_key: str | None = None, lookback_hours: int = 6) -> None:
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Alpha Vantage requires ALPHA_VANTAGE_API_KEY in env or constructor. "
                "Get a free key at https://www.alphavantage.co/support/#api-key"
            )
        self.lookback_hours = lookback_hours

    def fetch_batch(self, symbols: list[str]) -> list[SentimentReading]:
        if not symbols:
            return []
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ",".join(symbols),
            "limit": "100",  # max items per call
            "apikey": self.api_key,
        }
        url = f"{self.BASE_URL}?{urlencode(params)}"
        req = Request(url, headers={"User-Agent": "tradingbot/1.0"})
        try:
            with urlopen(req, timeout=self.TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read())
        except (URLError, HTTPError, json.JSONDecodeError, TimeoutError) as e:
            # Worker will log + retry next cycle
            raise RuntimeError(f"Alpha Vantage request failed: {e}") from e

        # Free tier rate limit gives a `Note` field instead of `feed`
        if "Note" in payload or "Information" in payload:
            raise RuntimeError(f"Alpha Vantage throttled: {payload}")

        feed = payload.get("feed", []) or []
        return self._aggregate(symbols, feed)

    def _aggregate(self, symbols: list[str], feed: list[dict]) -> list[SentimentReading]:
        """Per symbol: weighted mean of ticker_sentiment_score by relevance_score."""
        sums: dict[str, float] = {s: 0.0 for s in symbols}
        weights: dict[str, float] = {s: 0.0 for s in symbols}
        counts: dict[str, int] = {s: 0 for s in symbols}

        for article in feed:
            for ts in article.get("ticker_sentiment", []) or []:
                t = ts.get("ticker")
                if t not in sums:
                    continue
                try:
                    score = float(ts.get("ticker_sentiment_score", 0.0))
                    rel = float(ts.get("relevance_score", 0.0))
                except (TypeError, ValueError):
                    continue
                sums[t] += score * rel
                weights[t] += rel
                counts[t] += 1

        out: list[SentimentReading] = []
        for s in symbols:
            if weights[s] > 0:
                raw = sums[s] / weights[s]
                normalized = self._normalize(raw)
                conf = min(1.0, counts[s] / 20.0)   # 20 relevant articles → full confidence
            else:
                normalized = 0.0
                conf = 0.0
            out.append(SentimentReading(
                symbol=s, score=normalized, confidence=conf,
                article_count=counts[s], source=self.name,
                raw_json=None,    # don't store full feed per symbol — too verbose
            ))
        return out

    @staticmethod
    def _normalize(raw: float) -> float:
        """Alpha Vantage scores roughly in [-0.5, 0.5]. Scale to [-1, 1] and clamp."""
        return max(-1.0, min(1.0, raw * 2.0))


# ---------- Factory ----------

def get_provider(name: str) -> SentimentProvider:
    if name == "stub":
        return StubProvider()
    if name in ("alpha_vantage", "alphavantage"):
        return AlphaVantageProvider()
    raise ValueError(f"Unknown sentiment provider {name!r}. Available: stub, alpha_vantage")

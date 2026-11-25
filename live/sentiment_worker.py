"""
Sentiment polling worker.

Runs on its own cadence (default: hourly), calls the configured provider
in a single batch for all tactical-universe symbols, and writes results to
the `sentiment` table. The portfolio runner just reads the latest fresh row
per symbol — never calls the API itself, so trading loops stay snappy and we
stay under free-tier rate limits.

Usage:
    python -m live.sentiment_worker             # loop forever on the configured interval
    python -m live.sentiment_worker --once      # one fetch, then exit
    python -m live.sentiment_worker --interval 600   # override poll interval (sec)
    python -m live.sentiment_worker --check     # one fetch, health verdict, exit 0 ok / 2 failed

The health check is about the data source, so the worker and --check run it
whether or not the sentiment sleeve is enabled. A failed fetch logs an `error`
event with source `sentiment_health`, sends a Telegram `health` alert (once per
day per reason) and writes no sentiment rows.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any

from core.config import SentimentConfig, load_phase8_sleeves, load_sentiment_config
from core.sentiment import SentimentProvider, get_provider
from core.sentiment_health import HealthResult, check_readings, classify_error
from live.storage import Storage
from live.telegram_notifier import TelegramConfig, TelegramNotifier

ALERT_COOLDOWN_SEC = 86400  # one alert per day per reason


class SentimentWorker:
    def __init__(
        self,
        interval_override: int | None,
        once: bool,
        *,
        cfg: SentimentConfig | None = None,
        provider: SentimentProvider | None = None,
        storage: Storage | None = None,
        notifier: Any = None,
    ) -> None:
        self.cfg = cfg or load_sentiment_config()
        self.interval = interval_override or self.cfg.poll_interval_sec
        self.once = once
        self.stopped = False
        self.storage = storage or Storage()
        self.notifier = notifier or TelegramNotifier(
            TelegramConfig.from_env(), source="sentiment", storage=self.storage
        )
        # A provider that cannot be built (for example a missing API key) is a data
        # source failure: keep the error and report it on every fetch.
        self._provider_error: Exception | None = None
        self.provider: SentimentProvider | None = provider
        if provider is None:
            try:
                self.provider = get_provider(self.cfg.provider)
            except ValueError as e:
                self._provider_error = e
        if cfg is not None:  # an injected config (tests) keeps its own symbols
            return
        # Phase 8B: use the satellite sleeve's universe as the source of truth
        # for which symbols to poll. Falls back to the legacy config list if the
        # satellite isn't enabled (so polling still works for the dashboard).
        try:
            _, sat_cfg, _ = load_phase8_sleeves()
            if sat_cfg.universe:
                self.cfg.symbols = list(sat_cfg.universe)
        except Exception:
            pass

    def start(self) -> None:
        banner = (
            f"SENTIMENT WORKER · provider={self.cfg.provider} · "
            f"symbols={self.cfg.symbols} · interval={self.interval}s"
        )
        self.storage.log_event("info", "sentiment", f"starting | {banner}")
        print(banner)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_: Any) -> None:
        self.stopped = True

    def loop(self) -> None:
        while not self.stopped:
            t = time.time()
            try:
                self._tick()
            except Exception as e:
                self.storage.log_event("error", "sentiment", f"tick failed: {e}")
                print(f"[sentiment] tick failed: {e}")
            if self.once:
                return
            elapsed = time.time() - t
            sleep = max(0.0, self.interval - elapsed)
            slept = 0.0
            while slept < sleep and not self.stopped:
                time.sleep(min(1.0, sleep - slept))
                slept += 1.0

    def _fetch(self) -> tuple[list[Any], HealthResult]:
        """One provider fetch and its health verdict. Never raises."""
        if self.provider is None:
            return [], classify_error(self._provider_error or RuntimeError("no provider"))
        try:
            readings = self.provider.fetch_batch(self.cfg.symbols)
        except Exception as e:
            return [], classify_error(e)
        return readings, check_readings(self.provider.name, readings)

    def _report_failure(self, result: HealthResult) -> None:
        self.storage.log_event("error", "sentiment_health", result.reason)
        print(f"[sentiment] UNHEALTHY: {result.reason}")
        self.notifier.send(
            "health",
            f"sentiment data unavailable: {result.reason}",
            dedupe_key=f"sentiment_health:{result.reason}",
            cooldown_sec=ALERT_COOLDOWN_SEC,
        )

    def check(self) -> HealthResult:
        """One fetch, no rows written. Reports a failure loudly."""
        _, result = self._fetch()
        if result.ok:
            print(f"[sentiment] healthy: {result.reason}")
        else:
            self._report_failure(result)
        return result

    def _tick(self) -> None:
        readings, result = self._fetch()
        if not result.ok:
            # No zero rows on failure: stale data must read as missing, not neutral.
            self._report_failure(result)
            return
        for r in readings:
            self.storage.log_sentiment(
                symbol=r.symbol,
                source=r.source,
                score=r.score,
                confidence=r.confidence,
                article_count=r.article_count,
                raw_json=r.raw_json,
            )
        line = "  ".join(f"{r.symbol}={r.score:+.2f}({r.article_count})" for r in readings)
        print(f"[sentiment] {line}")
        self.storage.log_event(
            "info", "sentiment", f"fetched {len(readings)} readings from {self.cfg.provider}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sentiment polling worker.")
    p.add_argument("--interval", type=int, default=None, help="Override poll interval (seconds)")
    p.add_argument("--once", action="store_true", help="One fetch then exit (testing)")
    p.add_argument(
        "--check",
        action="store_true",
        help="One fetch, print the health verdict, exit 0 if healthy and 2 if not",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None, **deps: Any) -> int:
    """`deps` (cfg, provider, storage, notifier) are injected by tests."""
    args = parse_args(argv)
    worker = SentimentWorker(interval_override=args.interval, once=args.once, **deps)
    if args.check:
        return 0 if worker.check().ok else 2
    worker.start()
    worker.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

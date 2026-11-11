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
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any

from core.config import load_phase8_sleeves, load_sentiment_config
from core.sentiment import get_provider
from live.storage import Storage


class SentimentWorker:
    def __init__(self, interval_override: int | None, once: bool) -> None:
        self.cfg = load_sentiment_config()
        self.interval = interval_override or self.cfg.poll_interval_sec
        self.once = once
        self.stopped = False
        self.storage = Storage()
        self.provider = get_provider(self.cfg.provider)
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
            f"SENTIMENT WORKER · provider={self.provider.name} · "
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

    def _tick(self) -> None:
        readings = self.provider.fetch_batch(self.cfg.symbols)
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
            "info", "sentiment", f"fetched {len(readings)} readings from {self.provider.name}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sentiment polling worker.")
    p.add_argument("--interval", type=int, default=None, help="Override poll interval (seconds)")
    p.add_argument("--once", action="store_true", help="One fetch then exit (testing)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    worker = SentimentWorker(interval_override=args.interval, once=args.once)
    worker.start()
    worker.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

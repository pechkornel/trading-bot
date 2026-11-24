"""MacroFetcher.snapshot: the timestamp must be timezone-aware UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.config import MacroConfig
from core.macro import MacroFetcher


def test_snapshot_as_of_is_aware_utc():
    cfg = MacroConfig(fred_series={}, yf_tickers={})
    before = datetime.now(timezone.utc)
    snap = MacroFetcher(cfg).snapshot()
    after = datetime.now(timezone.utc)

    assert snap.as_of.tzinfo is not None
    assert snap.as_of.utcoffset() == timedelta(0)
    assert before <= snap.as_of <= after
    assert snap.complete is False

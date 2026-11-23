"""Tests for the SQLite storage layer (local temp files, no network)."""

from __future__ import annotations

import sqlite3
from datetime import timezone

import pytest

from live.storage import Storage


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(tmp_path / "bot.sqlite")


def _order(
    store: Storage,
    symbol: str,
    *,
    action: str = "SELL",
    sleeve: str = "sentiment",
    dry_run: bool = True,
    status: str = "logged",
) -> int:
    return store.log_order(
        symbol=symbol,
        action=action,
        quantity=10,
        entry_price=100.0,
        tp_price=None,
        stop_price=None,
        parent_id=None,
        tp_id=None,
        stop_id=None,
        dry_run=dry_run,
        status=status,
        sleeve=sleeve,
    )


def _set_ts(store: Storage, order_id: int, ts: str) -> None:
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("UPDATE orders SET ts = ? WHERE id = ?", (ts, order_id))
        conn.commit()
    finally:
        conn.close()


def test_last_exit_times_returns_latest_successful_sell_per_symbol(store):
    first = _order(store, "AAPL")
    second = _order(store, "AAPL")
    other = _order(store, "MSFT")
    _set_ts(store, first, "2026-11-10T10:00:00+00:00")
    _set_ts(store, second, "2026-11-12T10:00:00+00:00")
    _set_ts(store, other, "2026-11-11T10:00:00+00:00")

    result = store.last_exit_times("sentiment")

    assert set(result) == {"AAPL", "MSFT"}
    assert result["AAPL"].isoformat() == "2026-11-12T10:00:00+00:00"
    assert result["MSFT"].isoformat() == "2026-11-11T10:00:00+00:00"


def test_last_exit_times_ignores_buys_failed_orders_and_other_sleeves(store):
    _order(store, "BUYONLY", action="BUY")
    _order(store, "FAILED", status="failed")
    _order(store, "COREONLY", sleeve="core")

    assert store.last_exit_times("sentiment") == {}
    assert set(store.last_exit_times("core")) == {"COREONLY"}


def test_last_exit_times_counts_dry_run_and_placed(store):
    _order(store, "DRY", dry_run=True, status="logged")
    _order(store, "LIVE", dry_run=False, status="placed")

    assert set(store.last_exit_times("sentiment")) == {"DRY", "LIVE"}


def test_last_exit_times_values_are_tz_aware_utc(store):
    aware = _order(store, "AAPL")
    naive = _order(store, "MSFT")
    _set_ts(store, aware, "2026-11-12T10:00:00+00:00")
    _set_ts(store, naive, "2026-11-12T10:00:00")

    result = store.last_exit_times("sentiment")

    for value in result.values():
        assert value.tzinfo is not None
        assert value.utcoffset() == timezone.utc.utcoffset(None)
    assert result["AAPL"] == result["MSFT"]


def test_last_exit_times_empty_db(store):
    assert store.last_exit_times("sentiment") == {}

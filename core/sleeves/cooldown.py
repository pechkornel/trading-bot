"""Re-entry cooldown helpers shared by the core and sentiment sleeves.

Internal module: not exported from core.sleeves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _as_utc(ts: datetime) -> datetime:
    """Treat a naive datetime as UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def cooldown_remaining(
    exit_ts: datetime | None, now: datetime, cooldown_sec: int
) -> timedelta | None:
    """Time left before a symbol exited at exit_ts may be bought again, or None if free."""
    if exit_ts is None or cooldown_sec <= 0:
        return None
    left = timedelta(seconds=cooldown_sec) - (_as_utc(now) - _as_utc(exit_ts))
    if left <= timedelta(0):
        return None
    return left


def format_cooldown(left: timedelta) -> str:
    """Skip reason text, for example "reentry cooldown: 23.0h left"."""
    hours = left.total_seconds() / 3600
    return f"reentry cooldown: {hours:.1f}h left"

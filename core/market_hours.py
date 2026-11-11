"""
US equity market hours awareness.

Used by the portfolio runner to decide what to do per cycle:
  - DURING MARKET HOURS (Mon–Fri 09:30–16:00 ET):
      full evaluation — core scoring, sentiment entries, stops, exits
  - AFTER CLOSE on the rebalance day (Fri 16:00–23:59 ET):
      core sleeve evaluation only (works on the just-closed Friday prices)
  - WEEKENDS / OTHER OFF-HOURS:
      time-based satellite exits only (the 10-day timer doesn't care if
      the market is open — but we won't try to PLACE the exit order until
      Monday open). Everything else is a no-op.

Holidays are NOT handled yet. NYSE has ~9 full closures + ~3 early closes per
year. If we trade for a year and never hit a Thanksgiving false-evaluation, we
got lucky. TODO Phase 9: pull NYSE calendar from pandas_market_calendars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Optional

# US Eastern is UTC-5 (EST) or UTC-4 (EDT). Python's stdlib doesn't ship full
# tz data on all platforms; we approximate by detecting DST manually. For our
# use case (gating evaluations), being off by one hour around DST flips is fine.


# DST in the US: second Sunday of March through first Sunday of November.
def _us_eastern_offset(utc_now: datetime) -> timedelta:
    """Return offset to apply to UTC to get US Eastern time."""
    year = utc_now.year
    # Second Sunday of March
    dst_start = _nth_weekday(year, 3, 6, 2)  # March, Sunday, 2nd occurrence
    # First Sunday of November
    dst_end = _nth_weekday(year, 11, 6, 1)
    # DST window is [dst_start 02:00 ET, dst_end 02:00 ET]
    # Approximate: just compare dates (off-by-one-hour around the flip is fine for us)
    in_dst = dst_start.date() <= utc_now.date() < dst_end.date()
    return timedelta(hours=-4 if in_dst else -5)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """Nth occurrence of weekday in given year/month. weekday: 0=Mon..6=Sun."""
    d = datetime(year, month, 1)
    # Days to advance to first matching weekday
    days_ahead = (weekday - d.weekday()) % 7
    first = d + timedelta(days=days_ahead)
    return first + timedelta(days=7 * (n - 1))


def to_eastern(utc: datetime) -> datetime:
    """Convert a UTC datetime to US Eastern (naive). Use only for display + gating."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    et = utc.astimezone(timezone.utc) + _us_eastern_offset(utc)
    return et.replace(tzinfo=None)


# ---- regime detection ----


class MarketRegime(Enum):
    OPEN = "open"  # Mon–Fri 09:30–16:00 ET
    PRE_OPEN = "pre_open"  # Mon–Fri 00:00–09:30 ET
    AFTER_CLOSE = "after_close"  # Mon–Fri 16:00–23:59 ET
    WEEKEND = "weekend"  # Sat / Sun


@dataclass
class MarketState:
    regime: MarketRegime
    eastern_now: datetime
    weekday: int  # 0=Mon..6=Sun
    is_open: bool  # convenience
    is_weekend: bool
    minutes_to_open: Optional[int]  # None if currently open
    minutes_to_close: Optional[int]  # None if not currently open


_OPEN_TIME = time(9, 30)
_CLOSE_TIME = time(16, 0)


def market_state(now_utc: Optional[datetime] = None) -> MarketState:
    """Where in the market schedule are we right now? (or at a given UTC time.)"""
    now_utc = now_utc or datetime.now(timezone.utc)
    et = to_eastern(now_utc)
    wd = et.weekday()
    is_weekend = wd >= 5

    if is_weekend:
        regime = MarketRegime.WEEKEND
        is_open = False
        # Minutes until Monday 09:30 ET
        days_to_monday = 7 - wd
        next_open = et.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(
            days=days_to_monday
        )
        minutes_to_open = int((next_open - et).total_seconds() // 60)
        minutes_to_close = None
    else:
        t = et.time()
        if t < _OPEN_TIME:
            regime = MarketRegime.PRE_OPEN
            is_open = False
            opens = et.replace(hour=9, minute=30, second=0, microsecond=0)
            minutes_to_open = int((opens - et).total_seconds() // 60)
            minutes_to_close = None
        elif t < _CLOSE_TIME:
            regime = MarketRegime.OPEN
            is_open = True
            closes = et.replace(hour=16, minute=0, second=0, microsecond=0)
            minutes_to_open = None
            minutes_to_close = int((closes - et).total_seconds() // 60)
        else:
            regime = MarketRegime.AFTER_CLOSE
            is_open = False
            # Minutes until tomorrow 09:30 ET (or Monday if Friday)
            if wd == 4:  # Friday after close
                next_open_day = et + timedelta(days=3)
            else:
                next_open_day = et + timedelta(days=1)
            opens = next_open_day.replace(hour=9, minute=30, second=0, microsecond=0)
            minutes_to_open = int((opens - et).total_seconds() // 60)
            minutes_to_close = None

    return MarketState(
        regime=regime,
        eastern_now=et,
        weekday=wd,
        is_open=is_open,
        is_weekend=is_weekend,
        minutes_to_open=minutes_to_open,
        minutes_to_close=minutes_to_close,
    )


# ---- gating helpers ----


def should_run_entries(state: MarketState) -> bool:
    """Should we look for NEW positions to open (core eval + satellite scans)?

    Yes if market is open OR we're in the post-close window (entries placed
    after close fill at next open's price)."""
    return state.regime in (MarketRegime.OPEN, MarketRegime.AFTER_CLOSE)


def should_run_exits(state: MarketState) -> bool:
    """Should we check exit conditions (10-day timer, stops)?

    Yes always — the 10-day timer doesn't care about hours, and time-based
    exit ORDERS can be queued for the next open. Stops only meaningfully fire
    during market hours, but checking the flag at any time is cheap."""
    return True


def is_core_rebalance_window(
    state: MarketState,
    rebalance_day_of_week: int = 4,
) -> bool:
    """
    Core sleeve evaluates only on the configured rebalance day, AFTER close.

    Default = Friday after 16:00 ET. That way the eval works on the
    just-closed Friday bars, and orders queue for Monday open.
    """
    return state.weekday == rebalance_day_of_week and state.regime == MarketRegime.AFTER_CLOSE

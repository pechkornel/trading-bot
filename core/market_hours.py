"""
US equity market hours awareness.

Used by the portfolio runner to decide what to do per cycle. The schedule comes
from the NYSE (XNYS) calendar of exchange_calendars: weekends, full-day
holidays and early closes (13:00 ET) are all handled.
  - DURING MARKET HOURS (session open to session close, normally 09:30-16:00 ET):
      full evaluation: core scoring, sentiment entries, stops, exits
  - AFTER CLOSE (session close to midnight ET):
      the runner still evaluates core (it works on the just-closed bars).
      The core sleeve itself decides whether to act: at most once per day,
      on its rebalance weekday or on drift. PRE_OPEN, WEEKEND and HOLIDAY
      are skipped by the core sleeve.
  - WEEKENDS / HOLIDAYS:
      time-based satellite exits only (the hold timer doesn't care if
      the market is open, but we won't try to PLACE the exit order until
      the next open). Everything else is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

_EASTERN = ZoneInfo("America/New_York")


def to_eastern(utc: datetime) -> datetime:
    """Convert a datetime to US Eastern wall-clock time (naive).

    Naive input is treated as UTC. Aware input is converted. Uses the IANA zone,
    so the result is correct at DST changes. Use only for display + gating."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    return utc.astimezone(_EASTERN).replace(tzinfo=None)


# ---- NYSE calendar ----


@lru_cache(maxsize=2)
def _nyse(end_year: int) -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS", start="2020-01-01", end=f"{end_year}-12-31")


def nyse_calendar() -> xcals.ExchangeCalendar:
    """XNYS calendar from 2020 to 31 December of the current year + 2.

    Built once per process and calendar year, so a long-running process
    rebuilds it after New Year."""
    return _nyse(datetime.now(timezone.utc).year + 2)


def trading_days_between(start: datetime, end: datetime) -> int:
    """Number of NYSE session closes after `start` and at or before `end`.

    The close is inclusive, and an early close counts as a full day. Naive input
    is treated as UTC. Returns 0 when `end <= start`."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= start:
        return 0
    closes = nyse_calendar().closes
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    return int(((closes > lo) & (closes <= hi)).sum())


# ---- regime detection ----


class MarketRegime(Enum):
    OPEN = "open"  # session open to session close
    PRE_OPEN = "pre_open"  # session day, midnight ET to the open
    AFTER_CLOSE = "after_close"  # session day, close to midnight ET
    WEEKEND = "weekend"  # Sat / Sun
    HOLIDAY = "holiday"  # weekday with no NYSE session


@dataclass
class MarketState:
    regime: MarketRegime
    eastern_now: datetime
    weekday: int  # 0=Mon..6=Sun
    is_open: bool  # convenience
    is_weekend: bool  # Saturday or Sunday only
    minutes_to_open: Optional[int]  # None if currently open
    minutes_to_close: Optional[int]  # None if not currently open


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def market_state(now_utc: Optional[datetime] = None) -> MarketState:
    """Where in the market schedule are we right now? (or at a given UTC time.)

    Minutes are real elapsed minutes (computed in UTC), so they are correct
    across DST changes, holidays and early closes."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    et = to_eastern(now_utc)
    wd = et.weekday()
    cal = nyse_calendar()
    day = pd.Timestamp(et.date())

    minutes_to_open: Optional[int] = None
    minutes_to_close: Optional[int] = None
    if cal.is_session(day):
        open_utc = cal.session_open(day).to_pydatetime()
        close_utc = cal.session_close(day).to_pydatetime()
        if now_utc < open_utc:
            regime = MarketRegime.PRE_OPEN
            minutes_to_open = _minutes(open_utc - now_utc)
        elif now_utc < close_utc:
            regime = MarketRegime.OPEN
            minutes_to_close = _minutes(close_utc - now_utc)
        else:
            regime = MarketRegime.AFTER_CLOSE
            next_open = cal.session_open(cal.next_session(day)).to_pydatetime()
            minutes_to_open = _minutes(next_open - now_utc)
    else:
        regime = MarketRegime.WEEKEND if wd >= 5 else MarketRegime.HOLIDAY
        next_open = cal.session_open(cal.date_to_session(day, direction="next")).to_pydatetime()
        minutes_to_open = _minutes(next_open - now_utc)

    return MarketState(
        regime=regime,
        eastern_now=et,
        weekday=wd,
        is_open=regime == MarketRegime.OPEN,
        is_weekend=wd >= 5,
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

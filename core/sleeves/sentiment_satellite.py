"""
Sentiment satellite sleeve.

Watches sentiment scores written to SQLite by `live.sentiment_worker`. When a
symbol crosses a bullish threshold with enough articles, opens a long.
Holds for up to N trading days or until a stop-loss fires.

This is the smaller, more reactive sleeve (20% of equity by default).

Decision flow per cycle (only runs when market state permits):

  EXITS first — any open position whose time-since-entry > hold_max_days
                OR whose current price is < entry × (1 - stop_loss_pct)
                gets closed (or queued for next open if market is closed).

  ENTRIES next — for each universe symbol:
                if no open position, sentiment.score > threshold,
                article_count >= min, and we're under max_concurrent,
                open a position sized at position_pct_of_sleeve.

Returns plans the runner executes (so this module stays I/O-free).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from core.config import SentimentSatelliteConfig


@dataclass
class SentimentInput:
    """One symbol's snapshot the runner passes to the sleeve."""
    symbol: str
    price: float
    sentiment_score: float | None       # None if no fresh reading
    article_count: int
    sentiment_ts: datetime | None       # when the sentiment was captured


@dataclass
class Position:
    """An open satellite position the runner reads from `holdings`."""
    symbol: str
    shares: int
    entry_price: float
    opened_at: datetime


@dataclass
class SatelliteEntry:
    symbol: str
    shares: int
    price: float
    target_value: float
    reason: str


@dataclass
class SatelliteExit:
    symbol: str
    shares: int
    price: float
    reason: str                         # "10-day hard exit" / "5% stop" / etc.


@dataclass
class SatelliteCycleResult:
    entries: list[SatelliteEntry]
    exits: list[SatelliteExit]
    skipped: list[tuple[str, str]]      # (symbol, why-skipped) — useful in /targets


class SentimentSatelliteSleeve:
    name = "sentiment"

    def __init__(self, cfg: SentimentSatelliteConfig) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        *,
        inputs: Mapping[str, SentimentInput],
        positions: Mapping[str, Position],
        sleeve_equity: float,
        now: datetime | None = None,
        allow_entries: bool = True,
        allow_exits: bool = True,
    ) -> SatelliteCycleResult:
        """
        Decide what to enter / exit this cycle.

        `inputs` is symbol → SentimentInput for the satellite universe.
        `positions` is symbol → currently-held Position (only positions for THIS sleeve).
        """
        now = now or datetime.now(timezone.utc)
        entries: list[SatelliteEntry] = []
        exits: list[SatelliteExit] = []
        skipped: list[tuple[str, str]] = []

        # ---- EXITS first (free up slots) ----
        if allow_exits:
            for sym, pos in list(positions.items()):
                exit_reason = self._maybe_exit_reason(pos, inputs.get(sym), now)
                if exit_reason:
                    inp = inputs.get(sym)
                    exit_price = inp.price if inp else pos.entry_price
                    exits.append(SatelliteExit(
                        symbol=sym, shares=pos.shares,
                        price=exit_price, reason=exit_reason,
                    ))

        # Recompute available slots after exits
        remaining_positions = {
            s: p for s, p in positions.items()
            if not any(e.symbol == s for e in exits)
        }
        open_slots = self.cfg.max_concurrent_positions - len(remaining_positions)

        # ---- ENTRIES ----
        if allow_entries and open_slots > 0:
            # Rank candidates by sentiment_score descending (best entry first)
            candidates = sorted(
                (
                    (sym, inp) for sym, inp in inputs.items()
                    if sym in self.cfg.universe
                ),
                key=lambda kv: (kv[1].sentiment_score or 0.0),
                reverse=True,
            )
            for sym, inp in candidates:
                if open_slots <= 0:
                    break
                if sym in remaining_positions:
                    skipped.append((sym, "already holding"))
                    continue
                reason_or_skip = self._entry_check(sym, inp)
                if reason_or_skip.startswith("skip:"):
                    skipped.append((sym, reason_or_skip[5:]))
                    continue

                # Size: position_pct_of_sleeve of sleeve_equity, floor to int shares
                target_value = sleeve_equity * self.cfg.position_pct_of_sleeve
                if inp.price <= 0:
                    skipped.append((sym, "no price"))
                    continue
                shares = int(target_value / inp.price)
                if shares <= 0:
                    skipped.append((sym, "rounds to 0 shares"))
                    continue

                entries.append(SatelliteEntry(
                    symbol=sym, shares=shares, price=inp.price,
                    target_value=shares * inp.price, reason=reason_or_skip,
                ))
                open_slots -= 1

        return SatelliteCycleResult(entries=entries, exits=exits, skipped=skipped)

    # ---- helpers ----

    def _entry_check(self, sym: str, inp: SentimentInput | None) -> str:
        """Returns reason if entering, or 'skip: <why>' if not."""
        if inp is None or inp.sentiment_score is None:
            return "skip: no fresh sentiment"
        if abs(inp.sentiment_score) < self.cfg.sentiment_threshold:
            return f"skip: |sent|={abs(inp.sentiment_score):.2f} < {self.cfg.sentiment_threshold}"
        if inp.article_count < self.cfg.min_article_count:
            return f"skip: articles={inp.article_count} < {self.cfg.min_article_count}"
        if self.cfg.longs_only and inp.sentiment_score < 0:
            return f"skip: bearish ({inp.sentiment_score:+.2f}) and longs-only"
        if inp.price <= 0:
            return "skip: no price"
        return (
            f"enter: sent={inp.sentiment_score:+.2f} articles={inp.article_count} "
            f"@ {inp.price:.2f}"
        )

    def _maybe_exit_reason(
        self, pos: Position, inp: SentimentInput | None, now: datetime,
    ) -> str | None:
        """Returns a reason if this position should exit now, else None."""
        # Hard time-based exit
        if pos.opened_at.tzinfo is None:
            opened = pos.opened_at.replace(tzinfo=timezone.utc)
        else:
            opened = pos.opened_at
        age_days = (now - opened).days
        if age_days >= self.cfg.hold_max_days:
            return f"hold timer: {age_days}d >= {self.cfg.hold_max_days}d max"

        # Stop loss
        if inp is not None and pos.entry_price > 0:
            loss_pct = (pos.entry_price - inp.price) / pos.entry_price
            if loss_pct >= self.cfg.stop_loss_pct:
                return f"stop: -{loss_pct:.1%} <= -{self.cfg.stop_loss_pct:.0%}"

        return None

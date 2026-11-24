"""Total equity for the sleeve budgets: the paper account's net liquidation, with a safe fallback.

`resolve_total_equity` never raises and does not log. The caller logs the event.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from core.config import PortfolioConfig
from core.cpgw_client import mask_account

MAX_ERROR_CHARS = 160


@dataclass(frozen=True)
class EquityReading:
    equity: float
    source: str  # "gateway", "last_gateway" or "starting_equity"
    reason: str  # "" for gateway, otherwise why the fallback was used
    currency: str | None = None
    as_of: str | None = None  # timestamp of the reused reading (source "last_gateway")
    disabled: bool = False  # True only when use_account_equity is false (a deliberate choice)


@dataclass(frozen=True)
class LastReading:
    """The last accepted gateway reading from the equity table."""

    equity: float
    ts: datetime


def parse_net_liquidation(summary: object) -> tuple[float, str | None]:
    """Return (amount, currency) from a /portfolio/{id}/summary response. Raises ValueError."""
    if not isinstance(summary, dict):
        raise ValueError("summary is not a dict")
    entry = summary.get("netliquidation")
    if not isinstance(entry, dict):
        raise ValueError("netliquidation missing")
    if entry.get("isNull"):
        raise ValueError("netliquidation isNull")
    if "amount" not in entry or entry["amount"] is None:
        raise ValueError("amount missing")
    amount = entry["amount"]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("amount is not a number")
    currency = entry.get("currency")
    try:
        value = float(amount)
    except OverflowError:
        raise ValueError("amount is not a number") from None
    return value, currency if isinstance(currency, str) else None


# The old private name keeps working. The shared implementation lives in core.cpgw_client.
_mask_account = mask_account


def _usable_last(
    cfg: PortfolioConfig, last: LastReading | None, now: datetime
) -> LastReading | None:
    if last is None:
        return None
    ts = last.ts if last.ts.tzinfo is not None else last.ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    if 0 <= age <= cfg.equity_last_max_age_sec:
        return LastReading(last.equity, ts)
    return None


def _fallback(cfg: PortfolioConfig, reason: str, last: LastReading | None = None) -> EquityReading:
    if last is not None:
        return EquityReading(last.equity, "last_gateway", reason, None, last.ts.isoformat())
    return EquityReading(cfg.starting_equity, "starting_equity", reason)


def _resolve(
    client: object, cfg: PortfolioConfig, last: LastReading | None, now: datetime
) -> EquityReading:
    if not cfg.use_account_equity:
        return EquityReading(
            cfg.starting_equity,
            "starting_equity",
            "disabled (portfolio.use_account_equity: false)",
            disabled=True,
        )
    last = _usable_last(cfg, last, now)
    if client is None:
        return _fallback(cfg, "no gateway client", last)
    try:
        summary = client.account_summary()  # type: ignore[attr-defined]
    except Exception as exc:
        message = _mask_account(str(exc), getattr(client, "account_id", None))
        return _fallback(
            cfg, f"gateway error: {type(exc).__name__}: {message[:MAX_ERROR_CHARS]}", last
        )
    try:
        amount, currency = parse_net_liquidation(summary)
    except ValueError as exc:
        return _fallback(cfg, f"bad summary: {exc}", last)
    if not math.isfinite(amount):
        return _fallback(cfg, "net liquidation not finite", last)
    if amount <= 0:
        return _fallback(cfg, f"net liquidation not positive ({amount:,.2f})", last)
    if currency is not None and currency.upper() != cfg.equity_currency.upper():
        return _fallback(
            cfg, f"currency {currency[:MAX_ERROR_CHARS]}, expected {cfg.equity_currency}", last
        )
    low = cfg.equity_min_ratio * cfg.starting_equity
    high = cfg.equity_max_ratio * cfg.starting_equity
    if not low <= amount <= high:
        return _fallback(
            cfg,
            f"net liquidation {amount:,.2f} outside {low:,.2f} to {high:,.2f} "
            f"({cfg.equity_min_ratio:g}x to {cfg.equity_max_ratio:g}x starting_equity)",
            last,
        )
    if last is not None and abs(amount - last.equity) / last.equity > cfg.equity_max_step:
        return _fallback(
            cfg,
            f"net liquidation moved more than {cfg.equity_max_step:.0%} since {last.ts.isoformat()}",
            last,
        )
    return EquityReading(amount, "gateway", "", currency)


def resolve_total_equity(
    client: object,
    cfg: PortfolioConfig,
    last: LastReading | None = None,
    now: datetime | None = None,
) -> EquityReading:
    """Pure: `last` is the last accepted gateway reading, passed in by the caller."""
    now = now or datetime.now(timezone.utc)
    try:
        return _resolve(client, cfg, last, now)
    except Exception as exc:
        reason = f"unexpected error: {type(exc).__name__}"
        return EquityReading(cfg.starting_equity, "starting_equity", reason)


def format_equity_event(reading: EquityReading) -> str:
    if reading.source == "gateway":
        currency = f" {reading.currency}" if reading.currency else ""
        return f"equity: net liquidation {reading.equity:,.2f}{currency} (gateway)"
    if reading.source == "last_gateway":
        return (
            f"equity: last net liquidation {reading.equity:,.2f} "
            f"(fallback: {reading.reason}, from {reading.as_of})"
        )
    return f"equity: starting_equity {reading.equity:,.2f} (fallback: {reading.reason})"

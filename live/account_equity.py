"""Total equity for the sleeve budgets: the paper account's net liquidation, with a safe fallback.

`resolve_total_equity` never raises and does not log. The caller logs the event.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import PortfolioConfig

MAX_ERROR_CHARS = 160


@dataclass(frozen=True)
class EquityReading:
    equity: float
    source: str  # "gateway" or "starting_equity"
    reason: str  # "" for gateway, otherwise why the fallback was used
    currency: str | None = None


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
    return float(amount), currency if isinstance(currency, str) else None


def _mask_account(text: str, account_id: object) -> str:
    """Replace the account id in `text` with `***` plus its last 2 characters."""
    if not isinstance(account_id, str) or not account_id:
        return text
    return text.replace(account_id, "***" + account_id[-2:])


def _fallback(cfg: PortfolioConfig, reason: str) -> EquityReading:
    return EquityReading(cfg.starting_equity, "starting_equity", reason)


def _resolve(client: object, cfg: PortfolioConfig) -> EquityReading:
    if not cfg.use_account_equity:
        return _fallback(cfg, "disabled (portfolio.use_account_equity: false)")
    if client is None:
        return _fallback(cfg, "no gateway client")
    try:
        summary = client.account_summary()  # type: ignore[attr-defined]
    except Exception as exc:
        message = _mask_account(str(exc), getattr(client, "account_id", None))
        return _fallback(cfg, f"gateway error: {type(exc).__name__}: {message[:MAX_ERROR_CHARS]}")
    try:
        amount, currency = parse_net_liquidation(summary)
    except ValueError as exc:
        return _fallback(cfg, f"bad summary: {exc}")
    if not math.isfinite(amount):
        return _fallback(cfg, "net liquidation not finite")
    if amount <= 0:
        return _fallback(cfg, f"net liquidation not positive ({amount:,.2f})")
    if currency is not None and currency.upper() != cfg.equity_currency.upper():
        return _fallback(cfg, f"currency {currency}, expected {cfg.equity_currency}")
    low = cfg.equity_min_ratio * cfg.starting_equity
    high = cfg.equity_max_ratio * cfg.starting_equity
    if not low <= amount <= high:
        return _fallback(
            cfg,
            f"net liquidation {amount:,.2f} outside {low:,.2f} to {high:,.2f} "
            f"({cfg.equity_min_ratio:g}x to {cfg.equity_max_ratio:g}x starting_equity)",
        )
    return EquityReading(amount, "gateway", "", currency)


def resolve_total_equity(client: object, cfg: PortfolioConfig) -> EquityReading:
    try:
        return _resolve(client, cfg)
    except Exception as exc:
        reason = f"unexpected error: {type(exc).__name__}"
        return EquityReading(cfg.starting_equity, "starting_equity", reason)


def format_equity_event(reading: EquityReading) -> str:
    if reading.source == "gateway":
        currency = f" {reading.currency}" if reading.currency else ""
        return f"equity: net liquidation {reading.equity:,.2f}{currency} (gateway)"
    return f"equity: starting_equity {reading.equity:,.2f} (fallback: {reading.reason})"

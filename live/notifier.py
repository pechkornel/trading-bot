"""
Notifications.

Replaces trading_bot_1_for_tests/email_sender.py with:
  - correct signature: send_email(subject, text)  (old code called it with just text)
  - .env path resolved from bot/ project root (old code had hardcoded /Users/.../trading_bot_1/.env)
  - recipients list read from EMAIL_RECIPIENTS env (comma-separated), falls back to EMAIL_USER
  - silently no-ops if EMAIL_USER/EMAIL_PASS are missing — useful for dev / dashboard-only mode

Telegram support is sketched in the docstring of `send_telegram` but not wired up;
will be added in Phase 4 alongside the dashboard so the same notifier handles UI events.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

from core.config import ROOT


_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(ROOT / ".env")
        _ENV_LOADED = True


def send_email(subject: str, text: str) -> bool:
    """
    Send an email via Gmail SMTP. Returns True on success, False otherwise.

    Required env vars (in bot/.env):
        EMAIL_USER, EMAIL_PASS
    Optional:
        EMAIL_RECIPIENTS  — comma-separated list, defaults to EMAIL_USER
    """
    _ensure_env()
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")
    if not user or not password:
        print("notifier: EMAIL_USER/EMAIL_PASS not set; skipping email")
        return False

    recipients_raw = os.getenv("EMAIL_RECIPIENTS", user)
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(user, password)
            for recipient in recipients:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = user
                msg["To"] = recipient
                msg.attach(MIMEText(text, "plain"))
                server.sendmail(user, recipient, msg.as_string())
        return True
    except Exception as e:
        print(f"notifier: email send failed: {e}")
        return False


def format_trade_alert(
    *,
    symbol: str,
    action: str,
    quantity: float,
    entry_price: float,
    tp_price: float,
    stop_price: float,
    confidence: float,
    dry_run: bool,
) -> tuple[str, str]:
    """Return (subject, body) for a trade notification."""
    tag = "[DRY-RUN] " if dry_run else "[PAPER] "
    subject = f"{tag}{action} {symbol} × {quantity} @ {entry_price:.2f}"
    body = (
        f"Symbol:     {symbol}\n"
        f"Action:     {action}\n"
        f"Quantity:   {quantity}\n"
        f"Entry:      {entry_price:.2f}\n"
        f"Take prof:  {tp_price:.2f}\n"
        f"Stop loss:  {stop_price:.2f}\n"
        f"Confidence: {confidence:.2%}\n"
        f"Mode:       {'dry-run (no order sent)' if dry_run else 'paper account (live IB order)'}\n"
    )
    return subject, body

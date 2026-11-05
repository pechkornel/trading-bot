"""
CPGW session keeper.

Runs as its own daemon. Two responsibilities:

  1. **Authenticate** on startup using core.ibkr_auth (stub in the public version)
     (core.ibkr_auth.login). Writes the resulting session cookie to a small
     file under logs/ that the runners read on startup.

  2. **Keep the session alive** by calling /tickle every ~60s.
     If /auth/status reports `authenticated: false`, run a full re-login.

Without this daemon the CPGW session would silently expire after a few minutes
of inactivity and every other process would start hitting 401s.

Usage:
    python -m live.cpgw_keeper             # loop forever
    python -m live.cpgw_keeper --once      # one auth+status check, then exit
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from core.config import LOGS_DIR, assert_paper_mode, load_app_config
from core.cpgw_client import CPGWClient
from core.ibkr_auth import IbkrCredentials, IBKRAuthError, MaxLoginAttempts, login
from live.storage import Storage


SESSION_FILE = LOGS_DIR / "cpgw_session.json"


def _read_credentials() -> IbkrCredentials:
    """Pull username/password/paper from env. Refuses to start without them."""
    user = os.getenv("IBKR_USERNAME")
    password = os.getenv("IBKR_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "IBKR_USERNAME and IBKR_PASSWORD must be set in bot/.env "
            "(or your secret manager). The auth keeper has no way to log in without them."
        )
    return IbkrCredentials(
        username=user,
        password=password,
        is_paper=os.getenv("PAPER", "true").lower() == "true",
    )


def _write_session(cookie: str, account_id: str | None) -> None:
    """Tiny atomic write so runners can read the latest cookie any time."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie": cookie,
        "account_id": account_id,
        "updated_at": time.time(),
    }
    tmp = SESSION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, SESSION_FILE)
    # 600 — readable only by the bot user
    os.chmod(SESSION_FILE, 0o600)


def read_session() -> dict[str, Any] | None:
    """Used by runners on startup."""
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class Keeper:
    """Long-running auth/tickle loop."""

    TICKLE_INTERVAL_SEC = 60
    AUTH_RECHECK_INTERVAL_SEC = 300
    REAUTH_BACKOFF_SEC = 30           # wait before retrying after a hard failure

    def __init__(self, *, once: bool) -> None:
        self.once = once
        self.stopped = False

        app_cfg = load_app_config()
        assert_paper_mode(app_cfg)              # hard paper-only lock, same as everywhere else
        self.cpgw_port = app_cfg.ib.port        # reuse existing config field; defaults to 5000 in Phase 7
        self.creds = _read_credentials()

        self.storage = Storage()
        self.client: CPGWClient | None = None
        self.last_auth_check = 0.0

    def start(self) -> None:
        banner = (
            f"CPGW KEEPER · paper={self.creds.is_paper} · port={self.cpgw_port} · "
            f"user={self.creds.username!r}"
        )
        self.storage.log_event("info", "cpgw_keeper", f"starting | {banner}")
        print(banner)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        # Try to resume an existing session BEFORE attempting fresh SRP — each
        # fresh SRP risks Dispatcher rejection if CPGW still holds the previous
        # session. Reusing the saved cookie avoids that pitfall.
        if not self._try_resume_session():
            self._full_login()

    def _try_resume_session(self) -> bool:
        """If the saved cookie still works, adopt it without doing SRP."""
        session = read_session()
        if session is None or not session.get("cookie"):
            return False
        client = CPGWClient(cookie=session["cookie"], port=self.cpgw_port)
        try:
            status = client.tickle()
            iserver = (status or {}).get("iserver", {})
            auth = (iserver or {}).get("authStatus", {})
            if auth.get("authenticated"):
                self.client = client
                if session.get("account_id"):
                    self.client.account_id = session["account_id"]
                acct = session.get("account_id")
                self.storage.log_event(
                    "info", "cpgw_keeper",
                    f"resumed existing session (account={acct}); skipped SRP",
                )
                print(f"[cpgw_keeper] resumed existing session — account={acct}")
                return True
        except Exception as e:                       # noqa: BLE001
            self.storage.log_event("info", "cpgw_keeper",
                f"cannot resume saved session ({e}); doing fresh SRP")
        return False

    def _on_signal(self, *_: Any) -> None:
        self.stopped = True

    def loop(self) -> None:
        while not self.stopped:
            t = time.time()
            try:
                self._tick()
            except Exception as e:                # noqa: BLE001
                self.storage.log_event("error", "cpgw_keeper", f"tick error: {e}")
                print(f"[cpgw_keeper] tick error: {e}")
            if self.once:
                return
            elapsed = time.time() - t
            sleep = max(0.0, self.TICKLE_INTERVAL_SEC - elapsed)
            slept = 0.0
            while slept < sleep and not self.stopped:
                time.sleep(min(1.0, sleep - slept))
                slept += 1.0

    def _tick(self) -> None:
        assert self.client is not None
        # Tickle keeps the SSO session alive.
        status = self.client.tickle()
        iserver = (status or {}).get("iserver", {})
        auth = (iserver or {}).get("authStatus", {})
        connected = auth.get("connected")
        authenticated = auth.get("authenticated")
        if not authenticated:
            # Soft nudge: re-run ssodh_init. Does NOT trigger SRP, so no
            # Dispatcher conflict risk. If ssodh_init itself fails, we leave
            # the session alone; the dashboard surfaces the persistent state.
            self.storage.log_event(
                "info", "cpgw_keeper",
                f"tickle: authenticated={authenticated} connected={connected}; "
                "nudging with ssodh_init",
            )
            try:
                self.client.ssodh_init()
            except Exception as e:                   # noqa: BLE001
                self.storage.log_event("warn", "cpgw_keeper",
                    f"ssodh_init nudge failed: {e}")
            return

        # Periodic deep auth check (every AUTH_RECHECK_INTERVAL_SEC). If
        # auth/status says we're truly gone, ssodh_init once more — only if
        # THAT also fails for repeated cycles do we fall back to fresh SRP.
        if time.time() - self.last_auth_check > self.AUTH_RECHECK_INTERVAL_SEC:
            try:
                deep = self.client.auth_status()
                self.last_auth_check = time.time()
                if not deep.get("authenticated"):
                    self.storage.log_event("warn", "cpgw_keeper",
                        f"auth/status: {deep}; nudging ssodh_init")
                    try:
                        self.client.ssodh_init()
                    except Exception as e:           # noqa: BLE001
                        self.storage.log_event("warn", "cpgw_keeper",
                            f"deep ssodh_init nudge failed: {e}")
            except Exception as e:                   # noqa: BLE001
                self.storage.log_event("warn", "cpgw_keeper",
                    f"deep auth_status check failed: {e}")

    def _full_login(self) -> None:
        try:
            cookie = login(self.creds, port=self.cpgw_port)
        except MaxLoginAttempts as e:
            self.storage.log_event("error", "cpgw_keeper",
                f"max login attempts hit — bailing: {e}")
            raise
        except IBKRAuthError as e:
            self.storage.log_event("error", "cpgw_keeper", f"login failed: {e}")
            time.sleep(self.REAUTH_BACKOFF_SEC)
            raise

        if self.client is None:
            self.client = CPGWClient(cookie=cookie, port=self.cpgw_port)
        else:
            self.client.update_cookie(cookie)

        # First call after fresh SSO login — brings up the brokerage session.
        try:
            self.client.ssodh_init()
        except Exception as e:                    # noqa: BLE001
            self.storage.log_event("warn", "cpgw_keeper", f"ssodh_init warning: {e}")

        try:
            acct = self.client.bootstrap()
        except Exception as e:                    # noqa: BLE001
            acct = None
            self.storage.log_event("warn", "cpgw_keeper", f"account bootstrap warning: {e}")

        _write_session(cookie, acct)
        self.storage.log_event("info", "cpgw_keeper",
            f"login OK — account={acct} session written to {SESSION_FILE.name}")
        print(f"[cpgw_keeper] login OK — account={acct}")

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
        self.storage.log_event("info", "cpgw_keeper", "stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CPGW session keeper (Phase 7).")
    p.add_argument("--once", action="store_true",
                   help="One login + status check, then exit (useful for testing)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    keeper = Keeper(once=args.once)
    keeper.start()
    try:
        keeper.loop()
    finally:
        keeper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

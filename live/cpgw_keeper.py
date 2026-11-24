"""
CPGW session keeper.

Runs as its own daemon. It keeps the gateway session alive with /tickle, and logs in
again after the gateway's resets, under the rules in `live/keeper_policy.py`:

  - never logs in while the gateway reports authenticated, or while it is down;
  - no fresh login inside the nightly gap or the Saturday maintenance window;
  - backoff after a failed login, and a persisted lockout (`logs/keeper_state.json`)
    after a failure that needs a person or too many submitted attempts;
  - every login must land on paper accounts only (prefix from the keeper config),
    otherwise it logs out, locks out and alerts.

Credentials come from the chmod 600 secret file (`core.secret_store`), never from the
environment: the keeper refuses to start while IBKR_PASSWORD is set. The username is
masked and every event or alert text goes through the Redactor.

Usage:
    python -m live.cpgw_keeper                  # loop forever
    python -m live.cpgw_keeper --once           # one check (and login if due), then exit
    python -m live.cpgw_keeper --reset-lockout  # clear the lockout after fixing the cause
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.auth_types import IBKRAuthError, IbkrCredentials, ManualStepRequired, NotPaperAccount
from core.config import (
    LOGS_DIR,
    KeeperConfig,
    assert_paper_mode,
    load_app_config,
    load_keeper_config,
)
from core.cpgw_client import CPGWClient, CPGWError, NotAuthenticatedError, mask_account
from core.ibkr_auth import AUTH_BACKEND, login
from core.secret_store import Redactor, load_login_secrets
from live import keeper_policy as policy
from live.keeper_policy import ActionKind, Observation
from live.storage import Storage
from live.telegram_notifier import TelegramConfig, TelegramNotifier

SESSION_FILE = LOGS_DIR / "cpgw_session.json"
KEEPER_STATE_FILE = LOGS_DIR / "keeper_state.json"

GATEWAY_DOWN_ALERT_AFTER_SEC = 300
LOCKOUT_REMINDER_SEC = 86400
SOURCE = "cpgw_keeper"


def _write_session(cookie: str, account_id: str | None, path: Path | None = None) -> None:
    """Tiny atomic write so runners can read the latest cookie any time.

    The cookie may be empty: the gateway holds the session itself."""
    path = Path(path) if path is not None else SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie": cookie,
        "account_id": account_id,
        "updated_at": time.time(),
    }
    tmp = path.with_suffix(".json.tmp")
    # 600 from the first byte: readable only by the bot user, no world readable window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # also fixes a stale temp file and ignores the umask
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _read_session_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_session() -> dict[str, Any] | None:
    """Used by runners on startup."""
    return _read_session_file(SESSION_FILE)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_client(cookie: str, port: int) -> CPGWClient:
    return CPGWClient(cookie=cookie, port=port)


def _default_probe(port: int) -> CPGWClient:
    """A client with no cookie, so its answer does not depend on the stored session file."""
    return CPGWClient(cookie="", port=port)


class Keeper:
    """Long-running auth/tickle loop driven by `keeper_policy.decide`."""

    def __init__(
        self,
        *,
        once: bool,
        keeper_cfg: KeeperConfig | None = None,
        login_fn: Callable[..., str] | None = None,
        client_factory: Callable[[str, int], Any] | None = None,
        probe_factory: Callable[[int], Any] | None = None,
        notifier: Any = None,
        storage: Any = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | None = None,
        session_file: Path | None = None,
        sleep: Callable[[float], None] | None = None,
        install_signals: bool = True,
    ) -> None:
        # The password must only ever come from the secret file. Refuse before reading
        # anything else, and never put the value in the message.
        if os.environ.get("IBKR_PASSWORD"):
            raise RuntimeError(
                "IBKR_PASSWORD is set in the environment. Remove it from .env and the "
                "container environment: the keeper reads credentials from the secret file "
                "only (secrets/ibkr_login, see secrets/README.md)."
            )
        self.once = once
        self.stopped = False
        self.cfg = keeper_cfg or load_keeper_config()

        app_cfg = load_app_config()
        assert_paper_mode(app_cfg)  # hard paper-only lock, same as everywhere else
        self.cpgw_port = app_cfg.ib.port
        self.creds: IbkrCredentials = load_login_secrets()
        self.redactor = Redactor([self.creds.password, self.creds.username])

        self._login = login_fn or login
        self._client_factory = client_factory or _default_client
        self._probe_factory = probe_factory or _default_probe
        self._clock = clock or _default_clock
        self._sleep = sleep or time.sleep
        self._install_signals = install_signals
        self.state_path = Path(state_path) if state_path is not None else KEEPER_STATE_FILE
        self.session_file = Path(session_file) if session_file is not None else SESSION_FILE

        self.storage = storage if storage is not None else Storage()
        self.notifier = (
            notifier
            if notifier is not None
            else TelegramNotifier(TelegramConfig.from_env(), source="keeper", storage=self.storage)
        )

        self.state, self._pending_warning = policy.load_state(self.state_path)

        session = _read_session_file(self.session_file) or {}
        self.client: Any = self._client_factory(str(session.get("cookie") or ""), self.cpgw_port)
        self._session_verified = False
        self._down_since: datetime | None = None
        self._last_down_alert: datetime | None = None
        self._last_wait: tuple[str, datetime | None] | None = None

    # ---- output helpers (everything passes the Redactor) ----

    def _event(self, level: str, message: str) -> None:
        try:
            self.storage.log_event(level, SOURCE, mask_account(self.redactor.redact(message)))
        except Exception:  # noqa: BLE001 - logging must not stop the keeper
            pass

    def _alert(self, level: str, text: str, *, key: str, cooldown_sec: float | None = None) -> None:
        body = f"{level.upper()} {mask_account(self.redactor.redact(text))}"
        try:
            self.notifier.send("session", body, dedupe_key=key, cooldown_sec=cooldown_sec)
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        try:
            policy.save_state(self.state_path, self.state)
        except OSError as exc:
            self._event("error", f"cannot save keeper state: {type(exc).__name__}")

    # ---- lifecycle ----

    def start(self) -> None:
        masked = f"{self.creds.username[:2]}***"
        banner = (
            f"CPGW KEEPER · paper={self.creds.is_paper} · port={self.cpgw_port} · "
            f"user={masked} · auth={AUTH_BACKEND}"
        )
        self._event("info", f"starting | {banner}")
        print(banner)
        if self._pending_warning:
            self._event("warn", self._pending_warning)
            self._alert(
                "warn",
                "the keeper state file is corrupt, so the keeper is locked out. Check "
                "logs/keeper_state.json, then run: python -m live.cpgw_keeper --reset-lockout",
                key="lockout",
                cooldown_sec=LOCKOUT_REMINDER_SEC,
            )
            self._pending_warning = None
        if self._install_signals:
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)
        # Check the gateway BEFORE any login: a session that is still alive is adopted.
        self._adopt_if_authenticated(self._observe(), self._clock())

    def _on_signal(self, *_: Any) -> None:
        self.stopped = True

    def loop(self) -> None:
        while not self.stopped:
            t = time.monotonic()
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._event("error", f"tick error: {type(e).__name__}: {e}")
                print(f"[cpgw_keeper] tick error: {type(e).__name__}")
            if self.once:
                return
            remaining = max(0.0, self.cfg.tickle_interval_sec - (time.monotonic() - t))
            slept = 0.0
            while slept < remaining and not self.stopped:
                self._sleep(min(1.0, remaining - slept))
                slept += 1.0

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
        self._event("info", "stopped")

    # ---- observation ----

    def _observe(self) -> Observation:
        try:
            status = self.client.tickle()
            auth = ((status or {}).get("iserver") or {}).get("authStatus") or {}
            if auth.get("authenticated"):
                return Observation.AUTHENTICATED
            # Soft nudge. It does not log in, so it cannot collide with the gateway session.
            try:
                self.client.ssodh_init()
            except NotAuthenticatedError:
                return Observation.NOT_AUTHENTICATED
            except CPGWError as e:
                self._event("warn", f"ssodh_init nudge failed: {e}")
            deep = self.client.auth_status() or {}
            if deep.get("authenticated"):
                return Observation.AUTHENTICATED
            return Observation.NOT_AUTHENTICATED
        except NotAuthenticatedError:
            return Observation.NOT_AUTHENTICATED
        except Exception as e:  # noqa: BLE001 - transport errors and anything unexpected
            self._event("warn", f"gateway check failed: {type(e).__name__}: {e}")
            return Observation.GATEWAY_DOWN

    def _gateway_state_before_login(self) -> str:
        """Re-check right before a login: 'authenticated', 'down', 'unsure' or 'ok'.

        The first check uses the stored cookie. A lost or stale session file would make it
        say "not authenticated" while the gateway holds a live session for another cookie,
        so a second check runs with no cookie. Only an explicit "authenticated: false"
        from that second check allows a login; anything else means do not submit."""
        try:
            if (self.client.auth_status() or {}).get("authenticated"):
                return "authenticated"
        except NotAuthenticatedError:
            pass
        except Exception:  # noqa: BLE001
            return "down"
        return self._cookieless_state()

    def _cookieless_state(self) -> str:
        probe = None
        try:
            probe = self._probe_factory(self.cpgw_port)
            answer = probe.auth_status()
        except Exception:  # noqa: BLE001 - any error, including a 401, is inconclusive
            return "unsure"
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:  # noqa: BLE001
                    pass
        if not isinstance(answer, dict) or not isinstance(answer.get("authenticated"), bool):
            return "unsure"
        return "authenticated" if answer["authenticated"] else "ok"

    # ---- one tick ----

    def tick(self) -> None:
        now = self._clock()
        obs = self._observe()
        self._track_gateway(obs, now)
        if obs is not Observation.AUTHENTICATED:
            self._session_verified = False
        self._adopt_if_authenticated(obs, now)
        action = policy.decide(self.state, obs, now, self.cfg)

        if action.kind is ActionKind.TICKLE:
            self._last_wait = None
        elif action.kind is ActionKind.WAIT:
            self._on_wait(action)
        elif action.kind is ActionKind.LOCKED_OUT:
            self._on_locked_out(now, action)
        elif action.kind is ActionKind.LOGIN:
            self._on_login(action, now)

    def _track_gateway(self, obs: Observation, now: datetime) -> None:
        """Gateway-down alerts: once after the threshold, then at the alert cooldown."""
        if obs is not Observation.GATEWAY_DOWN:
            if self._down_since is not None:
                self._event("info", "gateway is reachable again")
            self._down_since = None
            self._last_down_alert = None
            return
        if self._down_since is None:
            self._down_since = now
            self._event("warn", "gateway is not reachable; no login will be tried")
        down_for = (now - self._down_since).total_seconds()
        if down_for < GATEWAY_DOWN_ALERT_AFTER_SEC:
            return
        cooldown = self.cfg.session_lost_alert_cooldown_sec
        if (
            self._last_down_alert is None
            or (now - self._last_down_alert).total_seconds() >= cooldown
        ):
            self._last_down_alert = now
            self._alert(
                "warn",
                f"the gateway has been unreachable for {int(down_for // 60)} min. "
                "Check the ibkr_gateway container. The keeper does not log in while it is down.",
                key="gateway_down",
                cooldown_sec=cooldown,
            )

    def _adopt_if_authenticated(self, obs: Observation, now: datetime) -> None:
        """A live session we did not just create: run the paper guard and record it."""
        if obs is not Observation.AUTHENTICATED or self._session_verified:
            return
        try:
            account = self._paper_guard()
        except NotPaperAccount as e:
            self._handle_login_failure(e, now)
            return
        self._session_verified = True
        if self.state.authenticated_since is None:
            self.state.authenticated_since = now
            self._save()
        self._event("info", f"existing session verified (account={account}); no login needed")

    # ---- window, backoff and lockout handling ----

    def _on_wait(self, action: policy.Action) -> None:
        if action.reason == "gateway_down":
            return
        marker = (action.reason, action.until)
        if marker == self._last_wait:
            return
        self._last_wait = marker
        until = action.until
        if until is None:
            return
        local = until.astimezone(ZoneInfo(self.cfg.timezone))
        if action.reason == "window":
            window = "the Saturday maintenance window" if local.hour == 0 else "the nightly gap"
            text = f"session dropped during {window}, next login at {local:%H:%M}"
            self._event("info", text)
            self._alert(
                "info", text, key=f"window:{until.isoformat()}", cooldown_sec=LOCKOUT_REMINDER_SEC
            )
        else:
            self._event("info", f"login backoff, next try at {until.isoformat()}")

    def _on_locked_out(self, now: datetime, action: policy.Action) -> None:
        reason = self.state.locked_out_reason or action.reason or "too_many_failed_logins"
        if policy.should_alert(self.state, "lockout", now, LOCKOUT_REMINDER_SEC):
            self._send_lockout_alert(reason, now)

    def _send_lockout_alert(self, reason: str, now: datetime) -> None:
        policy.mark_alert(self.state, "lockout", now)
        self._save()
        self._event("error", f"keeper locked out: {reason}")
        self._alert(
            "error",
            f"the keeper is locked out and will not log in again ({reason}). Fix the cause "
            "(see the keeper logs), then run: python -m live.cpgw_keeper --reset-lockout",
            key="lockout",
            cooldown_sec=LOCKOUT_REMINDER_SEC,
        )

    # ---- login ----

    def _on_login(self, action: policy.Action, now: datetime) -> None:
        gateway = self._gateway_state_before_login()
        if gateway == "authenticated":
            self._event("info", "gateway became authenticated before login; skipping login")
            return
        if gateway == "down":
            self._event("warn", "gateway not reachable right before login; skipping login")
            return
        if gateway == "unsure":
            self._event(
                "warn",
                "the cookie-less gateway check gave no clear answer right before login; "
                "skipping login (waiting)",
            )
            return
        cooldown = self.cfg.session_lost_alert_cooldown_sec
        if action.expected:
            self._alert(
                "info",
                "the 24 h session reset happened, logging in again",
                key="session_expected",
                cooldown_sec=cooldown,
            )
        else:
            self._alert(
                "warn", "session lost, logging in again", key="session_lost", cooldown_sec=cooldown
            )
        self._full_login(now)

    def _on_submit(self) -> None:
        """Called by the login flow right before the form is submitted.

        A failed save raises, so the form is never submitted without a durable record."""
        policy.record_submit(self.state, self._clock())
        try:
            policy.save_state(self.state_path, self.state)
        except OSError as exc:
            self._event("error", f"cannot save keeper state before login: {type(exc).__name__}")
            raise IBKRAuthError(
                "cannot record the login attempt, so no login was submitted"
            ) from None

    def _full_login(self, now: datetime) -> None:
        try:
            cookie = self._login(self.creds, port=self.cpgw_port, on_submit=self._on_submit)
        except NotImplementedError:
            # The public stub has no login. Retrying cannot help, so lock out at once.
            self._handle_login_failure(
                ManualStepRequired("automated login is not available in this build"), now
            )
            return
        except IBKRAuthError as e:
            self._handle_login_failure(e, now)
            return
        except Exception as e:  # noqa: BLE001 - unknown failure: count it and back off
            self._handle_login_failure(IBKRAuthError(f"{type(e).__name__}: {e}"), now)
            return

        try:
            account = self._finish_login(cookie)
        except IBKRAuthError as e:
            self._handle_login_failure(e, now)
            return

        policy.record_login_success(self.state, now, self.cfg)
        self._session_verified = True
        self._last_wait = None
        self._save()
        self._event(
            "info", f"login OK, account={account}, session written to {self.session_file.name}"
        )
        print(f"[cpgw_keeper] login OK, account={mask_account(account)}")
        self._alert("info", "login OK, session restored", key="login_ok", cooldown_sec=0)

    def _finish_login(self, cookie: str) -> str:
        self.client.update_cookie(cookie)
        try:
            status = self.client.auth_status() or {}
        except Exception as e:  # noqa: BLE001
            raise IBKRAuthError(f"cannot confirm the login ({type(e).__name__})") from None
        if not status.get("authenticated"):
            raise IBKRAuthError("login finished but the gateway is not authenticated")
        try:
            self.client.ssodh_init()
        except Exception as e:  # noqa: BLE001
            self._event("warn", f"ssodh_init warning: {e}")
        account = self._paper_guard()
        try:
            self.client.bootstrap()
        except Exception as e:  # noqa: BLE001
            self._event("warn", f"account bootstrap warning: {e}")
        self.client.account_id = account
        _write_session(cookie, account, self.session_file)
        return account

    def _paper_guard(self) -> str:
        """Every account must carry the paper prefix. Fails closed: logs out and raises."""
        prefix = self.cfg.paper_account_prefix
        try:
            accounts = self.client.list_accounts() or []
            ids = [str(a.get("id") or a.get("accountId") or "") for a in accounts]
        except Exception:  # noqa: BLE001
            ids = []
        if not ids or not all(i.startswith(prefix) for i in ids):
            try:
                self.client.logout()
            except Exception:  # noqa: BLE001
                pass
            raise NotPaperAccount("the logged in account is not a paper account")
        return ids[0]

    def _handle_login_failure(self, exc: BaseException, now: datetime) -> None:
        policy.record_login_failure(self.state, exc, now, self.cfg)
        self._session_verified = False
        self._save()
        self._event("error", f"login failed: {type(exc).__name__}: {exc}")
        if isinstance(exc, NotPaperAccount):
            self._alert(
                "error",
                "the gateway session is not a paper account. The keeper logged out "
                "and locked itself.",
                key="not_paper",
                cooldown_sec=LOCKOUT_REMINDER_SEC,
            )
        if self.state.locked_out_reason:
            self._send_lockout_alert(self.state.locked_out_reason, now)


def reset_lockout(state_path: Path, *, storage: Any = None) -> None:
    """Clear the lockout and the failure counters. Keeps session times and alert stamps."""
    state, _ = policy.load_state(state_path)
    policy.save_state(state_path, policy.clear_lockout(state))
    if storage is not None:
        try:
            storage.log_event("info", SOURCE, "lockout cleared with --reset-lockout")
        except Exception:  # noqa: BLE001
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CPGW session keeper.")
    p.add_argument(
        "--once",
        action="store_true",
        help="One status check (and a login if one is due), then exit (useful for testing)",
    )
    p.add_argument(
        "--reset-lockout",
        action="store_true",
        help="Clear the lockout after you fixed the cause, then exit",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.reset_lockout:
        reset_lockout(KEEPER_STATE_FILE, storage=Storage())
        print("keeper lockout cleared")
        return 0
    keeper = Keeper(once=args.once)
    keeper.start()
    try:
        keeper.loop()
    finally:
        keeper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

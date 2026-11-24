"""Pure session policy for the gateway keeper.

`decide` maps (state, observation, now, config) to one action. It does no I/O and never
reads the clock: `now` is an aware datetime passed in by the caller. The only I/O in this
module is `load_state` and `save_state` for the persisted state file.

Lockout safety: a missing state file is a fresh install, but a corrupt or unreadable one is
treated as locked out until `--reset-lockout`, never as unlocked. The state holds no secret.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from core.auth_types import (
    BadCredentials,
    ManualStepRequired,
    NotPaperAccount,
    SecondFactorRequired,
)
from core.config import KeeperConfig

# Failures that need a person: no retry after the first one.
FATAL_LOGIN_ERRORS = (BadCredentials, SecondFactorRequired, ManualStepRequired, NotPaperAccount)

_MAX_TRACKED_ATTEMPTS = 50
_MAX_TRACKED_SUBMITS = 200
_END_OF_DAY = time(23, 59)


class Observation(Enum):
    AUTHENTICATED = "authenticated"
    NOT_AUTHENTICATED = "not_authenticated"
    GATEWAY_DOWN = "gateway_down"


class ActionKind(Enum):
    TICKLE = "tickle"
    WAIT = "wait"
    LOGIN = "login"
    LOCKED_OUT = "locked_out"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    until: Optional[datetime] = None  # WAIT only: when to look again (None: no fixed time)
    expected: bool = False  # LOGIN only: the drop was the planned 24 h reset
    reason: str = ""  # short, secret-free tag for logs and alerts


@dataclass
class KeeperState:
    last_login_at: Optional[datetime] = None
    authenticated_since: Optional[datetime] = None
    failed_attempts: list[datetime] = field(default_factory=list)  # submitted attempts
    locked_out_reason: Optional[str] = None
    last_alert_at: dict[str, datetime] = field(default_factory=dict)
    consecutive_failures: int = 0
    last_attempt_at: Optional[datetime] = None
    # Every submit time. A successful login does not clear it, so the total cap holds
    # even when each login works and the session then drops again.
    submit_times: list[datetime] = field(default_factory=list)


# ---- decision ----


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


def _at(day: date, t: time, tz: ZoneInfo, fold: int) -> datetime:
    return datetime.combine(day, t, tzinfo=tz).replace(fold=fold)


def _window_end(local: datetime, cfg: KeeperConfig, tz: ZoneInfo) -> Optional[datetime]:
    """End of the no-login window that contains `local`, or None.

    Start is inclusive and end is exclusive. A maintenance end of 23:59 means the end of
    the day (the config forbids a midnight wrap), so the window ends at the next 00:00."""
    t = local.time().replace(tzinfo=None)
    day = local.date()
    if local.weekday() == cfg.maintenance_weekday:
        start, end = cfg.maintenance_start_time, cfg.maintenance_end_time
        if end >= _END_OF_DAY:
            if t >= start:
                return _at(day + timedelta(days=1), time(0, 0), tz, 0)
        elif start <= t < end:
            return _at(day, end, tz, local.fold)
    gap_start, gap_end = cfg.nightly_gap_start_time, cfg.nightly_gap_end_time
    if gap_start <= t < gap_end:
        return _at(day, gap_end, tz, local.fold)
    return None


def backoff_delay_sec(consecutive_failures: int, cfg: KeeperConfig) -> int:
    """60, 120, 240, ... capped at `login_backoff_max_sec`. Zero before any failure."""
    if consecutive_failures <= 0:
        return 0
    exponent = min(consecutive_failures - 1, 32)
    return min(cfg.login_backoff_base_sec * (2**exponent), cfg.login_backoff_max_sec)


def _recent_failures(state: KeeperState, now: datetime, cfg: KeeperConfig) -> int:
    cutoff = now - timedelta(seconds=cfg.failed_login_window_sec)
    return sum(1 for ts in state.failed_attempts if ts > cutoff)


def _recent_submits(state: KeeperState, now: datetime, cfg: KeeperConfig) -> int:
    cutoff = now - timedelta(seconds=cfg.failed_login_window_sec)
    return sum(1 for ts in state.submit_times if ts > cutoff)


def decide(
    state: KeeperState, observation: Observation, now: datetime, cfg: KeeperConfig
) -> Action:
    _require_aware(now)
    if observation is Observation.AUTHENTICATED:
        return Action(ActionKind.TICKLE)
    if observation is Observation.GATEWAY_DOWN:
        # Credentials are never submitted to a gateway that is down.
        return Action(ActionKind.WAIT, reason="gateway_down")
    if state.locked_out_reason:
        return Action(ActionKind.LOCKED_OUT, reason=state.locked_out_reason)
    if _recent_failures(state, now, cfg) >= cfg.max_failed_logins:
        return Action(ActionKind.LOCKED_OUT, reason="too_many_failed_logins")
    if _recent_submits(state, now, cfg) >= cfg.max_logins_per_window:
        return Action(
            ActionKind.LOCKED_OUT,
            reason=(
                f"login cap reached: {cfg.max_logins_per_window} logins in "
                f"{cfg.failed_login_window_sec // 3600} h, so the session keeps dropping. "
                "Find out why, then wait or run --reset-lockout"
            ),
        )

    tz = ZoneInfo(cfg.timezone)
    local = now.astimezone(tz)
    until = _window_end(local, cfg, tz)
    if until is not None:
        return Action(ActionKind.WAIT, until=until, reason="window")

    if state.consecutive_failures > 0 and state.last_attempt_at is not None:
        retry_at = state.last_attempt_at + timedelta(
            seconds=backoff_delay_sec(state.consecutive_failures, cfg)
        )
        if now < retry_at:
            return Action(ActionKind.WAIT, until=retry_at, reason="backoff")

    since = state.authenticated_since or state.last_login_at
    expected = since is not None and (now - since).total_seconds() >= cfg.max_session_age_sec
    return Action(ActionKind.LOGIN, expected=expected, reason="session_age" if expected else "lost")


# ---- state changes (in place, no I/O) ----


def record_submit(state: KeeperState, now: datetime) -> None:
    """Call before the login form is submitted, so a crash cannot hide the attempt."""
    state.failed_attempts.append(now)
    del state.failed_attempts[:-_MAX_TRACKED_ATTEMPTS]
    state.submit_times.append(now)
    del state.submit_times[:-_MAX_TRACKED_SUBMITS]


def record_login_failure(
    state: KeeperState, exc: BaseException, now: datetime, cfg: KeeperConfig
) -> None:
    """Count a failed login and lock out when it needs a person or the cap is reached.

    The reason holds the exception class name only, never its text."""
    state.consecutive_failures += 1
    state.last_attempt_at = now
    if isinstance(exc, FATAL_LOGIN_ERRORS):
        state.locked_out_reason = f"{type(exc).__name__}: fix the cause, then run --reset-lockout"
    elif _recent_failures(state, now, cfg) >= cfg.max_failed_logins:
        state.locked_out_reason = (
            f"{cfg.max_failed_logins} failed logins in "
            f"{cfg.failed_login_window_sec // 3600} h: fix the cause, then run --reset-lockout"
        )


def record_login_success(
    state: KeeperState, now: datetime, cfg: Optional[KeeperConfig] = None
) -> None:
    """Record a good login. `submit_times` is kept for the total cap.

    When the session this login replaces lasted less than `min_session_sec`, the backoff
    counter goes up instead of resetting, so a session that drops right after each login
    waits 60, 120, 240 ... seconds between logins."""
    cfg = cfg or KeeperConfig()
    previous = state.authenticated_since
    short = previous is not None and (now - previous).total_seconds() < cfg.min_session_sec
    state.last_login_at = now
    state.authenticated_since = now
    state.failed_attempts = []
    state.locked_out_reason = None
    if short:
        state.consecutive_failures += 1
        state.last_attempt_at = now
    else:
        state.consecutive_failures = 0
        state.last_attempt_at = None


def clear_lockout(state: KeeperState) -> KeeperState:
    """Fresh counters and no lockout; keeps the session times and alert stamps."""
    return KeeperState(
        last_login_at=state.last_login_at,
        authenticated_since=state.authenticated_since,
        last_alert_at=dict(state.last_alert_at),
    )


def should_alert(state: KeeperState, key: str, now: datetime, cooldown_sec: int) -> bool:
    last = state.last_alert_at.get(key)
    return last is None or (now - last).total_seconds() >= cooldown_sec


def mark_alert(state: KeeperState, key: str, now: datetime) -> None:
    state.last_alert_at[key] = now


# ---- persistence ----


def _dt_out(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _dt_in(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _state_to_dict(state: KeeperState) -> dict:
    return {
        "last_login_at": _dt_out(state.last_login_at),
        "authenticated_since": _dt_out(state.authenticated_since),
        "failed_attempts": [ts.isoformat() for ts in state.failed_attempts],
        "locked_out_reason": state.locked_out_reason,
        "last_alert_at": {k: v.isoformat() for k, v in state.last_alert_at.items()},
        "consecutive_failures": state.consecutive_failures,
        "last_attempt_at": _dt_out(state.last_attempt_at),
        "submit_times": [ts.isoformat() for ts in state.submit_times],
    }


def _state_from_dict(data: object) -> KeeperState:
    if not isinstance(data, dict):
        raise ValueError("state must be a JSON object")
    attempts = data.get("failed_attempts", [])
    alerts = data.get("last_alert_at", {})
    if not isinstance(attempts, list) or not isinstance(alerts, dict):
        raise ValueError("bad failed_attempts or last_alert_at")
    failures = data.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("bad consecutive_failures")
    reason = data.get("locked_out_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("bad locked_out_reason")
    parsed_attempts = []
    for ts in attempts:
        stamp = _dt_in(ts)
        if stamp is None:
            raise ValueError("bad failed_attempts entry")
        parsed_attempts.append(stamp)
    submits = data.get("submit_times", [])
    if not isinstance(submits, list):
        raise ValueError("bad submit_times")
    parsed_submits = []
    for ts in submits:
        stamp = _dt_in(ts)
        if stamp is None:
            raise ValueError("bad submit_times entry")
        parsed_submits.append(stamp)
    parsed_alerts = {}
    for key, ts in alerts.items():
        stamp = _dt_in(ts)
        if not isinstance(key, str) or stamp is None:
            raise ValueError("bad last_alert_at entry")
        parsed_alerts[key] = stamp
    return KeeperState(
        last_login_at=_dt_in(data.get("last_login_at")),
        authenticated_since=_dt_in(data.get("authenticated_since")),
        failed_attempts=parsed_attempts,
        locked_out_reason=reason or None,
        last_alert_at=parsed_alerts,
        consecutive_failures=failures,
        last_attempt_at=_dt_in(data.get("last_attempt_at")),
        submit_times=parsed_submits,
    )


def load_state(path: Path) -> tuple[KeeperState, Optional[str]]:
    """Load the state file. Returns (state, warning).

    A missing file is a fresh install: (fresh state, None). Any unreadable, malformed or
    wrongly typed file returns a state that is locked out until `--reset-lockout`, plus a
    warning text. It never unlocks a lockout that may have been recorded in that file."""
    path = Path(path)
    if not path.exists():
        return KeeperState(), None
    try:
        state = _state_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        warning = (
            f"keeper state file {path} is unreadable or corrupt ({type(exc).__name__}); "
            "treating the keeper as locked out until --reset-lockout"
        )
        return (
            KeeperState(locked_out_reason="state file corrupt: check it, then run --reset-lockout"),
            warning,
        )
    return state, None


def save_state(path: Path, state: KeeperState) -> None:
    """Write the state atomically (temp file in the same folder, then rename) with mode 600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".keeper_state.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_state_to_dict(state), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

"""Keeper wiring tests: fake client, fake login, fake notifier, temp logs dir, fake clock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.auth_types import (
    BadCredentials,
    GatewayUnreachable,
    ManualStepRequired,
    SecondFactorRequired,
)
from core.config import KeeperConfig
from core.cpgw_client import CPGWError, NotAuthenticatedError
from live import cpgw_keeper
from live.cpgw_keeper import Keeper
from live.keeper_policy import KeeperState, load_state, save_state

NY = ZoneInfo("America/New_York")
FAKE_USER = "paperuser-x1"
FAKE_PASSWORD = "not-a-real-password-7f3a"
FAKE_COOKIE = "x-sess-uuid=fake"
# 2025-06-10 is a Tuesday, outside every no-login window.
TUE_NOON = datetime(2025, 6, 10, 12, 0, tzinfo=NY)


class FakeClock:
    def __init__(self, now: datetime = TUE_NOON) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class FakeClient:
    def __init__(self, cookie: str = "", *, port: int = 5000) -> None:
        self.cookie = cookie
        self.port = port
        self.account_id = None
        self.authenticated = False
        self.down = False
        self.accounts = [{"id": "DU1234567"}]
        self.calls: list[str] = []

    def _check(self) -> None:
        if self.down:
            raise CPGWError("HTTP transport error on POST /tickle: connection refused")

    def tickle(self):
        self.calls.append("tickle")
        self._check()
        if not self.authenticated:
            raise NotAuthenticatedError("401")
        return {"iserver": {"authStatus": {"authenticated": True, "connected": True}}}

    def auth_status(self):
        self.calls.append("auth_status")
        self._check()
        return {"authenticated": self.authenticated}

    def ssodh_init(self, **kw):
        self.calls.append("ssodh_init")
        self._check()
        return {}

    def list_accounts(self):
        self.calls.append("list_accounts")
        return self.accounts

    def bootstrap(self):
        self.calls.append("bootstrap")
        return self.accounts[0]["id"]

    def logout(self):
        self.calls.append("logout")
        self.authenticated = False
        return {}

    def update_cookie(self, cookie):
        self.cookie = cookie

    def close(self):
        pass


class FakeCookielessProbe:
    """Cookie-less check that mirrors the fake gateway unless a test replaces the answer."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def auth_status(self):
        return {"authenticated": self.client.authenticated}

    def close(self):
        pass


class FakeStorage:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def log_event(self, level, source, message):
        self.events.append((level, source, message))


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, kind, text, *, dedupe_key=None, cooldown_sec=None):
        self.sent.append({"kind": kind, "text": text, "key": dedupe_key, "cooldown": cooldown_sec})
        return True


class Env:
    """Everything a Keeper needs, wired to fakes in tmp_path."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.dir = tmp_path
        self.secret = tmp_path / "ibkr_login"
        self.secret.write_text(f"IBKR_USERNAME={FAKE_USER}\nIBKR_PASSWORD={FAKE_PASSWORD}\n")
        os.chmod(self.secret, 0o600)
        monkeypatch.setenv("IBKR_LOGIN_SECRET_FILE", str(self.secret))
        self.client = FakeClient()
        self.storage = FakeStorage()
        self.notifier = FakeNotifier()
        self.clock = FakeClock()
        self.login_calls = 0
        self.login_script: list = []  # items: exception to raise, or cookie string
        self.state_path = tmp_path / "keeper_state.json"
        self.session_file = tmp_path / "cpgw_session.json"

    def login(self, creds, *, port, on_submit=None, **kw):
        self.login_calls += 1
        if on_submit is not None:
            on_submit()
        item = self.login_script.pop(0) if self.login_script else FAKE_COOKIE
        if isinstance(item, BaseException):
            raise item
        self.client.authenticated = True
        return item

    def keeper(self, *, once=False, cfg=None) -> Keeper:
        return Keeper(
            once=once,
            keeper_cfg=cfg or KeeperConfig(),
            login_fn=self.login,
            client_factory=lambda cookie, port: self.client,
            probe_factory=lambda port: FakeCookielessProbe(self.client),
            notifier=self.notifier,
            storage=self.storage,
            clock=self.clock,
            state_path=self.state_path,
            session_file=self.session_file,
            sleep=lambda s: None,
            install_signals=False,
        )

    def state(self) -> KeeperState:
        return load_state(self.state_path)[0]

    def all_text(self) -> list[str]:
        texts = [m for _, _, m in self.storage.events]
        texts += [n["text"] for n in self.notifier.sent]
        for p in (self.state_path, self.session_file):
            if p.exists():
                texts.append(p.read_text())
        return texts


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def assert_no_secret(texts, secrets=(FAKE_USER, FAKE_PASSWORD)):
    for text in texts:
        for secret in secrets:
            assert secret not in text, f"secret leaked in: {text!r}"


# ---- start-up checks ----


def test_env_password_refuses_to_start_without_echoing_it(env, monkeypatch):
    monkeypatch.setenv("IBKR_PASSWORD", "env-pass-9z9z")
    with pytest.raises(RuntimeError) as info:
        env.keeper()
    text = "".join(traceback.format_exception(info.value)) + str(info.value) + repr(info.value)
    assert "env-pass-9z9z" not in text
    assert "IBKR_PASSWORD" in str(info.value)


def test_banner_masks_username(env, capsys):
    keeper = env.keeper()
    keeper.client.authenticated = True
    keeper.start()
    out = capsys.readouterr().out
    assert FAKE_USER not in out
    assert "pa***" in out
    assert_no_secret(env.all_text())


# ---- decisions ----


def test_already_authenticated_gateway_means_no_login(env):
    env.client.authenticated = True
    keeper = env.keeper()
    keeper.start()
    keeper.tick()
    assert env.login_calls == 0
    assert "logout" not in env.client.calls


def test_drop_then_success_writes_session_and_sends_one_info_alert(env):
    keeper = env.keeper()
    keeper.tick()  # not authenticated, outside any window: logs in
    assert env.login_calls == 1
    session = json.loads(env.session_file.read_text())
    assert session["cookie"] == FAKE_COOKIE
    assert session["account_id"] == "DU1234567"
    infos = [n for n in env.notifier.sent if n["text"].startswith("INFO")]
    assert len(infos) == 1
    assert "login OK" in infos[0]["text"]
    st = env.state()
    assert st.consecutive_failures == 0
    assert st.failed_attempts == []
    assert st.authenticated_since == env.clock.now


def test_empty_cookie_is_allowed(env):
    env.login_script = [""]
    keeper = env.keeper()
    keeper.tick()
    assert json.loads(env.session_file.read_text())["cookie"] == ""
    assert env.state().locked_out_reason is None


def test_submit_is_recorded_before_the_click(env):
    seen = {}

    def login(creds, *, port, on_submit=None, **kw):
        on_submit()
        seen["state_on_disk"] = load_state(env.state_path)[0].failed_attempts
        raise BadCredentials("x")

    env.login = login
    keeper = env.keeper()
    keeper.tick()
    assert len(seen["state_on_disk"]) == 1


def test_second_factor_alerts_once_locks_out_and_restart_does_not_login(env):
    env.login_script = [SecondFactorRequired("needs 2fa")]
    keeper = env.keeper()
    keeper.tick()
    assert env.login_calls == 1
    assert env.state().locked_out_reason
    lockout_alerts = [n for n in env.notifier.sent if "reset-lockout" in n["text"]]
    assert len(lockout_alerts) == 1
    keeper.tick()  # still locked out, no second alert, no login
    assert env.login_calls == 1
    assert len([n for n in env.notifier.sent if "reset-lockout" in n["text"]]) == 1

    restarted = env.keeper()  # new instance reads the persisted lockout
    restarted.start()
    restarted.tick()
    assert env.login_calls == 1


def test_reset_lockout_clears_state(env):
    st = KeeperState(locked_out_reason="X: fix it", consecutive_failures=2)
    save_state(env.state_path, st)
    storage = FakeStorage()
    cpgw_keeper.reset_lockout(env.state_path, storage=storage)
    assert env.state().locked_out_reason is None
    assert env.state().consecutive_failures == 0
    assert storage.events


def test_main_reset_lockout_flag(env, monkeypatch, capsys):
    save_state(env.state_path, KeeperState(locked_out_reason="X: fix it"))
    monkeypatch.setattr(cpgw_keeper, "KEEPER_STATE_FILE", env.state_path)
    monkeypatch.setattr(cpgw_keeper, "Storage", FakeStorage)
    assert cpgw_keeper.main(["--reset-lockout"]) == 0
    assert env.state().locked_out_reason is None
    assert "lockout" in capsys.readouterr().out


def test_non_paper_account_logs_out_locks_out_and_alerts(env):
    env.client.accounts = [{"id": "U7654321"}]
    keeper = env.keeper()
    keeper.tick()
    assert "logout" in env.client.calls
    assert env.state().locked_out_reason
    assert not env.session_file.exists()
    assert any("not a paper account" in n["text"] for n in env.notifier.sent)
    keeper.tick()
    assert env.login_calls == 1


def test_mixed_accounts_fail_closed(env):
    env.client.accounts = [{"id": "DU1234567"}, {"id": "U7654321"}]
    keeper = env.keeper()
    keeper.tick()
    assert "logout" in env.client.calls
    assert env.state().locked_out_reason


def test_empty_account_list_fails_closed(env):
    env.client.accounts = []
    keeper = env.keeper()
    keeper.tick()
    assert "logout" in env.client.calls
    assert env.state().locked_out_reason


def test_resumed_non_paper_session_is_logged_out(env):
    env.client.authenticated = True
    env.client.accounts = [{"id": "U7654321"}]
    keeper = env.keeper()
    keeper.start()
    keeper.tick()
    assert "logout" in env.client.calls
    assert env.state().locked_out_reason
    assert env.login_calls == 0


def test_login_is_skipped_when_gateway_turns_out_authenticated(env):
    keeper = env.keeper()
    real = env.client.tickle

    def tickle():
        try:
            return real()
        finally:
            env.client.authenticated = True  # becomes authenticated before the recheck

    env.client.tickle = tickle
    keeper.tick()
    assert env.login_calls == 0


def test_login_inside_nightly_gap_waits(env):
    env.clock.now = datetime(2025, 6, 10, 1, 0, tzinfo=NY)
    keeper = env.keeper()
    keeper.tick()
    assert env.login_calls == 0
    assert any("nightly gap" in n["text"] for n in env.notifier.sent)
    env.clock.now = datetime(2025, 6, 10, 1, 30, tzinfo=NY)
    keeper.tick()
    assert env.login_calls == 1


def test_non_fatal_failure_backs_off_then_retries(env):
    env.login_script = [GatewayUnreachable("down")]
    keeper = env.keeper()
    keeper.tick()
    assert env.login_calls == 1
    assert env.state().locked_out_reason is None
    env.clock.advance(seconds=30)
    keeper.tick()
    assert env.login_calls == 1  # inside the 60 s backoff
    env.clock.advance(seconds=31)
    keeper.tick()
    assert env.login_calls == 2


def test_manual_step_locks_out(env):
    env.login_script = [ManualStepRequired("page")]
    env.keeper().tick()
    assert env.state().locked_out_reason


def test_stub_login_locks_out_instead_of_raising(env):
    def stub(creds, *args, **kwargs):
        raise NotImplementedError("Automated IBKR login is not part of the public version.")

    env.login = stub
    env.keeper().tick()  # must not raise
    assert env.state().locked_out_reason


def test_corrupt_state_file_keeps_lockout_and_warns(env):
    env.state_path.write_text("{not json")
    keeper = env.keeper()
    keeper.start()
    keeper.tick()
    assert env.login_calls == 0
    assert any(level == "warn" for level, _, _ in env.storage.events)
    assert any("reset-lockout" in n["text"] for n in env.notifier.sent)


# ---- gateway down ----


def test_gateway_down_never_logs_in_and_alerts_after_threshold_then_cooldown(env):
    env.client.down = True
    keeper = env.keeper()
    keeper.tick()
    assert env.login_calls == 0
    assert not env.notifier.sent
    env.clock.advance(seconds=299)
    keeper.tick()
    assert not env.notifier.sent
    env.clock.advance(seconds=2)
    keeper.tick()
    down = [n for n in env.notifier.sent if "gateway" in n["text"]]
    assert len(down) == 1
    assert down[0]["key"] == "gateway_down"
    assert down[0]["cooldown"] == KeeperConfig().session_lost_alert_cooldown_sec
    env.clock.advance(seconds=600)
    keeper.tick()
    assert len([n for n in env.notifier.sent if "gateway" in n["text"]]) == 1
    env.clock.advance(seconds=3600)
    keeper.tick()
    assert len([n for n in env.notifier.sent if "gateway" in n["text"]]) == 2
    assert env.login_calls == 0


def test_gateway_recovery_resets_down_timer(env):
    env.client.down = True
    keeper = env.keeper()
    keeper.tick()
    env.client.down = False
    env.client.authenticated = True
    env.clock.advance(seconds=200)
    keeper.tick()
    env.client.down = True
    env.clock.advance(seconds=200)
    keeper.tick()  # only 0 s into the new outage
    env.clock.advance(seconds=200)
    keeper.tick()
    assert not [n for n in env.notifier.sent if "gateway" in n["text"]]


# ---- secrets ----


def test_scripted_failure_run_leaks_nothing(env, capsys):
    class Boom(Exception):
        pass

    env.login_script = [
        GatewayUnreachable(f"cannot reach {FAKE_USER} {FAKE_PASSWORD}"),
        BadCredentials(f"bad {FAKE_PASSWORD}"),
    ]
    keeper = env.keeper()
    keeper.start()
    keeper.tick()
    env.clock.advance(seconds=120)
    keeper.tick()
    keeper.tick()
    cap = capsys.readouterr()
    assert_no_secret([cap.out, cap.err] + env.all_text())
    assert env.state().locked_out_reason


# ---- --once and imports ----


def test_once_path_runs_one_tick(env):
    env.client.authenticated = True
    keeper = env.keeper(once=True)
    keeper.start()
    keeper.loop()
    assert env.login_calls == 0
    assert env.client.calls.count("tickle") >= 1


def test_read_session_still_exported():
    assert callable(cpgw_keeper.read_session)
    assert cpgw_keeper.SESSION_FILE.name == "cpgw_session.json"


def test_import_does_not_import_playwright():
    code = "import sys, live.cpgw_keeper; assert 'playwright' not in sys.modules"
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

import os
import sys
import traceback
from pathlib import Path

import pytest

from core import secret_store
from core.auth_types import IbkrCredentials
from core.secret_store import (
    Redactor,
    SecretFileError,
    check_secret_file,
    load_login_secrets,
    resolve_login_secret_path,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")

USER = "throwaway_user_x1"
PASSWORD = "throwaway-pw-Zq83!"


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    os.chmod(path, mode)
    return path


def _good(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "ibkr_login",
        f"# comment\n\nIBKR_USERNAME={USER}\nIBKR_PASSWORD={PASSWORD}\n",
    )


def _assert_no_leak(exc: BaseException, *values: str) -> None:
    blobs = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    for blob in blobs:
        for value in values:
            assert value not in blob


def test_mode_600_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER", "true")
    creds = load_login_secrets(_good(tmp_path))
    assert isinstance(creds, IbkrCredentials)
    assert creds.username == USER
    assert creds.password == PASSWORD
    assert creds.is_paper is True


def test_is_paper_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER", "false")
    assert load_login_secrets(_good(tmp_path)).is_paper is False


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o666])
def test_group_or_other_bits_refused(tmp_path, mode):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\nIBKR_PASSWORD={PASSWORD}\n", mode)
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, USER, PASSWORD)


def test_symlink_refused(tmp_path):
    target = _good(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SecretFileError):
        check_secret_file(link)


def test_directory_refused(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o700)
    with pytest.raises(SecretFileError):
        check_secret_file(d)


def test_missing_refused(tmp_path):
    with pytest.raises(SecretFileError):
        check_secret_file(tmp_path / "nope")


def test_oversize_refused(tmp_path):
    path = _write(tmp_path / "big", "# " + "x" * 5000 + "\n")
    assert path.stat().st_size > 4096
    with pytest.raises(SecretFileError):
        check_secret_file(path)


def test_wrong_owner_refused(tmp_path, monkeypatch):
    path = _good(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, USER, PASSWORD)


def test_unknown_key_refused_without_leak(tmp_path):
    path = _write(
        tmp_path / "f",
        f"IBKR_USERNAME={USER}\nIBKR_PASSWORD={PASSWORD}\nEXTRA_KEY_NAME=sekret-extra\n",
    )
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, USER, PASSWORD, "sekret-extra")
    assert "line 3" in str(info.value)


def test_line_without_equals_does_not_leak(tmp_path):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\n{PASSWORD}\n")
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, USER, PASSWORD)


def test_missing_key_refused(tmp_path):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\n")
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    assert "IBKR_PASSWORD" in str(info.value)
    _assert_no_leak(info.value, USER)


def test_empty_value_refused(tmp_path):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\nIBKR_PASSWORD=\n")
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    assert "line 2" in str(info.value)
    _assert_no_leak(info.value, USER)


@pytest.mark.parametrize(
    "line",
    ['IBKR_PASSWORD="pw-quoted-1"', "export IBKR_PASSWORD=pw-export-1", "IBKR_PASSWORD='pw-q-2'"],
)
def test_quotes_and_export_refused(tmp_path, line):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\n{line}\n")
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, "pw-quoted-1", "pw-export-1", "pw-q-2", USER)


def test_duplicate_key_refused(tmp_path):
    path = _write(
        tmp_path / "f",
        f"IBKR_USERNAME={USER}\nIBKR_USERNAME={USER}\nIBKR_PASSWORD={PASSWORD}\n",
    )
    with pytest.raises(SecretFileError) as info:
        load_login_secrets(path)
    _assert_no_leak(info.value, USER, PASSWORD)


def test_value_may_contain_equals_and_hash(tmp_path):
    path = _write(tmp_path / "f", f"IBKR_USERNAME={USER}\nIBKR_PASSWORD=a=b#c\n")
    assert load_login_secrets(path).password == "a=b#c"


def test_path_resolution_env_first(tmp_path, monkeypatch):
    run = _good(tmp_path)
    monkeypatch.setattr(secret_store, "RUN_SECRET_PATH", run)
    monkeypatch.setenv("IBKR_LOGIN_SECRET_FILE", str(tmp_path / "from_env"))
    assert resolve_login_secret_path() == tmp_path / "from_env"


def test_path_resolution_run_secrets(tmp_path, monkeypatch):
    run = _good(tmp_path)
    monkeypatch.delenv("IBKR_LOGIN_SECRET_FILE", raising=False)
    monkeypatch.setattr(secret_store, "RUN_SECRET_PATH", run)
    assert resolve_login_secret_path() == run


def test_path_resolution_repo_default(tmp_path, monkeypatch):
    monkeypatch.delenv("IBKR_LOGIN_SECRET_FILE", raising=False)
    monkeypatch.setattr(secret_store, "RUN_SECRET_PATH", tmp_path / "absent")
    assert resolve_login_secret_path() == secret_store.ROOT / "secrets" / "ibkr_login"


def test_redactor_masks_registered_values():
    red = Redactor([PASSWORD, USER])
    out = red.redact(f"login {USER} with {PASSWORD} failed")
    assert PASSWORD not in out
    assert USER not in out
    assert out.count("***") == 2


def test_redactor_add_and_ignores_empty():
    red = Redactor([""])
    assert red.redact("plain text") == "plain text"
    red.add("tok-123456")
    red.add("")
    assert red.redact("x tok-123456 y") == "x *** y"


def test_redactor_longest_first():
    red = Redactor(["abc", "abcdef"])
    assert red.redact("abcdef") == "***"


def test_redactor_masks_telegram_url_token():
    text = "POST https://api.telegram.org/bot123456789:AAE-abc_DEF123xyz/sendMessage failed"
    out = Redactor().redact(text)
    assert "AAE-abc_DEF123xyz" not in out
    assert "123456789:" not in out
    assert "/sendMessage" in out


def test_redactor_masks_cookie_pairs():
    red = Redactor()
    assert "abc123" not in red.redact("got cookie=abc123; path=/")
    assert "abc123" not in red.redact("Cookie: x-sess-uuid=abc123; other=1")
    assert "abc123" not in red.redact("Set-Cookie: sid=abc123; HttpOnly")


def test_redactor_leaves_other_text_alone():
    text = "cycle done: 3 orders, equity 100000.5, path=/v1/api"
    assert Redactor([PASSWORD]).redact(text) == text

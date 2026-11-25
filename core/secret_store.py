"""Load IBKR login credentials from a chmod 600 file and redact secrets from text.

Error messages name the file, the line number and a known key. They never contain a
value, and never echo an unknown key or an unparsable line (it could be a pasted secret).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from core.auth_types import IbkrCredentials

ROOT = Path(__file__).resolve().parent.parent
RUN_SECRET_PATH = Path("/run/secrets/ibkr_login")
MAX_SECRET_BYTES = 4096
ALLOWED_KEYS = ("IBKR_USERNAME", "IBKR_PASSWORD")


class SecretFileError(RuntimeError):
    """A secret file is missing, unsafe or malformed. The text never holds a value."""


def resolve_login_secret_path() -> Path:
    env = os.environ.get("IBKR_LOGIN_SECRET_FILE", "").strip()
    if env:
        return Path(env)
    if RUN_SECRET_PATH.exists():
        return RUN_SECRET_PATH
    return ROOT / "secrets" / "ibkr_login"


def _check_stat(path: Path, st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError(f"secret file {path} is not a regular file")
    if st.st_size > MAX_SECRET_BYTES:
        raise SecretFileError(f"secret file {path} is larger than {MAX_SECRET_BYTES} bytes")
    if st.st_mode & 0o077:
        raise SecretFileError(
            f"secret file {path} has group or other permissions "
            f"({stat.S_IMODE(st.st_mode):04o}); run chmod 600 on it"
        )
    if st.st_uid != os.geteuid():
        raise SecretFileError(f"secret file {path} is owned by uid {st.st_uid}, not {os.geteuid()}")


def check_secret_file(path: Path) -> None:
    """Refuse unsafe secret files. Skipped on Windows (the keeper only runs on Linux)."""
    if os.name == "nt":
        return
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SecretFileError(f"secret file {path} does not exist") from None
    except OSError as exc:
        raise SecretFileError(f"secret file {path} cannot be read ({type(exc).__name__})") from None
    if stat.S_ISLNK(st.st_mode):
        raise SecretFileError(f"secret file {path} is a symlink")
    _check_stat(path, st)


def _read_checked(path: Path) -> str:
    check_secret_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SecretFileError(
            f"secret file {path} cannot be opened ({type(exc).__name__})"
        ) from None
    try:
        if os.name != "nt":
            _check_stat(path, os.fstat(fd))
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_SECRET_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretFileError(f"secret file {path} is not valid UTF-8") from None


def _parse(path: Path, text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in ALLOWED_KEYS:
            raise SecretFileError(
                f"{path} line {number}: expected IBKR_USERNAME=... or IBKR_PASSWORD=..."
            )
        if key in found:
            raise SecretFileError(f"{path} line {number}: duplicate key {key}")
        if not value:
            raise SecretFileError(f"{path} line {number}: empty value for {key}")
        if value[0] in "\"'":
            raise SecretFileError(f"{path} line {number}: {key} must not be quoted")
        found[key] = value
    for key in ALLOWED_KEYS:
        if key not in found:
            raise SecretFileError(f"{path}: missing key {key}")
    return found


def load_login_secrets(path: Path | None = None) -> IbkrCredentials:
    path = Path(path) if path is not None else resolve_login_secret_path()
    values = _parse(path, _read_checked(path))
    is_paper = os.environ.get("PAPER", "true").strip().lower() in ("1", "true", "yes")
    return IbkrCredentials(
        username=values["IBKR_USERNAME"],
        password=values["IBKR_PASSWORD"],
        is_paper=is_paper,
    )


_TELEGRAM_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_COOKIE_HEADER = re.compile(r"(?i)\b((?:set-)?cookie)\s*:\s*[^\r\n]*")
_COOKIE_PAIR = re.compile(r"(?i)\b(cookie)=[^;\s]+")


class Redactor:
    """Replace registered secret strings and known token patterns with `***`."""

    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._secrets: list[str] = []
        for value in secrets:
            self.add(value)

    def add(self, value: str | None) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        out = str(text)
        for value in self._secrets:
            out = out.replace(value, "***")
        out = _TELEGRAM_TOKEN.sub("bot***", out)
        out = _COOKIE_HEADER.sub(lambda m: f"{m.group(1)}: ***", out)
        out = _COOKIE_PAIR.sub(lambda m: f"{m.group(1)}=***", out)
        return out

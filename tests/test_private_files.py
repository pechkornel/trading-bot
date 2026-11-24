"""Private login module, secret files and vendored binaries must never be tracked."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")

PRIVATE_PATHS = (
    "core/ibkr_auth_private.py",
    "secrets/ibkr_login",
    "secrets/gateway_ssl_password",
    "vendor/ibkr-gateway/ibkr-gateway-linux-amd64.zip",
)
EXAMPLE_FILES = (
    "secrets/README.md",
    "secrets/ibkr_login.example",
    "secrets/gateway_ssl_password.example",
)
ALLOWED_TRACKED = re.compile(r"^(secrets/[^/]+\.example|secrets/README\.md)$")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def _ignore_lines(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def test_no_private_file_is_tracked():
    out = _git("ls-files", "--", "core/ibkr_auth_private.py", "secrets", "vendor").stdout
    tracked = [ln for ln in out.splitlines() if not ALLOWED_TRACKED.match(ln)]
    assert tracked == []


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_paths_are_git_ignored(path):
    assert _git("check-ignore", "-q", path).returncode == 0


@pytest.mark.parametrize("path", EXAMPLE_FILES)
def test_example_files_exist_and_are_not_ignored(path):
    assert (ROOT / path).is_file()
    assert _git("check-ignore", "-q", path).returncode == 1


def test_example_files_hold_only_placeholders():
    for path in ("secrets/ibkr_login.example", "secrets/gateway_ssl_password.example"):
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                value = line.split("=", 1)[1]
                assert value.startswith("<") and value.endswith(">"), (path, line)
            else:
                assert line.startswith("<") and line.endswith(">"), path
    assert not re.search(
        r"[0-9a-f]{48}", (ROOT / "secrets/gateway_ssl_password.example").read_text()
    )


def test_dockerignore_excludes_secrets_and_vendor():
    lines = _ignore_lines(".dockerignore")
    assert "secrets/" in lines
    assert "vendor/" in lines
    assert "core/ibkr_auth_private.py" not in lines


def test_ci_checks_for_tracked_private_files():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Private files are not tracked" in ci
    assert "git ls-files -- core/ibkr_auth_private.py secrets vendor" in ci


@pytest.mark.parametrize(
    "path",
    [
        ".env.local",
        ".env.production",
        "certs/server.key",
        "config/gateway_cert.pem",
        ".claude/settings.local.json",
    ],
)
def test_env_variants_keys_certificates_and_claude_dir_are_git_ignored(path):
    assert _git("check-ignore", "-q", path).returncode == 0


@pytest.mark.parametrize("path", [".env.example", "tests/fixtures/gateway_cert.pem"])
def test_env_example_and_the_test_certificate_are_not_ignored(path):
    assert _git("check-ignore", "-q", path).returncode == 1


def test_nothing_is_tracked_under_claude_so_ignoring_it_is_safe():
    assert _git("ls-files", "--", ".claude").stdout.strip() == ""


def test_dockerignore_excludes_env_variants_keys_and_certificates():
    lines = _ignore_lines(".dockerignore")
    for expected in (
        ".env.*",
        "!.env.example",
        "*.key",
        "*.pem",
        "!tests/fixtures/*.pem",
        ".claude/",
    ):
        assert expected in lines, expected
    assert lines.index("!.env.example") > lines.index(".env.*")


def test_ci_fails_on_tracked_env_variants_and_keys():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "git ls-files -- '.env' '.env.*' '*.key'" in ci
    assert "grep -Fxv '.env.example'" in ci

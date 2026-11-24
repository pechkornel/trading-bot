"""Offline checks of the deploy files. They pin the safety properties."""

from pathlib import Path

import yaml

from core.config import ROOT

HOST_DEFAULT = "${DASHBOARD_HOST:-127.0.0.1}"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_read("docker-compose.yml"))


def test_dashboard_command_binds_to_dashboard_host():
    command = _compose()["services"]["dashboard"]["command"]
    assert command[command.index("--host") + 1] == HOST_DEFAULT


def test_dashboard_healthcheck_stays_on_loopback():
    test = _compose()["services"]["dashboard"]["healthcheck"]["test"]
    assert "http://127.0.0.1:8765/api/health" in " ".join(test)


def test_dockerfile_cmd_uses_dashboard_host():
    cmd_lines = [line for line in _read("docker/Dockerfile").splitlines() if line.startswith("CMD")]
    assert len(cmd_lines) == 1
    assert HOST_DEFAULT in cmd_lines[0]


def test_systemd_dashboard_unit_uses_dashboard_host():
    unit = _read("deploy/systemd/tradingbot-dashboard.service")
    assert "Environment=DASHBOARD_HOST=127.0.0.1" in unit
    assert "--host ${DASHBOARD_HOST}" in unit


def test_no_wildcard_bind_in_deploy_files():
    paths = [
        ROOT / "docker-compose.yml",
        ROOT / "docker" / "Dockerfile",
        *sorted((ROOT / "deploy" / "systemd").glob("*.service")),
    ]
    for path in paths:
        assert "0.0.0.0" not in Path(path).read_text(encoding="utf-8"), path


def test_portfolio_command_is_dry_run():
    command = _compose()["services"]["portfolio"]["command"]
    assert command == ["python", "-m", "live.portfolio_runner"]


def test_env_example_safe_defaults():
    lines = _read(".env.example").splitlines()
    for expected in ("PAPER=true", "DRY_RUN=true", "IB_PORT=5000", "DASHBOARD_HOST=127.0.0.1"):
        assert expected in lines

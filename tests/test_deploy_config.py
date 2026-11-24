"""Offline checks of the deploy files. They pin the safety properties."""

from fnmatch import fnmatch
from pathlib import Path

import yaml

from core.config import PAPER_PORTS, ROOT

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
    # exec makes uvicorn PID 1, so it receives SIGTERM from docker stop.
    assert cmd_lines[0].startswith('CMD ["sh", "-c", "exec uvicorn ')


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


GATEWAY_SHA256 = "01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d"
BOT_SERVICES = ("dashboard", "cpgw_keeper", "portfolio", "sentiment")


def _gateway() -> dict:
    return _compose()["services"]["ibkr_gateway"]


def _gateway_conf() -> dict:
    return yaml.safe_load(_read("docker/gateway/conf.yaml"))


def _subnet() -> str:
    return _compose()["networks"]["ibkr_gateway"]["ipam"]["config"][0]["subnet"]


def test_gateway_service_is_loopback_only():
    gateway = _gateway()
    assert gateway["ports"] == ["127.0.0.1:5000:5000"]
    assert "network_mode" not in gateway
    assert gateway["networks"] == ["ibkr_gateway"]
    assert gateway["profiles"] == ["gateway"]
    assert gateway["restart"] == "unless-stopped"


def test_gateway_is_locked_down():
    gateway = _gateway()
    assert "ALL" in gateway["cap_drop"]
    assert "no-new-privileges:true" in gateway["security_opt"]
    conf_mounts = [v for v in gateway["volumes"] if v.endswith("/root/conf.yaml:ro")]
    assert len(conf_mounts) == 1
    assert conf_mounts[0].startswith("./docker/gateway/conf.yaml:")


def test_no_service_publishes_a_port_beyond_loopback():
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_bot_services_keep_host_network():
    services = _compose()["services"]
    for name in BOT_SERVICES:
        assert services[name]["network_mode"] == "host", name


def test_gateway_network_subnet_is_pinned():
    assert _subnet() == "172.31.250.0/24"


def test_gateway_conf_listens_on_the_paper_port_with_ssl():
    conf = _gateway_conf()
    assert conf["listenPort"] == 5000
    assert conf["listenPort"] in PAPER_PORTS
    assert conf["listenSsl"] is True


def test_gateway_conf_allow_list_is_loopback_and_bridge_only():
    allow = _gateway_conf()["ips"]["allow"]
    assert allow == ["127.0.0.1", "172.31.250.*"]
    for entry in allow:
        assert not str(entry).startswith("192.")
        assert entry != "*"
        assert "0.0.0.0" not in entry


def test_gateway_allow_pattern_matches_the_compose_subnet():
    prefix = _subnet().split("/")[0].rsplit(".", 1)[0]
    assert f"{prefix}.*" in _gateway_conf()["ips"]["allow"]


def test_gateway_dockerfile_pins_release_and_hash():
    text = _read("docker/gateway/Dockerfile")
    assert "GATEWAY_VERSION=v1.0.0" in text
    assert "GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip" in text
    assert GATEWAY_SHA256 in text
    assert "sha256sum -c" in text
    assert any(line.startswith("USER ") for line in text.splitlines())


def _ignore_lines(rel: str) -> list[str]:
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_gitignore_covers_the_gateway_session_file():
    # logs/cpgw_session.json holds the gateway session cookie.
    assert "logs/*.json" in _ignore_lines(".gitignore")
    assert fnmatch("logs/cpgw_session.json", "logs/*.json")


def test_dockerignore_excludes_secrets_and_bind_mounted_models():
    lines = _ignore_lines(".dockerignore")
    for expected in (
        ".env",
        "logs/",
        "models/artifacts/*/model.pkl",
        "models/artifacts/*/model.keras",
        "models/artifacts/*/scaler.gz",
    ):
        assert expected in lines, expected


def test_dockerignore_keeps_the_private_auth_module():
    # Decision D4: the private login module must be copied into the image.
    lines = _ignore_lines(".dockerignore")
    assert not any("ibkr_auth_private" in line or line == "core/" for line in lines)
    assert not any(fnmatch("core/ibkr_auth_private.py", line) for line in lines)

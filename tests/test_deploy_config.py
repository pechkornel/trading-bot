"""Offline checks of the deploy files. They pin the safety properties."""

import ipaddress
import re
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
    conf_mounts = [v for v in gateway["volumes"] if v.endswith("/conf.template.yaml:ro")]
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
    assert allow == ["127.0.0.1", "172.31.250.1"]
    for entry in allow:
        assert not str(entry).startswith("192.")
        assert entry != "*"
        assert "0.0.0.0" not in entry


def test_gateway_allow_pattern_matches_the_compose_subnet():
    network = ipaddress.ip_network(_subnet())
    bridge = [e for e in _gateway_conf()["ips"]["allow"] if e != "127.0.0.1"]
    assert bridge == ["172.31.250.1"]
    assert ipaddress.ip_address(bridge[0]) in network


def test_gateway_hash_file_matches_the_pinned_release():
    text = _read("docker/gateway/gateway.sha256").strip()
    assert text == f"{GATEWAY_SHA256}  ibkr-gateway-linux-amd64.zip"


def test_gateway_dockerfile_copies_vendored_zip_and_verifies_it():
    text = _read("docker/gateway/Dockerfile")
    assert "GATEWAY_VERSION=v1.0.0" in text
    assert "GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip" in text
    assert "curl -fsSL" not in text
    assert "releases/download" not in text
    assert "COPY --from=vendor" in text
    assert "sha256sum -c gateway.sha256" in text
    assert "rm -f" in text and "vertx.jks" in text
    assert "openssl" in text
    assert 'ENTRYPOINT ["./entrypoint.sh"]' in text
    assert any(line.startswith("USER ") for line in text.splitlines())


def test_gateway_compose_build_uses_vendor_context():
    build = _gateway()["build"]
    assert build["context"] == "docker/gateway"
    assert build["additional_contexts"] == {"vendor": "./vendor/ibkr-gateway"}


def test_gateway_compose_mounts_secret_tmpfs_and_tls_volume():
    gateway = _gateway()
    assert gateway["secrets"] == ["gateway_ssl_password"]
    assert any(t.startswith("/opt/ibkr-gateway/run:") for t in gateway["tmpfs"])
    assert "ibkr_gateway_tls:/opt/ibkr-gateway/tls" in gateway["volumes"]
    compose = _compose()
    assert compose["secrets"]["gateway_ssl_password"]["file"] == "./secrets/gateway_ssl_password"
    assert "ibkr_gateway_tls" in compose["volumes"]


def test_gateway_conf_takes_ssl_password_from_a_placeholder():
    assert _gateway_conf()["sslPwd"] == "__SSL_PWD__"
    assert "mywebapi" not in _read("docker/gateway/conf.yaml")


def test_fetch_script_is_strict_and_pinned():
    text = _read("scripts/fetch_gateway.sh")
    assert "set -euo pipefail" in text
    assert "docker/gateway/gateway.sha256" in text
    assert "vendor/ibkr-gateway" in text
    assert "unzip" not in text
    assert "ibkr-gateway-linux-amd64.zip" in text
    assert "rm -f" in text


def _entrypoint_code() -> str:
    lines = _read("docker/gateway/entrypoint.sh").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def test_entrypoint_never_leaks_the_password():
    code = _entrypoint_code()
    assert "set -eu" in code
    assert "set -x" not in code and "xtrace" not in code
    assert "-passin file:" in code and "-passout file:" in code
    assert "-passin pass:" not in code and "-passout pass:" not in code
    assert "[!0-9A-Za-z]" in code
    assert not re.search(r"(echo|printf)[^\n]*\$\{?(PW|PASSWORD|SSL_PWD)", code)


def test_entrypoint_certificate_covers_localhost_and_loopback_ip():
    code = _entrypoint_code()
    assert "subjectAltName=DNS:localhost,IP:127.0.0.1" in code
    assert "extendedKeyUsage=serverAuth" in code
    assert "rsa:3072" in code


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


def test_gateway_dockerfile_pins_every_base_image_by_digest():
    froms = [
        line for line in _read("docker/gateway/Dockerfile").splitlines() if line.startswith("FROM ")
    ]
    assert len(froms) == 2
    for line in froms:
        assert re.search(r"^FROM ubuntu:24\.04@sha256:[0-9a-f]{64}( AS \w+)?$", line), line


MEM_LIMITS = {
    "ibkr_gateway": "768m",
    "cpgw_keeper": "640m",
    "portfolio": "640m",
    "dashboard": "512m",
    "sentiment": "256m",
}
CA_PATH = "/app/gateway_tls/cert.pem"
TLS_MOUNT = "ibkr_gateway_tls:/app/gateway_tls:ro"


def _mb(value: str) -> int:
    assert value.endswith("m"), value
    return int(value[:-1])


def test_every_service_has_the_planned_memory_limit_and_no_swap():
    services = _compose()["services"]
    assert set(services) == set(MEM_LIMITS)
    for name, limit in MEM_LIMITS.items():
        assert services[name]["mem_limit"] == limit, name
        assert services[name]["memswap_limit"] == limit, name


def test_memory_total_of_auto_profile_plus_gateway_fits_the_budget():
    services = _compose()["services"]
    names = ["dashboard"] + [
        n for n, s in services.items() if "auto" in s.get("profiles", []) or n == "ibkr_gateway"
    ]
    assert sum(_mb(services[n]["mem_limit"]) for n in names) <= 2560


def test_sentiment_moved_to_the_research_profile():
    assert _compose()["services"]["sentiment"]["profiles"] == ["research"]


def test_only_the_keeper_has_the_login_secret_and_keeper_target():
    compose = _compose()
    assert compose["secrets"]["ibkr_login"]["file"] == "./secrets/ibkr_login"
    for name, service in compose["services"].items():
        has_secret = "ibkr_login" in service.get("secrets", [])
        assert has_secret == (name == "cpgw_keeper"), name
        target = service.get("build", {}).get("target")
        if name == "cpgw_keeper":
            assert target == "keeper"
            assert service["image"] == "tradingbot-keeper:local"
        elif name != "ibkr_gateway":
            assert target == "app", name


def test_keeper_runs_as_uid_1000_to_own_the_secret_file():
    # core/secret_store.py wants mode 600 and the reading uid as owner. A compose
    # file secret keeps the host owner, so the container uid must match it.
    assert str(_compose()["services"]["cpgw_keeper"]["user"]) == "1000:1000"
    assert re.search(r"useradd[^\n]*--uid 1000 bot", _read("docker/Dockerfile"))
    assert "uid 1000" in _read("docker-compose.yml")


def test_gateway_certificate_reaches_keeper_and_portfolio_read_only():
    services = _compose()["services"]
    for name in ("cpgw_keeper", "portfolio"):
        assert TLS_MOUNT in services[name]["volumes"], name
        assert f"CPGW_CA_FILE={CA_PATH}" in services[name]["environment"], name
    assert not any("gateway_tls" in v for v in services["dashboard"]["volumes"])


def _dockerfile_stages() -> list[str]:
    return re.findall(r"^FROM .+ AS (\w+)$", _read("docker/Dockerfile"), flags=re.MULTILINE)


def test_dockerfile_has_three_named_stages_with_app_last():
    assert _dockerfile_stages() == ["deps", "keeper", "app"]


def test_keeper_stage_installs_headless_shell_only():
    text = _read("docker/Dockerfile")
    keeper = text.split(" AS keeper", 1)[1].split(" AS app", 1)[0]
    assert "requirements-login.txt" in keeper
    assert "--only-shell chromium" in keeper
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in keeper
    assert "playwright" not in text.split(" AS app", 1)[1].lower()


def test_no_deploy_file_mentions_live_wildcard_password_or_xvfb():
    for rel in ("docker-compose.yml", "docker/Dockerfile"):
        text = _read(rel).lower()
        for banned in ("--live", "0.0.0.0", "ibkr_password", "xvfb"):
            assert banned not in text, (rel, banned)


def test_deploy_files_have_no_em_or_en_dashes():
    for rel in ("docker-compose.yml", "docker/Dockerfile", ".env.example"):
        text = _read(rel)
        assert "—" not in text and "–" not in text, rel


def test_env_example_has_no_password_and_lists_the_new_variables():
    text = _read(".env.example")
    assert "IBKR_PASSWORD" not in text and "IBKR_USERNAME" not in text
    lines = text.splitlines()
    for expected in (
        "TELEGRAM_BOT_TOKEN=",
        "TELEGRAM_CHAT_ID=",
        "IBKR_LOGIN_SECRET_FILE=",
        "CPGW_CA_FILE=",
    ):
        assert expected in lines


def _phase1_section() -> str:
    text = _read("deploy/README.md")
    match = re.search(r"^## [^\n]*Phase 1 upgrade \(paper account only\)[^\n]*$", text, re.M)
    assert match, "deploy/README.md has no Phase 1 upgrade section"
    rest = text[match.end() :]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_phase1_section_starts_with_a_backup_and_never_uses_live():
    section = _phase1_section()
    first_compose = section.find("docker compose")
    assert first_compose != -1
    backup = section.find("deploy/backup.sh")
    assert backup != -1 and backup < first_compose
    assert "--live" not in section
    assert "uv run" not in section and "uv sync" not in section
    assert "—" not in section and "–" not in section


def test_keeper_drops_capabilities_like_the_gateway():
    services = _compose()["services"]
    keeper = services["cpgw_keeper"]
    assert keeper["cap_drop"] == ["ALL"]
    assert keeper["security_opt"] == ["no-new-privileges:true"]
    assert keeper["cap_drop"] == services["ibkr_gateway"]["cap_drop"]
    assert keeper["security_opt"] == services["ibkr_gateway"]["security_opt"]


def test_fetch_gateway_curl_forces_https_and_tls12():
    curl_lines = [
        line for line in _read("scripts/fetch_gateway.sh").splitlines() if line.startswith("curl ")
    ]
    assert curl_lines
    for line in curl_lines:
        assert "--proto '=https'" in line
        assert "--tlsv1.2" in line

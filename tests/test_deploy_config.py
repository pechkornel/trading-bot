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


# The native build ignores --conf and always uses the conf.yaml compiled into it
# (IBKR's default, ADR 0004). Its allow list, as found in the v1.0.0 binary:
BUILTIN_ALLOW = ("192.*", "131.216.*", "127.0.0.1")
BUILTIN_SSL_PWD = "mywebapi"


def _allowed_by_builtin_list(address: str) -> bool:
    return any(fnmatch(address, pattern) for pattern in BUILTIN_ALLOW)


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


def test_gateway_mounts_no_config_file():
    # The binary would ignore it, so a mounted conf.yaml only misleads (ADR 0004).
    assert not any("conf" in str(v) for v in _gateway()["volumes"])
    assert not (ROOT / "docker/gateway/conf.yaml").exists()


def test_no_service_publishes_a_port_beyond_loopback():
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_bot_services_keep_host_network():
    services = _compose()["services"]
    for name in BOT_SERVICES:
        assert services[name]["network_mode"] == "host", name


def test_gateway_network_subnet_is_pinned():
    assert _subnet() == "192.168.250.0/24"


def test_gateway_bridge_address_is_admitted_by_the_builtin_allow_list():
    # Docker's proxy delivers every connection to the published port from the bridge
    # gateway, the first host of the subnet. The built-in list must admit it, or every
    # request gets "Access Denied" (seen on the server with 172.31.250.0/24).
    network = ipaddress.ip_network(_subnet())
    assert network.is_private
    bridge = str(next(network.hosts()))
    assert bridge == "192.168.250.1"
    assert _allowed_by_builtin_list(bridge)
    assert not _allowed_by_builtin_list("172.31.250.1")


def test_gateway_listens_on_the_paper_port():
    assert _gateway()["ports"] == ["127.0.0.1:5000:5000"]
    assert 5000 in PAPER_PORTS


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


def test_entrypoint_starts_the_gateway_without_a_config_flag():
    # --conf makes the native build look for a compiled-in resource and exit with
    # "Failed to load configuration file ... Stream closed" (seen on the server).
    code = _entrypoint_code()
    assert "--conf" not in code
    assert "conf.template.yaml" not in code
    assert "exec ./ibkr-gateway" in code


def test_entrypoint_gives_the_gateway_our_key_under_the_builtin_password():
    code = _entrypoint_code()
    # The built-in sslCert is "vertx.jks" in the working directory /opt/ibkr-gateway.
    assert 'mv "$BASE/vertx.jks.new" "$BASE/vertx.jks"' in code
    assert f"-passout pass:{BUILTIN_SSL_PWD}" in code
    # Our keystore in the volume stays protected by the secret.
    assert '-in "$TLS/keystore.p12" -passin file:"$PWD_FILE"' in code


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
    assert "-passin pass:" not in code
    # The only literal password is the public built-in one, never the secret.
    assert re.findall(r"-passout pass:(\S+)", code) == [BUILTIN_SSL_PWD]
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
    assert not any("gateway_tls" in v for v in services["dashboard"]["volumes"])


def test_compose_does_not_force_a_ca_file_on_every_gateway():
    # A hard-coded CPGW_CA_FILE crash-loops the keeper and the portfolio runner
    # with the host Java gateway, which writes no /app/gateway_tls/cert.pem.
    # The path comes from .env (env_file), set only for the native gateway.
    compose = _compose()
    for name, service in compose["services"].items():
        env = service.get("environment", [])
        assert not any("CPGW_CA_FILE" in str(item) for item in env), name
    assert ".env" in compose["x-bot-common"]["env_file"]


def test_env_example_leaves_the_ca_file_empty_and_documents_the_native_path():
    text = _read(".env.example")
    assert "CPGW_CA_FILE=" in text.splitlines()
    assert CA_PATH in text


def test_readme_documents_exporting_the_java_gateway_certificate():
    text = _read("deploy/README.md")
    assert "openssl s_client -connect 127.0.0.1:5000" in text
    assert "subjectAltName" in text or "Subject Alternative Name" in text
    assert f"CPGW_CA_FILE={CA_PATH}" in text


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


LOGIN_IN_ENV = re.compile(r"^\s*(Environment=)?IBKR_(USERNAME|PASSWORD)\s*=", re.MULTILINE)


def _deploy_files() -> list[str]:
    units = sorted(p.name for p in (ROOT / "deploy" / "systemd").glob("*.service"))
    return ["deploy/README.md", "deploy/UBUNTU_SETUP.md", *[f"deploy/systemd/{u}" for u in units]]


def test_no_deploy_file_puts_the_login_in_env():
    # The login lives in secrets/ibkr_login. A sample .env line with it would
    # also trip the keeper's refusal to start while IBKR_PASSWORD is set.
    for rel in _deploy_files():
        assert not LOGIN_IN_ENV.search(_read(rel)), rel


def test_systemd_keeper_unit_uses_the_secret_file_and_the_env_file_for_the_ca():
    unit = _read("deploy/systemd/tradingbot-cpgw-keeper.service")
    assert "Environment=IBKR_LOGIN_SECRET_FILE=/opt/trading-bot/secrets/ibkr_login" in unit
    assert "EnvironmentFile=/opt/trading-bot/.env" in unit
    assert "IBKR_PASSWORD" not in unit


def test_ubuntu_setup_explains_the_keeper_login_prerequisites():
    text = _read("deploy/UBUNTU_SETUP.md")
    assert "secrets/ibkr_login" in text
    assert "chmod 600 secrets/ibkr_login" in text
    assert "requirements-login.txt" in text
    assert "playwright install --with-deps --only-shell chromium" in text
    assert "CPGW_CA_FILE" in text
    assert "xvfb" not in text.lower()


def _readme_section(heading_re: str) -> str:
    text = _read("deploy/README.md")
    match = re.search(heading_re, text, re.M)
    assert match, heading_re
    rest = text[match.end() :]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_readme_0b_step_references_point_to_the_right_headings():
    text = _read("deploy/README.md")
    steps = dict(
        (int(n), title)
        for n, title in re.findall(r"^### (\d+)\. ([^\n]+)$", _phase1_section_text(text), re.M)
    )
    for n, word in ((16, "Sentiment"), (15, "Lockout")):
        assert word in steps[n], (n, steps[n])
    for n in map(int, re.findall(r"0B step (\d+)", text)):
        assert n in steps
    assert "see section 0B step 16)" in text
    assert "section 0B step 15." in text


def test_readme_matches_the_builtin_gateway_config():
    text = _read("deploy/README.md")
    assert "--conf ../run/conf.yaml" not in text
    assert "docker/gateway/conf.yaml" not in text
    assert "172.31.250" not in text
    assert "192.168.250.1" in text


def _phase1_section_text(text: str) -> str:
    start = text.index("## 0B. Phase 1 upgrade")
    return text[start : text.index("\n## 1. Get the code", start)]


def test_both_install_paths_fetch_the_gateway_before_the_first_gateway_build():
    sections = {
        "0A": _readme_section(r"^## 0A\. Home server \(Docker\)$"),
        "0B": _phase1_section(),
    }
    for name, section in sections.items():
        fetch = section.find("scripts/fetch_gateway.sh")
        build = section.find("up -d --build ibkr_gateway")
        assert fetch != -1, name
        assert build != -1, name
        assert fetch < build, name
        # the password secret must also exist before the first gateway start
        assert "secrets/gateway_ssl_password" in section[:build], name

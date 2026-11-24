# Plan: home server prep (private auth loader, loopback dashboard, sentiment from .env, native gateway container)

Branch: `feat/home-server-prep` (from main 5c22406). Code changes are test-first: write the test, see it fail on current code, then change the code, then see it pass. Run `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .` and `.venv/bin/python -m pytest -q` before every commit (169 tests pass today). Do not use `uv run` or `uv sync`.

Hard limits for every task: `assert_paper_mode`, `PAPER_PORTS = {5000}`, the `DRY_RUN` default and `--live` handling do not change. The `portfolio` service stays dry run. No private auth code is committed. No agent deploys, connects to the home server, runs the gateway binary, or runs any ufw, iptables or tailscale command.

## Approval

Approved by the lead on 2026-11-24 while the user was away, with all Decisions and Open-question defaults as written. The user asked for unattended work within this scope; every choice below is listed in the PR.

## Decisions (taken by the architect, reversible, for the lead to confirm)

- D1. Private module location: `core/ibkr_auth_private.py`, git-ignored. `core/ibkr_auth.py` imports it with `importlib.import_module` and falls back to the stub only when that exact module is missing. If the private module exists but fails to import (for example a missing dependency), the error is raised, not hidden. Reversible: change the fallback rule in one `except` block.
- D2. The private module only has to define `login(creds, *, port, **kwargs) -> str` (the session cookie, as `live/cpgw_keeper.py` uses it today). If it also defines `IbkrCredentials`, `IBKRAuthError` or `MaxLoginAttempts`, those replace the public ones, so a copy of the original full module works as a drop-in. It may also import those names from `core.ibkr_auth`.
- D3. `core/ibkr_auth.py` exposes `AUTH_BACKEND` (`"stub"` or `"private"`), and the keeper banner prints `auth=<backend>`. It never prints the password (the banner already prints the username today, unchanged). This lets the user confirm on the server that the private login is active.
- D4. In Docker the private module is baked into the local image by the existing `COPY . /app` (the image is `tradingbot:local`, built on the server, never pushed). This also covers the dashboard container, which can spawn the keeper through `dashboard/process.py`. Rejected: a bind mount, because Docker creates a root-owned directory at the host path when the file is missing, which breaks the public default.
- D5. `DASHBOARD_HOST` is read by the launchers, not by Python: compose interpolation (`${DASHBOARD_HOST:-127.0.0.1}`, compose reads the project `.env`), a shell-form `CMD` in `docker/Dockerfile`, and `Environment=DASHBOARD_HOST=127.0.0.1` plus `--host ${DASHBOARD_HOST}` in the systemd dashboard unit (`EnvironmentFile=` overrides `Environment=`). The healthcheck stays on `http://127.0.0.1:8765/api/health`, which works for `127.0.0.1` and `0.0.0.0`. Docs say only those two values are supported, and `0.0.0.0` is not recommended.
- D6. `SENTIMENT_PROVIDER` overrides `provider` inside the `sentiment_provider:` block of `config/symbols.yaml` (loader: `core.config.load_sentiment_config`). Values are stripped and lowercased. Empty or unset means "use the YAML". Any value other than `stub` or `alpha_vantage` raises `ValueError` (the `alphavantage` alias that `get_provider` accepts is not accepted from `.env`). `load_sentiment_config` calls `load_dotenv(ROOT / ".env")` like `load_app_config` does, so local runs see `.env` too. `.env.example` ships `SENTIMENT_PROVIDER=` (empty, YAML wins).
- D7. Gateway runs as a Docker service `ibkr_gateway` (container, not systemd), under a new compose profile `gateway`, with `restart: unless-stopped` (starts on boot because the Docker service is enabled at boot). A separate profile keeps `docker compose --profile auto up` working for people who run the Java gateway on the host (deploy/UBUNTU_SETUP.md section 3), which would otherwise clash on port 5000.
- D8. Network design (no firewall rule needed): the gateway sits on its own bridge network `ibkr_gateway` with a pinned subnet `172.31.250.0/24` and publishes `127.0.0.1:5000:5000` only. The bot containers keep `network_mode: host` and reach `https://localhost:5000` through Docker's loopback-only proxy. The gateway then sees the source address `172.31.250.1` (the bridge gateway), so the mounted `conf.yaml` allows exactly `127.0.0.1` and `172.31.250.*`. It never allows `192.*` or `0.0.0.0`.
- D9. Pinned release: tag `v1.0.0`, asset `ibkr-gateway-linux-amd64.zip`, 22762329 bytes, SHA-256 `01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d`. The author publishes no checksum file; this digest is the one GitHub computed at upload (release API `digest` field). The implementer confirms it by downloading the asset into an empty scratch dir and hashing it, without unzipping into the repo or executing anything. The Dockerfile verifies it with `sha256sum -c`.
- D10. Gateway image base: `ubuntu:24.04` (the release was built on `ubuntu-latest` with GraalVM native-image, which links glibc dynamically, so a matching glibc is the safe choice; no Alpine). Multi-stage: curl and unzip stay in the fetch stage. Runs as a non-root user with `cap_drop: [ALL]` and `no-new-privileges`.
- D11. The gateway reads our config through `--conf ../root/conf.yaml` with the working dir `/opt/ibkr-gateway` and the file mounted read-only at `/opt/ibkr-gateway/root/conf.yaml`. This mirrors IBKR's own `bin/run.sh` (`GatewayStart --conf ../$config_file`, run from the gateway dir with `root/conf.yaml`). The native README says the config is baked in and only "some things" are CLI-configurable, so this is unverified until the user runs it. The design fails closed: if the binary ignores `--conf`, the baked default does not allow `172.31.250.*`, the check in the deploy doc returns "Access Denied", and nothing is exposed (see Open question 1).
- D12. Deploy path: the new "Home server (Docker)" section uses `~/trading-bot` (the user's words). The systemd path and deploy/UBUNTU_SETUP.md keep `/opt/trading-bot`. A note in the new section says the paths differ and why that is fine (compose uses relative paths; `deploy/backup.sh` resolves its own dir).
- D13. Firewall: none needed, none added. Nothing in this plan touches ufw, iptables or sshd. The docs explain that Docker bypasses ufw for published ports, which is why both 5000 and 8765 are bound to loopback only.
- D14. Offline "deploy config" tests (`tests/test_deploy_config.py`) parse `docker-compose.yml`, the Dockerfiles, the systemd dashboard unit and the gateway `conf.yaml` and pin the safety properties (loopback binds, no `--live`, pinned hash, allow list). PyYAML is already in `requirements.txt`.
- D15. Write `docs/adr/0001-native-ibkr-gateway-container.md` (the user's global convention for architectural decisions). Reversible: drop the file.
- D16. The keeper does not get a compose `depends_on` on the gateway (cross-profile dependencies fail when the `gateway` profile is off). If the gateway is not up yet, the keeper's login fails, the container exits and `restart: unless-stopped` retries it.

## Open questions (each has a default already applied)

1. Does native `v1.0.0` honour `--conf ../root/conf.yaml`? Default: assume yes, verify on the server with the fail-closed check (deploy step 4). If it returns "Access Denied", the user stops and reports back. Options for a follow-up: (a) rebuild the native binary from source with our `conf.yaml` (the README's own advice), or (b) give the bridge network a subnet inside `192.0.2.0/24` (TEST-NET-1) so the baked `192.*` rule admits only the Docker proxy. Option (b) breaks the "never 192.*" rule, so it needs the lead's explicit approval. Not done in this plan.
2. Does the server CPU support x86-64-v3 (AVX2)? The `-march=compatibility` build flag was added to the source repo in July 2026, after `v1.0.0` (Nov 2025) was built, and GraalVM's default target needs AVX2 or newer. Default: the deploy doc has a preflight check (`grep -c -w avx2 /proc/cpuinfo`). If it is 0, stop and keep using the Java gateway from deploy/UBUNTU_SETUP.md section 3.
3. Does the user's private module need packages that are not in `requirements.txt`? Default: assume no. If the keeper log shows `ModuleNotFoundError` for another name, add them in a follow-up (for example a git-ignored `requirements-private.txt`).
4. Is the server user's uid 1000? The bot image runs as uid 1000 and writes to the bind-mounted `logs/`. Default: the doc checks `id -u`; if it is not 1000, `chown 1000:1000 logs` is the documented fix.

## 1. Goal

Make a fresh Docker install on the user's Ubuntu home server (at `~/trading-bot`, reached over Tailscale, deployed by hand over password SSH) work end to end with: the user's private IBKR login module loaded from a git-ignored file, the dashboard bound to loopback and published to the tailnet with `tailscale serve`, the sentiment provider chosen in `.env`, and the IBKR Client Portal Gateway running as the pinned native executable in a container whose port 5000 is reachable only from the server itself. Document the exact fresh-install commands.

Out of scope:

- Any change to the paper lock, `PAPER_PORTS`, the `DRY_RUN` default or `--live` handling.
- Writing or committing any login code. The plan only loads a file the user places by hand.
- Building the gateway from source, a custom TLS certificate, CORS changes in the gateway config.
- Firewall changes, SSH hardening, Tailscale Funnel, any public exposure.
- Moving the systemd docs from `/opt/trading-bot` to `~/trading-bot`.
- Dashboard authentication, the session cookie in backups, ruff on `core/ibkr_auth.py` (listed as follow-ups in Risks).

## 2. Current state

- `core/ibkr_auth.py`: stub. Defines `IBKRAuthError`, `MaxLoginAttempts`, `IbkrCredentials` (frozen dataclass: `username`, `password`, `is_paper=True`) and `login(creds, *args, **kwargs)` that raises `NotImplementedError`. Excluded from ruff in `pyproject.toml` (`extend-exclude`, `force-exclude`).
- `live/cpgw_keeper.py`: `from core.ibkr_auth import IBKRAuthError, IbkrCredentials, MaxLoginAttempts, login`. `Keeper._full_login` calls `login(self.creds, port=self.cpgw_port)` and expects a cookie string. `Keeper.start` prints a banner with `paper`, `port` and `user`. `CPGWClient(cookie=..., port=...)` defaults to `host="localhost"` (`core/cpgw_client.py`, `base_url=f"https://{host}:{port}"`, `verify=False`).
- `.gitignore`: has `.env`, `logs/*.sqlite`, model artifacts; no private auth entry. `.dockerignore` excludes `.env`, `logs/`, `docs/`.
- `core/config.py`: `SentimentConfig(provider="stub", poll_interval_sec, staleness_sec, symbols)`. `load_sentiment_config()` reads `sentiment_provider:` (or legacy top-level `sentiment:`) from `config/symbols.yaml`, which has `provider: alpha_vantage`. It does not call `load_dotenv`. `load_app_config()` does. Callers: `live/sentiment_worker.py` (`SentimentWorker.__init__`, then `core.sentiment.get_provider(self.cfg.provider)`), `live/portfolio_runner.py` (line 105), `dashboard/app.py` (lines 124 and 302). `/api/health` does not call it.
- `tests/conftest.py`: autouse fixtures block the network, patch `core.config.load_dotenv`, and delete `_ENV_KEYS` (`PAPER`, `IB_PORT`, `IB_HOST`, `IB_CLIENT_ID`, `DRY_RUN`, `ALPHA_VANTAGE_API_KEY`).
- `docker-compose.yml`: `x-bot-common` (build `docker/Dockerfile`, image `tradingbot:local`, `network_mode: host`, `env_file: .env`, mounts `./logs`, `./models/artifacts`, `./config:ro`, `./.env:ro`). `dashboard` runs `uvicorn ... --host 0.0.0.0 --port 8765`, healthcheck `curl -fsS http://127.0.0.1:8765/api/health`. `cpgw_keeper`, `portfolio` (no `--live`), `sentiment` under profile `auto`. Header comment says CPGW runs on the host.
- `docker/Dockerfile`: python:3.11-slim, installs curl, `COPY . /app`, user `bot` uid 1000, `CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8765"]`.
- `deploy/systemd/tradingbot-dashboard.service`: already `--host 127.0.0.1`, paths under `/opt/trading-bot`.
- Docs: `deploy/README.md` (sections 0 to 6, Docker path at `/opt/trading-bot`, section 3 says open `http://100.x.x.x:8765`), `deploy/UBUNTU_SETUP.md` (Java gateway as systemd `cpgw.service` in section 3, ufw in section 0 with SSH-first ordering, `<tailnet-ip>:8765` in sections 2, 6 and 11), `SECURITY.md` (four bullets), `CLAUDE.md`, `README.md` (line 83 `uvicorn dashboard.app:app --port 8765`), `docs/TECH_STACK.md` line 143 (says `0.0.0.0:8765` in Docker), `.env.example` (no `DASHBOARD_HOST`, no `SENTIMENT_PROVIDER`).
- Gateway upstream (read with `gh api`, nothing downloaded or run): repo `petdomaa100/ibkr-client-portal-api-gateway-native-exe`, one release `v1.0.0` (2025-11-12, empty body, not prerelease). Assets: `ibkr-gateway-linux-amd64.zip` (22762329 bytes, GitHub digest above), plus darwin-arm64 and windows-amd64. No checksum file, no build attestations, no license. The release workflow (`.github/workflows/build.yml`, manual dispatch) zips `ibkr-gateway`, `run.sh` and `vertx.jks` at the zip root. `META-INF/native-image/native-image.properties` embeds `conf.yaml`, `logback.xml` and `vertx.jks` as resources. The README says the default `root/conf.yaml` and `vertx.jks` are baked in and `vertx.jks` must sit next to the executable. IBKR's default `conf.yaml` (in the repo's `clientportal.gw.zip`): `listenPort: 5000`, `listenSsl: true`, `ips.allow: [192.*, 131.216.*, 127.0.0.1]`, `ips.deny: [212.90.324.10]`. The tag `v1.0.0` points at commit `1554eecdd8bb`, which is not on the current `main` (history was rewritten after the release).

## 3. Design

### A. Private auth loader (`core/ibkr_auth.py`)

Keep the three public types as they are. Rename the stub to `_stub_login`, then try the private module at the bottom of the file, after the types exist (so the private module may import them back without a circular-import failure):

```python
def _stub_login(creds: IbkrCredentials, *args, **kwargs):
    raise NotImplementedError(
        "Automated IBKR login is not part of the public version. "
        "Log in to the Client Portal Gateway manually, see the module docstring."
    )


login = _stub_login
AUTH_BACKEND = "stub"

try:
    _private = importlib.import_module("core.ibkr_auth_private")
except ModuleNotFoundError as exc:
    if exc.name != "core.ibkr_auth_private":
        raise
else:
    login = _private.login
    IbkrCredentials = getattr(_private, "IbkrCredentials", IbkrCredentials)
    IBKRAuthError = getattr(_private, "IBKRAuthError", IBKRAuthError)
    MaxLoginAttempts = getattr(_private, "MaxLoginAttempts", MaxLoginAttempts)
    AUTH_BACKEND = "private"
```

Add `import importlib` at the top. The loader prints and logs nothing. Update the module docstring: where the private file goes, the `login` contract (D2), that it is git-ignored and must never be committed. `.gitignore` gets `core/ibkr_auth_private.py` under a comment. `live/cpgw_keeper.py` imports `AUTH_BACKEND` and adds `auth={AUTH_BACKEND}` to the banner. The file stays excluded from ruff; keep it ruff-clean by hand.

Rejected: an env var with a module path (more surface, a typo silently loads the stub); a plugin entry point (needs packaging, the repo has no `[project]` table); replacing `core/ibkr_auth.py` on the server (a tracked file would show as modified and could be committed by accident).

### B. `SENTIMENT_PROVIDER` (`core/config.py`)

```python
SENTIMENT_PROVIDERS: frozenset[str] = frozenset({"stub", "alpha_vantage"})


def load_sentiment_config() -> SentimentConfig:
    load_dotenv(ROOT / ".env")
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    # New key in Phase 8 is `sentiment_provider`; fall back to legacy `sentiment` block
    raw = data.get("sentiment_provider") or data.get("sentiment") or {}
    cfg = SentimentConfig(**raw)
    env_provider = (os.getenv("SENTIMENT_PROVIDER") or "").strip().lower()
    if env_provider:
        if env_provider not in SENTIMENT_PROVIDERS:
            allowed = ", ".join(sorted(SENTIMENT_PROVIDERS))
            raise ValueError(
                f"SENTIMENT_PROVIDER={env_provider!r} is not supported. Use one of: {allowed}."
            )
        cfg.provider = env_provider
    return cfg
```

All callers get the effective provider, so the dashboard shows what the worker uses. An invalid value makes the worker and the portfolio runner fail at start (loud, visible in `docker compose logs`) and makes the two dashboard pages that read the sentiment config fail. `/api/health` is not affected, so the dashboard container stays healthy. Rejected: warn and fall back to the YAML (a typo would silently call Alpha Vantage or silently use the stub).

### C. `DASHBOARD_HOST`

- `docker-compose.yml` dashboard command: `["uvicorn", "dashboard.app:app", "--host", "${DASHBOARD_HOST:-127.0.0.1}", "--port", "8765"]`. Healthcheck unchanged.
- `docker/Dockerfile`: `CMD ["sh", "-c", "exec uvicorn dashboard.app:app --host \"${DASHBOARD_HOST:-127.0.0.1}\" --port 8765"]`, comment updated. `EXPOSE 8765` stays (metadata only).
- `deploy/systemd/tradingbot-dashboard.service`: add `Environment=DASHBOARD_HOST=127.0.0.1` above `EnvironmentFile=`, and `ExecStart=... --host ${DASHBOARD_HOST} --port 8765`. Paths unchanged.
- `.env.example`: `DASHBOARD_HOST=127.0.0.1` with a comment: the dashboard has no auth, keep loopback, use `sudo tailscale serve --bg 8765` for tailnet access.

Remote access is `sudo tailscale serve --bg 8765`: Tailscale terminates HTTPS on `https://<machine>.<tailnet>.ts.net` and proxies to `http://127.0.0.1:8765`. Only devices on the tailnet can reach it. It is not Funnel (`tailscale funnel` would publish to the internet; the docs say never run it and show `tailscale funnel status` as a check). `--bg` keeps the config across reboots. Undo with `sudo tailscale serve reset`.

Rejected: a Python launcher that reads `DASHBOARD_HOST` (new entry point for no gain); binding to the Tailscale IP (breaks the healthcheck and fails at boot if Tailscale is not up yet).

### D. Gateway container

New files:

- `docker/gateway/Dockerfile` (build context `docker/gateway`, so the bot sources are not in its context):

  ```dockerfile
  FROM ubuntu:24.04 AS fetch
  ARG GATEWAY_VERSION=v1.0.0
  ARG GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip
  ARG GATEWAY_SHA256=01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d
  RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
      && rm -rf /var/lib/apt/lists/*
  RUN curl -fsSL -o /tmp/gw.zip \
        "https://github.com/petdomaa100/ibkr-client-portal-api-gateway-native-exe/releases/download/${GATEWAY_VERSION}/${GATEWAY_ASSET}" \
      && echo "${GATEWAY_SHA256}  /tmp/gw.zip" | sha256sum -c - \
      && mkdir /out && unzip -q /tmp/gw.zip -d /out \
      && test -f /out/ibkr-gateway && test -f /out/vertx.jks

  FROM ubuntu:24.04
  RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
      && rm -rf /var/lib/apt/lists/* \
      && useradd --system --create-home --uid 10001 gateway
  WORKDIR /opt/ibkr-gateway
  COPY --from=fetch /out/ /opt/ibkr-gateway/
  RUN chmod 0755 ibkr-gateway && mkdir -p root logs && cp vertx.jks root/vertx.jks \
      && chown -R gateway:gateway /opt/ibkr-gateway
  USER gateway
  EXPOSE 5000
  HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
      CMD curl -sk -o /dev/null https://127.0.0.1:5000/ || exit 1
  ENTRYPOINT ["./ibkr-gateway", "-Dvertx.disableDnsResolver=true", "-Djava.net.preferIPv4Stack=true", "-Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory", "-Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener", "-Dnolog4j.debug=true", "-Dnolog4j2.debug=true"]
  CMD ["--conf", "../root/conf.yaml"]
  ```

  The JVM flags are the ones in the upstream `scripts/run.sh`. The implementer checks the zip listing (`unzip -l`, in the scratch dir) and adjusts the `test -f` names if the layout differs. `chmod` is needed because the zip is made with PowerShell `Compress-Archive`, which may not keep the exec bit.

- `docker/gateway/conf.yaml`: IBKR's default config with only the `ips` block changed, written without the leading indent:

  ```yaml
  ips:
    allow:
      - 127.0.0.1        # the gateway's own healthcheck
      - 172.31.250.*     # docker proxy on the ibkr_gateway bridge (see docker-compose.yml)
    deny:
      - 212.90.324.10    # unchanged from IBKR's default
  ```

  Every other key (`listenPort: 5000`, `listenSsl: true`, `sslCert: "vertx.jks"`, `sslPwd: "mywebapi"`, `proxyRemoteHost`, `serverOptions`, `cors`, `webApps`) is copied verbatim from the default shown in Current state. A header comment explains why the allow list is still localhost-only.

- `docker-compose.yml` additions:

  ```yaml
  ibkr_gateway:
    build:
      context: docker/gateway
    image: tradingbot-ibkr-gateway:v1.0.0
    container_name: tradingbot-ibkr-gateway
    profiles: ["gateway"]
    restart: unless-stopped
    networks: [ibkr_gateway]
    ports:
      - "127.0.0.1:5000:5000"   # loopback only, never "5000:5000"
    volumes:
      - ./docker/gateway/conf.yaml:/opt/ibkr-gateway/root/conf.yaml:ro
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]

  networks:
    ibkr_gateway:
      ipam:
        config:
          - subnet: 172.31.250.0/24
  ```

  Update the header comment: the gateway can run here (profile `gateway`) or on the host as before.

Why this is localhost-only even though the allow list names a bridge range: the container has its own network namespace. The only ways in are the published port, which Docker binds to `127.0.0.1` on the host (so only local processes, including the `network_mode: host` bot containers, can connect), and other containers on the `ibkr_gateway` network (there are none). Every local connection arrives from `172.31.250.1`. A LAN host cannot reach the port: it is not bound on the LAN interface, Docker Engine 28 and newer also drop direct routed packets to container ports published on loopback, and a LAN source address (`192.168.*`) would be denied by the allow list anyway. That is why the list must not contain `192.*`.

Rejected:

- Gateway with `network_mode: host`: it binds all host interfaces and the baked default allows `192.*`, so the LAN could reach it; it would need a firewall rule, which risks SSH.
- Gateway as a systemd service on the host: same exposure, plus the binary would run unconfined on the host.
- Allowing all of `172.16.0.0/12`: wider than needed; the pinned /24 matches only our bridge.
- A ufw rule for 5000: Docker publishes bypass ufw anyway, and any ufw change risks the user's password SSH.

### E. Docs

- `deploy/README.md`: new section "Home server (Docker)" right after section 0 (full commands in Task 6), and section 3 rewritten for `tailscale serve` (the old `http://100.x.x.x:8765` no longer works with a loopback bind).
- `deploy/UBUNTU_SETUP.md`: replace the three `<tailnet-ip>:8765` spots (sections 2, 6, 11) and the "network_mode: host + Tailscale" sentence in section 0 with `tailscale serve`. Section 3 (Java gateway) gets a one-line pointer to the native container option in deploy/README.md. Paths stay `/opt/trading-bot`.
- `SECURITY.md`: private login file (git-ignored, never commit, `git check-ignore`), loopback binds for 5000 and 8765, `tailscale serve` not Funnel, the pinned and hash-checked third-party gateway binary.
- `CLAUDE.md`: dashboard command with `--host 127.0.0.1`; Gotchas: private loader and `AUTH_BACKEND`, `DASHBOARD_HOST`, `SENTIMENT_PROVIDER` (added to the `.env` variable list), gateway container and profile `gateway`; Deployment: `~/trading-bot` for the Docker home server, `/opt/trading-bot` for systemd.
- `README.md` line 83: `uvicorn dashboard.app:app --host 127.0.0.1 --port 8765`; Safety bullet on the private module.
- `docs/TECH_STACK.md` line 143: loopback in Docker too, tailnet access through `tailscale serve`.
- `docs/adr/0001-native-ibkr-gateway-container.md` (D15).
- `CHANGELOG.md` `[Unreleased]`: short entries (Added: private auth loader, `SENTIMENT_PROVIDER`, `DASHBOARD_HOST`, gateway container, home server section. Changed: dashboard binds to 127.0.0.1 in Docker, with an upgrade note to run `sudo tailscale serve --bg 8765`).

## 4. Tasks

### Task 1. Load the private login module when present

`feat: load login() from a git-ignored core/ibkr_auth_private.py`

Files: `tests/test_ibkr_auth.py` (new), `core/ibkr_auth.py`, `.gitignore`, `live/cpgw_keeper.py` (banner only).

Tests first (`tests/test_ibkr_auth.py`), all offline:

```python
import importlib
import sys
import types

import pytest

from core import ibkr_auth
from core.config import ROOT

PRIVATE = "core.ibkr_auth_private"


@pytest.fixture
def reload_auth(monkeypatch):
    def _reload(private):
        monkeypatch.setitem(sys.modules, PRIVATE, private)
        return importlib.reload(ibkr_auth)

    yield _reload
    sys.modules.pop(PRIVATE, None)
    importlib.reload(ibkr_auth)


def test_stub_is_used_when_private_module_is_missing(reload_auth, capsys):
    auth = reload_auth(None)  # None in sys.modules makes the import fail like a missing file
    assert auth.AUTH_BACKEND == "stub"
    assert auth.login is auth._stub_login
    with pytest.raises(NotImplementedError):
        auth.login(auth.IbkrCredentials(username="u", password="secret"), port=5000)
    assert capsys.readouterr() == ("", "")
```

More cases: a fake private module with only `login` is used and the public exception classes stay; a fake module that defines `IBKRAuthError` and `MaxLoginAttempts` replaces them; a `ModuleNotFoundError` for another name (patch `importlib.import_module` inside `monkeypatch.context()` so it is undone before teardown) is re-raised; a private module without `login` raises `AttributeError`; `.gitignore` lines contain `core/ibkr_auth_private.py`; no loader output on stdout or stderr when a private module is loaded (`capsys`, with a fake password in the credentials).

Verify: `git check-ignore -v core/ibkr_auth_private.py` prints the `.gitignore` rule; ruff and pytest green.

independent

### Task 2. `SENTIMENT_PROVIDER` override

`feat: select the sentiment provider with SENTIMENT_PROVIDER in .env`

Files: `tests/test_sentiment_config.py` (new), `tests/conftest.py` (add `SENTIMENT_PROVIDER` to `_ENV_KEYS`), `core/config.py`, `.env.example` (new `SENTIMENT_PROVIDER=` line in the sentiment block, with a comment: empty means `config/symbols.yaml`, allowed `stub` or `alpha_vantage`).

Tests first. Point `core.config.CONFIG_DIR` at a `tmp_path` with a small `symbols.yaml` (monkeypatch), then: unset uses the YAML value; `stub` overrides `alpha_vantage`; `" Alpha_Vantage "` becomes `alpha_vantage`; empty string uses the YAML; `alphavantage` and `bogus` raise `ValueError` matching `SENTIMENT_PROVIDER`; other YAML fields (`poll_interval_sec`, `symbols`) are kept; no YAML file gives `stub`, and the env var still applies; the real repo `config/symbols.yaml` with no env var gives `alpha_vantage` (pins the fallback); `get_provider(load_sentiment_config().provider).name == "stub"` with `SENTIMENT_PROVIDER=stub` (the path `SentimentWorker` takes); `load_sentiment_config` calls `load_dotenv` (replace the conftest patch with a recorder for that test).

Verify: ruff and pytest green.

independent

### Task 3. `DASHBOARD_HOST`, default 127.0.0.1

`feat: bind the dashboard to DASHBOARD_HOST, default 127.0.0.1`

Files: `tests/test_deploy_config.py` (new), `docker-compose.yml` (dashboard command), `docker/Dockerfile` (`CMD`), `deploy/systemd/tradingbot-dashboard.service`, `.env.example` (`DASHBOARD_HOST=127.0.0.1` block).

Tests first (`tests/test_deploy_config.py`, load files from `core.config.ROOT`, parse YAML with `yaml.safe_load`): the dashboard command has `--host` followed by `${DASHBOARD_HOST:-127.0.0.1}`; the healthcheck targets `http://127.0.0.1:8765/api/health`; the Dockerfile `CMD` contains `${DASHBOARD_HOST:-127.0.0.1}`; the systemd unit has `Environment=DASHBOARD_HOST=127.0.0.1` and `--host ${DASHBOARD_HOST}`; the string `0.0.0.0` appears nowhere in `docker-compose.yml`, `docker/Dockerfile` or `deploy/systemd/*.service`; the `portfolio` command is exactly `["python", "-m", "live.portfolio_runner"]` (pins the dry-run default); `.env.example` contains `PAPER=true`, `DRY_RUN=true`, `IB_PORT=5000` and `DASHBOARD_HOST=127.0.0.1`.

Verify: ruff and pytest green. If Docker is installed locally, `docker compose config -q` and `docker compose --profile auto config | grep -A6 'command:'` (parse only, starts nothing).

depends on: 2

### Task 4. Native gateway container on loopback

`feat: run the IBKR gateway as a pinned native-exe container on 127.0.0.1:5000`

Files: `docker/gateway/Dockerfile` (new), `docker/gateway/conf.yaml` (new), `docker-compose.yml` (service `ibkr_gateway`, network `ibkr_gateway`, header comment), `tests/test_deploy_config.py` (gateway tests).

Before writing the Dockerfile, confirm the hash. On this Mac, in a new empty scratch dir, without executing anything:

```sh
mkdir -p "$SCRATCH/gw-asset" && cd "$SCRATCH/gw-asset"
gh release download v1.0.0 -R petdomaa100/ibkr-client-portal-api-gateway-native-exe \
  -p ibkr-gateway-linux-amd64.zip
shasum -a 256 ibkr-gateway-linux-amd64.zip   # must equal 01e3b095...e0fe5d
unzip -l ibkr-gateway-linux-amd64.zip        # list only; expect ibkr-gateway, run.sh, vertx.jks
```

If the hash differs, stop and report (the release asset changed). Delete the scratch dir afterwards. Never unzip it inside the repo.

Tests first: `ibkr_gateway` has `ports == ["127.0.0.1:5000:5000"]`, no `network_mode`, `networks == ["ibkr_gateway"]`, `profiles == ["gateway"]`, `restart == "unless-stopped"`, the conf mount ends in `:ro`, `cap_drop` contains `ALL`; no service publishes a port without a `127.0.0.1:` prefix; the bot services keep `network_mode: host`; the network subnet is `172.31.250.0/24`; `conf.yaml` parses, `listenPort` is 5000 and in `core.config.PAPER_PORTS`, `listenSsl` is true, `ips.allow == ["127.0.0.1", "172.31.250.*"]`, and no allow entry starts with `192.`, is `*` or contains `0.0.0.0`; the subnet prefix and the allow pattern match (derive one from the other in the test); `docker/gateway/Dockerfile` contains `GATEWAY_VERSION=v1.0.0`, `GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip`, the full SHA-256 and `sha256sum -c`, and has a `USER` line.

Verify: ruff and pytest green. If Docker is installed locally: `docker compose --profile gateway config -q` only. Do not build or run the gateway image on this machine (the build would download the binary, and the image is amd64).

depends on: 3

### Task 5. Security and contributor docs

`docs: private login file, loopback binds and the gateway container`

Files: `SECURITY.md`, `CLAUDE.md`, `README.md`, `docs/TECH_STACK.md`, `deploy/UBUNTU_SETUP.md`, `docs/adr/0001-native-ibkr-gateway-container.md` (new). Content as in Design E.

Verify: `rg -n "0\.0\.0\.0" README.md CLAUDE.md SECURITY.md docs/TECH_STACK.md deploy/UBUNTU_SETUP.md` finds nothing that recommends a public bind; `rg -n "<tailnet-ip>:8765|100\.x\.x\.x:8765" deploy docs README.md` finds nothing; `rg -n "\x{2014}|\x{2013}" <changed files>` finds nothing (no em or en dashes); ruff format check still green (it formats Python blocks in Markdown).

depends on: 1, 2, 3, 4

### Task 6. "Home server (Docker)" section and changelog

`docs: home server (Docker) install guide`

Files: `deploy/README.md`, `CHANGELOG.md`.

`deploy/README.md`: rewrite section 3 for `tailscale serve`, and add "Home server (Docker)" after section 0. Every command says where it runs. All server commands are typed by the user over SSH, never by an agent. Content:

0. Note: this section uses `~/trading-bot`; the systemd path and UBUNTU_SETUP.md use `/opt/trading-bot`. Compose uses relative paths and `deploy/backup.sh` finds its own dir, so both work. It changes no firewall rule and does not touch SSH.
1. Preflight (server): `uname -m` (expect `x86_64`), `grep -c -w avx2 /proc/cpuinfo` (expect more than 0, else stop, Open question 2), `docker version --format '{{.Server.Version}}'` (expect 28 or newer), `systemctl is-enabled docker` (expect `enabled`), `tailscale status`, `id -u` (expect 1000), `ss -ltn '( sport = :5000 )'` (expect nothing; if the Java `cpgw` service from UBUNTU_SETUP.md is running, `sudo systemctl disable --now cpgw` first).
2. Clone (server): `git clone https://github.com/pechkornel/trading-bot.git ~/trading-bot`, `cd ~/trading-bot`, `mkdir -p logs`.
3. `.env` (server): `cp .env.example .env`, `chmod 600 .env`, `nano .env`. Required: `PAPER=true`, `DRY_RUN=true`, `IB_PORT=5000`, `DASHBOARD_HOST=127.0.0.1`, `SENTIMENT_PROVIDER=stub` (or `alpha_vantage` plus `ALPHA_VANTAGE_API_KEY`), `IBKR_USERNAME`, `IBKR_PASSWORD`, `EMAIL_*`.
4. Private auth module. Laptop (macOS zsh or Windows PowerShell, same command): `scp ibkr_auth_private.py <user>@<server>:~/trading-bot/core/ibkr_auth_private.py`. Server: `chmod 600 core/ibkr_auth_private.py`, `git check-ignore -v core/ibkr_auth_private.py` (must print the rule), `git status --short` (must be empty). Never `git add -f` it.
5. Gateway (server): `docker compose --profile gateway up -d --build ibkr_gateway`, `docker compose ps ibkr_gateway` (wait for `healthy`), `ss -ltn '( sport = :5000 )'` (only `127.0.0.1:5000`), `curl -sk https://127.0.0.1:5000/ | head -c 300` (login page HTML; "Access Denied" means the mounted config was ignored: run `docker compose --profile gateway down` and stop, nothing is exposed). From the laptop: `curl -sk --max-time 5 https://<server-lan-ip>:5000/` must fail to connect.
6. Dashboard (server): `docker compose up -d --build dashboard`, `curl -fsS http://127.0.0.1:8765/api/health` (expect `"paper":true`, `"dry_run":true`, `"ib_port":5000`), `ss -ltn '( sport = :8765 )'` (only `127.0.0.1:8765`).
7. Tailnet access (server): `sudo tailscale serve --bg 8765`, `tailscale serve status` (shows the `https://<machine>.<tailnet>.ts.net` URL proxying to `http://127.0.0.1:8765`; the first run may ask to enable HTTPS certificates in the admin console), `tailscale funnel status` (must show no Funnel). Open the URL from a tailnet device. Never run `tailscale funnel`.
8. Workers (server): `docker compose --profile gateway --profile auto up -d --build`, `docker compose logs -f cpgw_keeper` (banner shows `auth=private`, then `login OK`), `docker compose logs portfolio | head` (dry run, no `--live`), `docker compose logs sentiment | head` (banner shows the chosen provider).
9. Reboot check (optional, server): `sudo reboot`, reconnect, `docker compose --profile gateway --profile auto ps` (all up), `tailscale serve status`.
10. Updates, backups and rollback (server): `git pull && docker compose --profile gateway --profile auto up -d --build`; cron line `15 3 * * * $HOME/trading-bot/deploy/backup.sh`; stop everything with `docker compose --profile gateway --profile auto down` and `sudo tailscale serve reset`.
11. Troubleshooting: "Access Denied" (Open question 1), gateway exits with code 132 or "Illegal instruction" (CPU lacks x86-64-v3, Open question 2), `Pool overlaps` when creating the network (another Docker network uses 172.31.250.0/24: change the subnet in `docker-compose.yml` and the allow pattern in `docker/gateway/conf.yaml` together, the tests enforce the match), permission errors in `logs/` (`sudo chown 1000:1000 logs`), keeper `ModuleNotFoundError` (Open question 3).

`CHANGELOG.md` `[Unreleased]`: the short entries listed in Design E.

Verify: `rg -n "\x{2014}|\x{2013}" deploy/README.md CHANGELOG.md` finds nothing; ruff format check green; every command in the section is copy-paste ready (the only placeholders are `<user>`, `<server>` and `<server-lan-ip>`).

depends on: 1, 2, 3, 4

## 5. Tests

Write first: `tests/test_ibkr_auth.py` (stub when missing, the user's explicit ask) and `tests/test_sentiment_config.py`. Both must fail on current code (`AUTH_BACKEND` and `_stub_login` do not exist; `SENTIMENT_PROVIDER` is ignored). Then `tests/test_deploy_config.py`, which fails until compose, Dockerfiles and the unit are changed.

Edge cases:

- Auth: a dev machine that has a real `core/ibkr_auth_private.py`. The tests force `None` or a fake into `sys.modules`, so they pass on CI and on the dev machine. The fixture restores the module afterwards. Missing dependency inside the private module must not fall back to the stub. A private module without `login`. Nothing printed.
- Sentiment: empty string, whitespace, uppercase, the `alphavantage` alias (rejected), no YAML file, YAML without `provider`.
- Deploy config: `0.0.0.0` anywhere, a port published without `127.0.0.1:`, `--live` in the portfolio command, allow list drift between `conf.yaml` and the compose subnet, a wildcard or `192.*` entry.

Fixtures: none on disk. Fake modules with `types.ModuleType`; a temp `symbols.yaml` in `tmp_path`. Add `SENTIMENT_PROVIDER` to `_ENV_KEYS` in `tests/conftest.py`. No network, no Docker, no gateway binary in any test.

Not testable offline (the user verifies on the server, deploy steps 5 to 9): the binary runs, `--conf` is honoured, the keeper logs in, `tailscale serve` works.

## 6. Risks

- Supply chain of the gateway binary. A two-star personal repo with no license, no checksum file, no build attestations, an empty release body, and a tag that points at a commit no longer on `main`. The binary handles the IBKR login session. Mitigations: pin `v1.0.0` and the SHA-256 (a changed asset fails the build), non-root user, `cap_drop: [ALL]`, `no-new-privileges`, own bridge network, loopback-only publish, paper account only (`assert_paper_mode` unchanged). Residual risk: the binary can still talk to the internet and sees the session. Follow-up: build from source in our own CI, or go back to IBKR's Java gateway.
- CPU support: `v1.0.0` may need AVX2 (Open question 2). Preflight check in the docs; fallback is the Java gateway from UBUNTU_SETUP.md section 3.
- `--conf` may be ignored (Open question 1). Fails closed; the check in step 5 catches it.
- SSH lockout: nothing in this plan changes ufw, iptables, sshd or Tailscale ACLs. No agent runs any server command. The only server-side network change is Docker's own rules for a loopback publish and a new bridge.
- Exposure of 5000: loopback publish only, allow list without LAN ranges, Docker 28 or newer blocks direct routed access. The tests forbid non-loopback publishes. Risk if someone later changes the publish to `5000:5000`: Docker would bypass ufw and expose the gateway to the LAN; the test fails first.
- Exposure of 8765: the dashboard has no auth. Default `127.0.0.1` everywhere, tailnet access only through `tailscale serve`, never Funnel. Setting `DASHBOARD_HOST=0.0.0.0` together with `network_mode: host` exposes it to the LAN; the docs say so.
- Behaviour change for existing Docker installs: the dashboard no longer answers on `http://<tailnet-ip>:8765`. Upgrade note in the CHANGELOG: run `sudo tailscale serve --bg 8765`, or set `DASHBOARD_HOST=0.0.0.0` knowingly.
- Secrets: the private module is git-ignored, never `git add -f`. It is inside the local image `tradingbot:local`; never push or `docker save` that image. `.env` stays `chmod 600` and is still excluded from the image. `logs/cpgw_session.json` holds the session cookie and is in the nightly backup (existing behaviour, follow-up issue). The loader and the banner never print the password.
- `SENTIMENT_PROVIDER` typo: the worker and the runner fail at start, by design; the dashboard health stays green.
- localhost resolution: `CPGWClient` uses `localhost`; if it tries `::1` first, the connection is refused and it falls back to `127.0.0.1`. Set `IB_HOST=localhost` as today; no change.
- Deployment steps: by hand by the user, over SSH, in the order of the new section. Rollback: `docker compose --profile gateway --profile auto down`, `sudo tailscale serve reset`, re-enable the Java gateway (`sudo systemctl enable --now cpgw`) if it was used, `git checkout` the previous commit and `docker compose --profile auto up -d --build`. Code rollback is a revert of the PR; there is no schema or data migration.

Suggested follow-up issues (not in this plan): build the gateway from source with our `conf.yaml` in CI; Dependabot for the Docker base images; dashboard authentication; exclude `logs/cpgw_session.json` from backups or encrypt them; decide whether `core/ibkr_auth.py` should stay excluded from ruff now that it has real logic.

## 7. Open questions

See "Open questions" at the top. Summary of defaults: (1) assume `--conf` works, fail closed and stop if not; (2) preflight AVX2 check, stop if missing; (3) assume the private module needs no extra packages; (4) assume uid 1000, `chown` documented.

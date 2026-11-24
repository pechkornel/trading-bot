# Plan: Phase 1 safe foundation (paper only)

Status: approved by the user with every recommended default (Open question 1: option A, the tracked generic flow). Task 17 still needs a separate yes.

## Open questions first (each has a recommended default; details in section 9)

1. Where the login code lives. Recommended: the generic, testable parts go in a tracked module `core/gateway_login.py` with offline tests, and `core/ibkr_auth_private.py` stays a thin wiring file. The user asked for the code in the private file, so this needs a yes before Task 7.
2. Gateway allow list: exact bridge address `172.31.250.1` (recommended) or the `/24`.
3. `IBKR_PASSWORD` still set in `.env`: the keeper refuses to start (recommended) or warns and ignores it.
4. Gmail notifier: keep it for `live/runner.py` only, Telegram for everything else (recommended).
5. Order alerts: one Telegram message per order including dry-run orders, tagged `[DRY-RUN]` or `[PAPER]` (recommended), or only `--live` orders.
6. Keeper maintenance windows: time zone and times (defaults below need the user's confirmation).
7. Paper account guard: refuse any account id that does not start with `DU` (recommended on).
8. The `sentiment` compose service moves to a new profile `research` (recommended).
9. Old simulated sentiment holdings in SQLite: warn only (recommended) or delete.
10. Gateway binary: Dockerfile copies the zip from `vendor/` (recommended) or keeps downloading.

## 1. Goal

Make the paper-only bot run unattended on a headless home server. The gateway container gets its own TLS key and a password from a Docker secret, and only the exact Docker bridge address may reach it. The keeper logs in by itself with headless Playwright on the gateway's own login page, keeps the session alive, logs in again after the known resets, and never retries in a way that could lock the account. Credentials come only from a chmod 600 file or a Docker secret. Nothing secret reaches logs, exceptions, the SQLite events table or Telegram. A Telegram notifier sends a daily summary, every order, errors and session-lost alerts. The sentiment satellite is switched off (`allocation_pct: 0`, `status: research`) and gets a data health check that fails loudly. Every compose service gets a memory limit. `deploy/README.md` gets the steps the user runs by hand on the server, starting with a backup.

Unchanged and guarded by tests: `PAPER_PORTS = {5000}`, `assert_paper_mode`, the dry-run default, `--live` handling. No live order path is added.

### Non-goals

- No connection to a real (non-paper) IBKR account or to Lightyear. No research of IBKR policy. Gateway behaviour is taken only from the existing code, the ADRs and the user's request.
- No push, merge or deploy. Nobody connects to the server; the user runs the deploy steps.
- No change to the core sleeve, sizing, or the 70% core allocation. With the satellite at 0, 30% of equity stays unallocated.
- No removal of the Gmail notifier.
- No firewall, ufw, iptables or sshd change.
- No X server, Xvfb, VNC or desktop on the server.
- No Telegram commands or inbound webhooks (send only).

## 2. Current state

- `core/ibkr_auth.py`: public stub plus the importlib loader for `core/ibkr_auth_private.py`. Defines `IBKRAuthError`, `MaxLoginAttempts`, `IbkrCredentials` (a frozen dataclass whose default `repr` prints the password), `login`, `AUTH_BACKEND`. Excluded from ruff in `pyproject.toml` (that exclusion stays).
- `live/cpgw_keeper.py`: `_read_credentials()` reads `IBKR_USERNAME` and `IBKR_PASSWORD` from env. `Keeper.start()` tries `_try_resume_session()` and else `_full_login()`, which re-raises on failure, so the process exits and Docker restarts it (every restart is a fresh login attempt, with no persisted counter). `Keeper._tick()` tickles every 60 s and nudges `ssodh_init`. It never logs in again after a session drop. The banner prints the username. `_write_session()` writes `logs/cpgw_session.json` (chmod 600).
- `core/cpgw_client.py`: `CPGWClient.__init__` uses `httpx.Client(verify=False)`. Has `tickle`, `auth_status`, `ssodh_init`, `logout`, `list_accounts`, `bootstrap`.
- `live/notifier.py`: Gmail SMTP `send_email` and `format_trade_alert`, used only by `live/runner.py`. No Telegram code.
- `live/portfolio_runner.py`: `PortfolioRunner.__init__` builds the satellite when `sent_cfg.enabled`. `start()` refuses an empty session cookie. `loop()` logs `cycle error`. `_execute_core_leg` and `_exec_satellite_leg` log orders. It imports `read_session` from `live.cpgw_keeper`, which imports `core.ibkr_auth`, which imports the private module. So anything the private module imports at module level is imported by the portfolio runner too.
- `core/config.py`: `SentimentSatelliteConfig` (no `status`, no validation), `PortfolioConfig.__post_init__` shows the strict validation style to copy. `load_phase8_sleeves()`, `load_sentiment_config()`.
- `config/symbols.yaml`: `sleeves.sentiment.enabled: true`, `allocation_pct: 0.20`; `sentiment_provider.provider: alpha_vantage`.
- `core/sentiment.py`: `AlphaVantageProvider.fetch_batch` raises `RuntimeError("Alpha Vantage throttled: ...")` on a `Note` or `Information` payload; an empty feed silently becomes zero readings. `live/sentiment_worker.py` writes whatever comes back and has no `--check`.
- `core/market_hours.py`: `to_eastern`, the `America/New_York` `ZoneInfo`, NYSE calendar helpers.
- `docker/gateway/Dockerfile`: fetch stage downloads `ibkr-gateway-linux-amd64.zip` `v1.0.0` from `github.com/petdomaa100/ibkr-client-portal-api-gateway-native-exe` at build time and checks SHA-256 `01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d`. Both stages are `ubuntu:24.04` pinned by digest. It copies IBKR's default `vertx.jks` into `root/`.
- `docker/gateway/conf.yaml`: IBKR default except `ips`. `sslPwd: "mywebapi"` is the IBKR default, and `vertx.jks` is the keystore every gateway user ships, so its private key is public. Changing only `sslPwd` would not help: the keystore must be replaced too.
- `docker-compose.yml`: `ibkr_gateway` (profile `gateway`, bridge `172.31.250.0/24`, `127.0.0.1:5000:5000`). Bot services share `x-bot-common` (one image `tradingbot:local`, `network_mode: host`, `env_file: .env`, `.env` mounted read-only). No memory limits. No secrets.
- `docker/Dockerfile`: one stage, `python:3.11-slim`, `requirements.txt`, `COPY . /app`, user `bot` (uid 1000).
- `.gitignore`: has `core/ibkr_auth_private.py` and `logs/*.json`. No `secrets/` or `vendor/`. `.dockerignore`: no `secrets/` or `vendor/` (a `secrets/` folder would be copied into the image today).
- `.github/workflows/ci.yml`: ruff format check, ruff check, pytest on 3.11 and 3.12.
- Tests: `tests/conftest.py` (sockets and DNS blocked, `load_dotenv` patched, `_ENV_KEYS` cleared), `tests/test_ibkr_auth.py`, `tests/test_deploy_config.py` (pins compose, conf.yaml, Dockerfiles).
- `deploy/README.md` section "0A. Home server (Docker)"; `deploy/backup.sh` archives `logs/`, `models/artifacts/`, `config/symbols.yaml` (not `.env`, not secrets).
- ADRs: `0001-native-ibkr-gateway-container.md`, `0002-own-backtest-replay.md`. The next number is 0003.

## 3. Design

### A. Module layout for the login (Open question 1, recommended option)

```
core/auth_types.py        IBKRAuthError, MaxLoginAttempts, IbkrCredentials (moved here),
                          new subclasses BadCredentials, SecondFactorRequired,
                          ManualStepRequired, GatewayUnreachable, LoginTimeout, NotPaperAccount
core/ibkr_auth.py         re-exports those names; loader logic unchanged
core/secret_store.py      secret file loading, chmod 600 check, Redactor
core/gateway_login.py     generic Playwright flow behind a LoginPage protocol (tracked, tested)
core/ibkr_auth_private.py git-ignored thin wiring: defines login(), may override selectors
deploy/ibkr_auth_private.py.example   template for that file (not importable, not linted)
```

Why `core/auth_types.py`: the private module will import `core.gateway_login`, which needs `IBKRAuthError`. If `gateway_login` imported `core.ibkr_auth`, importing `core.gateway_login` first would start `core.ibkr_auth`, which imports the private file, which imports the half-loaded `core.gateway_login`: a circular import. Moving the classes into a module with no imports breaks the cycle. The loader in `core/ibkr_auth.py` needs no logic change. Only its class definitions become `from core.auth_types import ...`, so `from core.ibkr_auth import IBKRAuthError` keeps working and the private-override `getattr` lines still apply. The ruff exclusion for `core/ibkr_auth.py` is not touched; keep the file clean by hand.

`IbkrCredentials` gets `password: str = field(repr=False)` and a `__repr__` that masks the username (first two characters, then `***`). Same for `__str__`.

Import rule, enforced by a test: importing `core.gateway_login` (and therefore the private module) must not import `playwright`. Playwright is imported inside the function that launches the browser. Otherwise the portfolio, dashboard and sentiment containers, which have no Playwright, would crash on `import live.cpgw_keeper`.

### B. Secrets

Files on the host (all git-ignored and docker-ignored):

| Host path | Contents | Owner, mode | Mounted as |
|---|---|---|---|
| `secrets/ibkr_login` | `IBKR_USERNAME=...` and `IBKR_PASSWORD=...` lines | uid 1000 (`bot`), 600 | Docker secret `/run/secrets/ibkr_login` in `cpgw_keeper` only |
| `secrets/gateway_ssl_password` | one line, 48 hex chars (`openssl rand -hex 24`) | uid 10001 (`gateway`), 600 | Docker secret `/run/secrets/gateway_ssl_password` in `ibkr_gateway` only |

Tracked placeholders: `secrets/README.md`, `secrets/ibkr_login.example`, `secrets/gateway_ssl_password.example` (contents: obvious placeholders, never real values). `.gitignore` gets `secrets/*`, `!secrets/*.example`, `!secrets/README.md` and `vendor/`. `.dockerignore` gets `secrets/` and `vendor/`.

Compose file-based secrets are bind mounts that keep the host owner and mode. That is why the host files are owned by the uid of the container user, and why the mode check below works the same in Docker and on bare Linux.

`core/secret_store.py`:

- `resolve_login_secret_path() -> Path`: `IBKR_LOGIN_SECRET_FILE` if set, else `/run/secrets/ibkr_login` if it exists, else `ROOT / "secrets" / "ibkr_login"`.
- `check_secret_file(path)`: refuses (raises `SecretFileError`) when the path is missing, is a symlink, is not a regular file, is larger than 4 KiB, has any group or other permission bit (`st_mode & 0o077`), or is owned by a uid other than `os.geteuid()`. Skipped on Windows (`os.name == "nt"`) with a comment; the keeper only runs on Linux.
- `load_login_secrets(path=None) -> IbkrCredentials`: strict `KEY=value` parser. Blank lines and `#` comments are ignored. No quotes, no `export`, no interpolation. Exactly the keys `IBKR_USERNAME` and `IBKR_PASSWORD`, both non-empty. Error messages name the file, the line number and the key, never a value. `is_paper` comes from `PAPER` as today.
- `Redactor`: holds secret strings (password, username, cookie, Telegram token) and replaces each with `***` in any text. It also masks the pattern `bot<digits>:<token>` (Telegram API URLs) and `cookie=...` style pairs. Every Telegram message, every keeper event and every exception text that leaves the login code goes through it.

Credentials in env: `IBKR_USERNAME` and `IBKR_PASSWORD` are no longer read. If `IBKR_PASSWORD` is non-empty in the keeper's environment, the keeper refuses to start and says to move it into `secrets/ibkr_login` (Open question 3). Reason: `.env` is loaded into every container and mounted into every container today.

### C. Headless login (`core/gateway_login.py`)

```python
class LoginPage(Protocol):
    def goto(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def wait_for_first(self, markers: dict[str, str], timeout_sec: float) -> str | None: ...


def run_login_flow(
    page: LoginPage,
    creds: IbkrCredentials,
    config: GatewayLoginConfig,
    *,
    on_submit: Callable[[], None],
    is_authenticated: Callable[[], bool],
) -> None: ...


def login_via_gateway_page(
    creds: IbkrCredentials, *, port: int, config: GatewayLoginConfig | None = None
) -> str: ...
```

- `GatewayLoginConfig` (frozen dataclass): `base_url` (`https://127.0.0.1:<port>`), `cert_file` (from `CPGW_CA_FILE`), selectors for username, password and submit, marker selectors or texts for success, bad credentials and second factor, page timeout (60 s), API confirm timeout (30 s). Defaults are best guesses for the gateway's login page and are confirmed on the server (deploy step). The private file can override them without touching tracked code.
- Flow: open `base_url + "/"` and follow the gateway's redirect to its own login page, fill username and password, call `on_submit()` (the keeper records the attempt before the click), click submit, wait for the first marker. Then confirm with the gateway API through `is_authenticated()` (polls `/iserver/auth/status` through `CPGWClient`). Success requires the API to say authenticated, not page text.
- Outcome mapping: API authenticated means success. Bad-credentials marker raises `BadCredentials`. Second-factor marker raises `SecondFactorRequired`. Any other page state after submit raises `ManualStepRequired`. Connection refused before submit raises `GatewayUnreachable`. Timeout before submit raises `LoginTimeout`. All messages are fixed strings; Playwright exceptions are caught and re-raised with `from None` so the chained text cannot carry a filled value.
- Browser (`playwright_page(config)`, a context manager, Playwright imported inside): `chromium.launch(headless=True)` using the `chromium-headless-shell` build, so no X server or Xvfb is needed. Launch args: `--ignore-certificate-errors-spki-list=<pin>` and `--disable-dev-shm-usage`. The context blocks service workers and downloads, records no video, trace, HAR or screenshot (a screenshot could show the filled form). A route handler aborts every request whose origin is not the gateway origin and logs only the blocked host names. The browser, context and page are closed in `finally`, so the browser always closes after login, success or not.
- TLS without disabling verification broadly: the gateway gets its own key (section D) and writes its public cert to a shared volume. `spki_pin_from_pem(pem: bytes) -> str` computes the base64 SHA-256 of the cert's SubjectPublicKeyInfo with `cryptography` (already in `requirements.txt`). Chromium accepts exactly that one key. `ignore_https_errors` is never used. If `CPGW_CA_FILE` is unset or missing, the login refuses to run (fails closed). Users of the Java gateway export its cert once (documented).
- Return value: the gateway origin's cookies from the browser context as a `name=value; ...` header string. It may be empty, because the gateway holds the session itself. The keeper and the runner accept an empty cookie (Tasks 9 and 12).
- Where it runs: inside the `cpgw_keeper` container (host network) against `https://127.0.0.1:5000`. Docker's loopback proxy delivers that connection to the gateway from `172.31.250.1`, which the allow list admits.
- The private file becomes:

```python
from core.gateway_login import GatewayLoginConfig, login_via_gateway_page


def login(creds, *, port, **kwargs):
    return login_via_gateway_page(creds, port=port, config=GatewayLoginConfig.from_env())
```

### D. Gateway container

- Vendor pin (Open question 10): `scripts/fetch_gateway.sh` downloads release `v1.0.0` asset `ibkr-gateway-linux-amd64.zip` from the same repository into `vendor/ibkr-gateway/`, verifies it against `docker/gateway/gateway.sha256` (one line in `sha256sum -c` format, the single source of the hash), then moves it into place. It never unzips or runs anything. It uses `sha256sum`, or `shasum -a 256` on macOS. The compose build gets `additional_contexts: {vendor: ./vendor/ibkr-gateway}`, and the fetch stage does `COPY --from=vendor` plus `sha256sum -c gateway.sha256` again. The main build context stays `docker/gateway`, so no bot source, private module or secret is sent to the daemon. Why vendor instead of downloading in the build: the user asked for it, rebuilds stop depending on a personal GitHub release whose tag already points off `main`, and the user can inspect the exact file. Cost: one manual step, and a fresh clone fails to build with a clear "run scripts/fetch_gateway.sh" error.
- Own keystore and `sslPwd` from a secret: `docker/gateway/entrypoint.sh` (POSIX sh, `set -eu`, never `set -x`) runs before the binary:
  1. Reads `/run/secrets/gateway_ssl_password`. Exits 1 unless it matches `^[0-9A-Za-z]{32,128}$`.
  2. If the named volume `ibkr_gateway_tls` (mounted at `/opt/ibkr-gateway/tls`) has no `keystore.p12`, generates an RSA 3072 self-signed cert with `openssl` (SAN `DNS:localhost,IP:127.0.0.1`, `keyUsage`, `extendedKeyUsage=serverAuth`, 825 days) and a PKCS12 keystore. Passwords go through `-passout file:` and `-passin file:`, never argv. The private key is written only to the tmpfs and removed after export. `cert.pem` is written with mode 644 for the keeper.
  3. If the keystore exists, checks that the password opens it. Otherwise exits 1 with "password does not match the keystore, remove the ibkr_gateway_tls volume to regenerate". It never regenerates silently.
  4. Renders `/opt/ibkr-gateway/conf.template.yaml` (the tracked `docker/gateway/conf.yaml`, mounted read-only) into `/opt/ibkr-gateway/run/conf.yaml` (tmpfs, mode 600), replacing `__SSL_PWD__`, and copies the keystore to `run/vertx.jks`. Then `exec ./ibkr-gateway ... --conf ../run/conf.yaml`.
- The Dockerfile deletes IBKR's default `vertx.jks` (both copies), so the gateway cannot fall back to the public key. If the binary rejects the PKCS12 file, TLS does not start, the healthcheck fails and nothing is exposed (fails closed; fallback in Risks).
- `docker/gateway/conf.yaml`: `sslPwd: "__SSL_PWD__"`, `ips.allow: ["127.0.0.1", "172.31.250.1"]` (Open question 2). The deny entry and the rest stay IBKR's default.
- Compose: `secrets: [gateway_ssl_password]`, `tmpfs: /opt/ibkr-gateway/run` (uid 10001, mode 0700, 1 MiB), volume `ibkr_gateway_tls:/opt/ibkr-gateway/tls`, unchanged loopback publish, `cap_drop: [ALL]`, `no-new-privileges`.

### E. Keeper session policy

New `keeper:` block in `config/symbols.yaml`, loaded by `core.config.load_keeper_config()` into a strictly validated `KeeperConfig` (same style as `PortfolioConfig.__post_init__`):

| Key | Default | Meaning |
|---|---|---|
| `tickle_interval_sec` | 60 | `POST /tickle` cadence |
| `timezone` | `America/New_York` | zone for the windows below (Open question 6) |
| `nightly_gap_start`, `nightly_gap_end` | `"00:45"`, `"01:30"` | the ~01:00 `/iserver` gap: no fresh login inside it |
| `maintenance_weekday`, `maintenance_start`, `maintenance_end` | 5, `"00:00"`, `"23:59"` | Saturday maintenance: no fresh login inside it |
| `max_session_age_sec` | 86400 | the 24 h reset: a drop after this age is expected |
| `login_backoff_base_sec`, `login_backoff_max_sec` | 60, 1800 | backoff 60, 120, 240 ... capped at 30 min |
| `max_failed_logins`, `failed_login_window_sec` | 3, 86400 | lockout guard |
| `session_lost_alert_cooldown_sec` | 3600 | at most one session-lost alert per hour |
| `paper_account_prefix` | `"DU"` | every account id must start with this (Open question 7) |

`live/keeper_policy.py` is pure (no I/O, injected `now`, uses `zoneinfo.ZoneInfo(cfg.timezone)`):

- `Observation`: `AUTHENTICATED`, `NOT_AUTHENTICATED` (tickle or `auth_status` says false after one `ssodh_init` nudge, or `NotAuthenticatedError`), `GATEWAY_DOWN` (transport error).
- `KeeperState` (persisted to `logs/keeper_state.json`, chmod 600, already git-ignored by `logs/*.json`, holds no secret): `last_login_at`, `authenticated_since`, `failed_attempts` (timestamps of submitted attempts), `locked_out_reason`, `last_alert_at` per key, `consecutive_failures`.
- `decide(state, observation, now, cfg) -> Action`, with actions `TICKLE` (all fine), `WAIT` (lost, but inside a window or in backoff; carries `until`), `LOGIN`, `LOCKED_OUT`.
- Rules:
  - Never log in while the gateway reports authenticated. The keeper re-checks `auth_status` right before every login.
  - Inside a nightly gap or Saturday maintenance window: `WAIT` until the window ends, then `LOGIN`. One info alert per window ("session dropped during the nightly gap, next login at HH:MM").
  - A drop after `max_session_age_sec`: expected, `LOGIN` at once, info alert.
  - Any other drop: warn alert "session lost", `LOGIN` with backoff.
  - `GATEWAY_DOWN`: no login (credentials are never submitted to a gateway that is down), alert after 5 minutes, then hourly.
  - Only attempts where the form was submitted count toward `max_failed_logins`.
  - `BadCredentials`, `SecondFactorRequired`, `ManualStepRequired` and `NotPaperAccount` lock out at once: no retry. A Telegram alert says what is needed and how to clear it.
  - Three submitted failures within 24 h lock out too.
  - The lockout is persisted, so a container restart cannot bypass it. The keeper stays running (tickling, no logins) instead of exiting, so the restart loop cannot add attempts.
  - `python -m live.cpgw_keeper --reset-lockout` clears it after the user fixed the cause.
  - Success resets the counters.
- Paper account guard: after every login, `CPGWClient.list_accounts()` must return at least one account and every id must start with `paper_account_prefix`. Otherwise the keeper calls `logout()`, raises `NotPaperAccount`, locks out and alerts. Fails closed.

### F. Telegram notifier (`live/telegram_notifier.py`)

- `TelegramConfig.from_env()`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. If either is missing, the notifier is disabled: `send` returns False and one info event says so. It never raises.
- `TelegramNotifier(config, *, source, storage=None, transport=None, clock=None, state_path=None)`. It sends via `httpx.Client` (already a dependency) to `https://api.telegram.org/bot<token>/sendMessage` with `chat_id`, `text`, no `parse_mode` (plain text, no markup injection), timeout 10 s. Tests pass `httpx.MockTransport`, so no socket is opened.
- Every text goes through the `Redactor` (token, chat-id free), is truncated to 3500 characters and starts with a tag (`[tradingbot paper]`).
- `send(kind, text, *, dedupe_key=None, cooldown_sec=None) -> bool`. Kinds: `summary`, `order`, `error`, `session`, `health`.
  - Per-key cooldown: the same `dedupe_key` is not sent again within its cooldown (`session_lost` 3600 s, errors 900 s keyed by the first 80 characters of the message).
  - Global cap: at most 20 messages per hour per process; suppressed messages are counted and the next sent message says "(N alerts suppressed)".
  - State is persisted in `logs/telegram_state_<source>.json` (one file per process, no shared writes), so a restart loop does not resend.
- Send failures return False and log a redacted `warn` event. httpx error texts contain the URL with the token, so they are always redacted.
- `python -m live.telegram_notifier --test` sends one test message (manual server step).
- Gmail (Open question 4): `live/notifier.py` stays as is for `live/runner.py`. Telegram is used by the keeper, the portfolio runner and the sentiment worker. Merging the two is a follow-up issue.

Senders:

| Event | Where | Kind and dedupe |
|---|---|---|
| Daily summary | portfolio runner, first cycle each US Eastern date at or after 17:00 ET | `summary`, once per date (checked against a `telegram` event `daily_summary <date>` in SQLite) |
| Every order | `_execute_core_leg`, `_exec_satellite_leg` (dry-run, placed and failed) | `order`, no dedupe; `[DRY-RUN]` or `[PAPER]` tag (Open question 5) |
| Cycle error | `PortfolioRunner.loop` | `error`, deduped |
| Session lost, login needs a manual step, lockout, not a paper account, gateway down | keeper | `session`, deduped |
| Sentiment data missing | sentiment worker and `--check` | `health`, once per day per reason |

Daily summary contents: date, the equity reading and its source, holdings per sleeve, orders today (counts by status), error events today, keeper state (authenticated since, last login, lockout), sentiment health status, dry-run flag.

### G. Sentiment satellite off and the data health check

- `SentimentSatelliteConfig` gains `status: str = "active"` with `__post_init__` validation: `status` must be `active` or `research`, `allocation_pct` must be a number in [0, 1], and `research` requires `allocation_pct == 0`. A new property `tradeable` is `enabled and status == "active" and allocation_pct > 0`.
- `config/symbols.yaml`: `enabled: false`, `allocation_pct: 0.0`, `status: research`, with a comment that Alpha Vantage `NEWS_SENTIMENT` is now premium.
- `PortfolioRunner` builds the satellite and adds its universe only when `sent_cfg.tradeable`. The banner lists it as `sentiment (research, 0%)`. At start, if `storage.get_holdings("sentiment")` is not empty while the sleeve is not tradeable, it logs one `warn` event: "N simulated sentiment holdings are not managed" (Open question 9). No orders are sent for them.
- `core/sentiment_health.py`: `check_readings(provider_name, readings) -> HealthResult(ok, reason)` and `classify_error(exc) -> HealthResult`. The check fails when the provider raised (premium or throttle message, transport error), returned no readings, or every reading has `article_count == 0`. The stub provider gives `HealthResult(ok=True, reason="stub provider, nothing to check")`.
- `AlphaVantageProvider.fetch_batch` raises a new `SentimentDataUnavailable(RuntimeError)` for `Note` and `Information` payloads, keeping the API's message text but never the URL (the URL holds the API key).
- "Fails loudly" means all of:
  1. an `error` event with source `sentiment_health` and the reason;
  2. a Telegram `health` alert, once per day per reason;
  3. `python -m live.sentiment_worker --check` does one fetch, prints the result and exits 0 when healthy, 2 when not (stub exits 0 with its message);
  4. in the worker loop, a failed fetch writes no zero readings to the `sentiment` table.
- Compose (Open question 8): the `sentiment` service moves from profile `auto` to profile `research`, so `--profile auto` no longer polls a premium endpoint every hour.

### H. Docker images and memory

- `docker/Dockerfile` becomes multi-stage:
  - `deps` (current apt and `pip install -r requirements.txt`);
  - `keeper` (FROM `deps`, `pip install -r requirements-login.txt`, then `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright playwright install --with-deps --only-shell chromium` as root, then `COPY . /app`, user `bot`);
  - `app` (FROM `deps`, `COPY . /app`, user `bot`, current `CMD`), listed last so it stays the default target.
  - `requirements-login.txt` pins `playwright==<exact current version at implementation time>` (1.49 or newer for `--only-shell`).
  - The `chromium-headless-shell` build needs no X server or Xvfb. Playwright's default `chromiumSandbox: false` is kept, because the Chromium sandbox needs user namespaces that Docker blocks by default; the browser only ever opens the local gateway origin.
  - The keeper image grows by an estimated 300 to 450 MB (browser plus system libraries). The user measures it with `docker image ls`. The `app` image used by the other services does not change.
- Compose: `x-bot-common` gets `build.target: app`. `cpgw_keeper` overrides it with `target: keeper`, `image: tradingbot-keeper:local`, `secrets: [ibkr_login]`, the volume `ibkr_gateway_tls:/app/gateway_tls:ro` and `environment: CPGW_CA_FILE=/app/gateway_tls/cert.pem`. `portfolio` gets the same volume and variable.
- Memory, about 3.6 GB free on the server. Each service also gets `memswap_limit` equal to `mem_limit`, so it never swaps.

| Service | `mem_limit` | Why |
|---|---|---|
| `ibkr_gateway` | 768m | native gateway; GraalVM sizes its heap from the container limit |
| `cpgw_keeper` | 640m | Python about 100 MB idle, headless Chromium peak about 250 to 400 MB during a login, then closed |
| `portfolio` | 640m | pandas, exchange_calendars, xgboost import, 2 years of daily bars for about 20 symbols |
| `dashboard` | 512m | FastAPI plus pandas; it can spawn workers inside its own container |
| `sentiment` | 256m | one HTTP call per hour; profile `research`, normally off |

  Normal running total: 2560m. With `research` on: 2816m. Both leave at least about 800 MB for the host. The deploy steps check `docker stats` after the first day and lower or raise limits from real numbers.

- Training through the dashboard inside the 512m container will likely be killed. Training stays out of band, as today.

### I. CPGW client TLS

`CPGWClient.__init__` gains `ca_file: str | None = None`. When `None`, it reads `CPGW_CA_FILE`. When a path is set, `verify` becomes an `ssl.SSLContext` loaded from that file with hostname checking on (the cert has SAN `localhost` and `127.0.0.1`). When the variable is set but the file is missing, it raises a clear `CPGWError` (fails closed). Unset keeps `verify=False`, so the host Java gateway keeps working.

### J. CI check for private files

A new CI step before the tests:

```yaml
      - name: Private files are not tracked
        run: |
          tracked="$(git ls-files -- core/ibkr_auth_private.py secrets vendor \
            | grep -Ev '^(secrets/[^/]+\.example|secrets/README\.md)$' || true)"
          if [ -n "$tracked" ]; then
            echo "Private files are tracked by git:"
            echo "$tracked"
            exit 1
          fi
```

Also a pytest test (`tests/test_private_files.py`) that runs the same `git ls-files` through `subprocess` (local git, no network) and skips when `.git` is missing, plus `git check-ignore` assertions for `core/ibkr_auth_private.py`, `secrets/ibkr_login`, `secrets/gateway_ssl_password` and `vendor/ibkr-gateway/ibkr-gateway-linux-amd64.zip`.

### Rejected alternatives

- All login code only in the private file, tests skipped when it is absent: CI would never test the secret handling, 2FA detection or backoff.
- `ignore_https_errors=True` in Playwright: accepts any certificate, so any local process on port 5000 could collect the password.
- Keeping IBKR's `vertx.jks` and only changing `sslPwd`: the key is public; the password protects nothing.
- Generating the keystore with `keytool` from a JRE image: adds a second unpinned image and a manual step. Kept as the fallback if PKCS12 is rejected.
- Official `mcr.microsoft.com/playwright/python` image: about 1.5 GB or more with all browsers, too big.
- Xvfb with headed Chromium: the user ruled it out, and headless shell needs no display.
- Credentials in `.env`: loaded into and mounted in every container.
- Telegram via a third-party library: `httpx` already does it in a few lines and is easy to mock.
- One Telegram state file shared by all processes: concurrent writes race.
- Proactive logout and login before 24 h: adds a login per day and risks a lockout for no gain.
- Running the login in the gateway container: it would need Python and Chromium inside the hardened gateway image.

## 4. Tasks

Every task: write the tests first, then the code, then run `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .` and `.venv/bin/python -m pytest -q`. Never `uv run` or `uv sync`. One Conventional Commit per task.

### Task 1. Private files: ignore rules, placeholders, CI check

- Files: `.gitignore` (`secrets/*`, `!secrets/*.example`, `!secrets/README.md`, `vendor/`), `.dockerignore` (`secrets/`, `vendor/`), `secrets/README.md`, `secrets/ibkr_login.example`, `secrets/gateway_ssl_password.example`, `.github/workflows/ci.yml` (step from section J), `tests/conftest.py` (add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IBKR_LOGIN_SECRET_FILE`, `IBKR_USERNAME`, `IBKR_PASSWORD`, `CPGW_CA_FILE` to `_ENV_KEYS`), `tests/test_private_files.py` (new).
- Tests first: `git ls-files` finds no private file; `git check-ignore` matches each private path; the example files are not ignored and contain no plausible secret (no line matching `IBKR_PASSWORD=` followed by a non-placeholder); `.dockerignore` lists `secrets/` and `vendor/` and still keeps `core/ibkr_auth_private.py` (existing test stays green).
- Commit: `ci: fail when the private login module or secret files are tracked`.

independent

### Task 2. Move auth types out of the loader

- Files: `core/auth_types.py` (new), `core/ibkr_auth.py` (class definitions replaced by imports and re-exports, docstring notes the new exception classes; loader logic and ruff exclusion unchanged), `tests/test_auth_types.py` (new).
- Tests first: `repr()` and `str()` of `IbkrCredentials` contain neither the password nor the full username; every new exception subclasses `IBKRAuthError`; `core.ibkr_auth.IBKRAuthError is core.auth_types.IBKRAuthError` with the stub active; `import core.auth_types` imports nothing from `core`. All of `tests/test_ibkr_auth.py` passes unchanged.
- Commit: `refactor: move IBKR auth types into core/auth_types.py and hide the password in repr`.

independent

### Task 3. Secret file loader and redactor

- Files: `core/secret_store.py` (new), `tests/test_secret_store.py` (new).
- Tests first (with `tmp_path` and `os.chmod`, skipped on Windows):
  - mode 600 loads;
  - 640, 644 and 604 each raise `SecretFileError`;
  - a symlink, a directory, a missing file, a 5 KiB file and an unknown key each raise;
  - a missing key and an empty value raise;
  - a wrong owner raises (monkeypatch `os.geteuid`);
  - path resolution order (env, `/run/secrets` via monkeypatched path, repo default);
  - every error message contains neither value (assert on `str(exc)`, `repr(exc)` and `traceback.format_exception`);
  - `Redactor` masks registered values, Telegram URL tokens and cookie pairs, and leaves other text alone.
- Commit: `feat: load IBKR credentials from a chmod 600 secret file`.

depends on: 1, 2

### Task 4. Telegram notifier

- Files: `live/telegram_notifier.py` (new), `tests/test_telegram_notifier.py` (new).
- Tests first (all with `httpx.MockTransport` and a fixed clock):
  - disabled without token or chat id, never raises;
  - the request goes to the right path with `chat_id` and plain text and no `parse_mode`;
  - the token never appears in events, stdout or return values, even when the transport raises an error whose text holds the URL;
  - the same dedupe key inside the cooldown sends once; after the cooldown it sends again;
  - the global cap suppresses message 21 and the next message reports the count;
  - the state survives a new instance (persisted file);
  - long text is truncated;
  - `--test` CLI calls the sender once (injected).
- Commit: `feat: add a Telegram notifier with redaction, dedupe and a rate cap`.

depends on: 1, 3

### Task 5. Sentiment satellite to research status

- Files: `core/config.py` (`SentimentSatelliteConfig.status`, `__post_init__`, `tradeable`), `config/symbols.yaml` (sentiment block and header comment), `live/portfolio_runner.py` (use `tradeable` in `__init__`, `_all_required_symbols`, `start` banner; leftover holdings warning), `docs/TRADING_LOGIC.md` (split table: sentiment 0%, research; unallocated 30%; section 5 intro), `tests/test_sleeve_status.py` (new).
- Tests first:
  - `status: research` with `allocation_pct: 0.2` raises; an unknown status raises; a string allocation raises;
  - the shipped `config/symbols.yaml` loads with the satellite not tradeable;
  - a `PortfolioRunner` built like in `tests/test_portfolio_runner.py` has `satellite is None` and no satellite symbols;
  - leftover sentiment holdings produce exactly one warn event and no order rows;
  - core behaviour tests stay green.
- Commit: `feat: switch the sentiment satellite to research status with 0% allocation`.

independent

### Task 6. Keeper config block

- Files: `core/config.py` (`KeeperConfig`, `load_keeper_config`), `config/symbols.yaml` (`keeper:` block with the defaults from section E and comments), `tests/test_keeper_config.py` (new).
- Tests first: defaults load from the shipped YAML; a bad time string, an unknown time zone, `max_failed_logins: 0`, a backoff max below base, a boolean where a number is expected and an empty prefix each raise with the key name; a missing block gives the defaults.
- Commit: `feat: add keeper timing and lockout settings to the config`.

depends on: 5

### Task 7. Generic headless login flow

Needs the user's answer to Open question 1 first. If the user picks "all in the private file", this task becomes a private file plus `importorskip` tests, and Task 9 imports from the private file instead.

- Files: `core/gateway_login.py` (new), `requirements-login.txt` (new, `-r requirements.txt` plus pinned `playwright`), `deploy/ibkr_auth_private.py.example` (new), `tests/test_gateway_login.py` (new), `tests/fixtures/gateway_cert.pem` (new, a throwaway self-signed test cert generated once with openssl, public part only).
- Tests first (a `FakePage` implementing `LoginPage` records calls and returns scripted markers):
  - success requires `is_authenticated()` to be True; a success marker with False raises `ManualStepRequired`;
  - each marker maps to its exception;
  - `on_submit` is called exactly once and before `click(submit)`; no submit happens on `GatewayUnreachable` before the form;
  - the password is passed only to `fill(password_selector, ...)`;
  - no exception text, `repr` or formatted traceback contains the password or the username, including when `FakePage.fill` raises an error that contains the value;
  - `spki_pin_from_pem` on the fixture equals the value computed independently in the test with `cryptography`;
  - `GatewayLoginConfig.from_env()` without `CPGW_CA_FILE` makes `login_via_gateway_page` raise before any browser start;
  - after `import core.gateway_login`, `"playwright" not in sys.modules` (run in a subprocess for a clean interpreter);
  - the example file defines `login` with the documented signature (parse it with `ast`, do not import it).
- Commit: `feat: add a headless login flow for the gateway's own login page`.

depends on: 2, 3

### Task 8. Keeper session policy

- Files: `live/keeper_policy.py` (new), `tests/test_keeper_policy.py` (new).
- Tests first (fixed aware datetimes):
  - authenticated gives `TICKLE`;
  - a drop at 00:50 local is `WAIT` until 01:30, then `LOGIN`;
  - a drop on Saturday is `WAIT` until Sunday 00:00 local;
  - window edges are exact (start inclusive, end exclusive);
  - a DST change night in `America/New_York` is handled;
  - a drop after 24 h is `LOGIN` at once and tagged expected;
  - the backoff sequence is 60, 120, 240, ... capped at 1800;
  - three submitted failures inside 24 h give `LOCKED_OUT`, and failures older than the window do not count;
  - each of `BadCredentials`, `SecondFactorRequired`, `ManualStepRequired`, `NotPaperAccount` locks out after one attempt;
  - `GATEWAY_DOWN` never gives `LOGIN`;
  - state round-trips through JSON, the file is written with mode 600, and a corrupt file gives a fresh state plus a warning (never an unlock of a recorded lockout: a corrupt file is treated as locked out until `--reset-lockout`).
- Commit: `feat: add a pure session policy for the keeper`.

depends on: 6

### Task 9. Keeper wiring

- Files: `live/cpgw_keeper.py`, `tests/test_cpgw_keeper.py` (new).
- Changes:
  - `_read_credentials` is replaced by `load_login_secrets`;
  - refuse to start when `IBKR_PASSWORD` is in env;
  - the banner masks the username;
  - `start()` checks gateway auth before any login;
  - the loop asks `decide()` and acts;
  - every submit is recorded in the state before the click (`on_submit`);
  - a successful login is confirmed by `auth_status`, the paper account guard runs, then the session file is written (an empty cookie is allowed);
  - Telegram alerts as in section F;
  - all event texts pass the `Redactor`;
  - the tickle interval comes from `KeeperConfig`;
  - new flag `--reset-lockout`;
  - login failures no longer raise out of `start()`.
- Tests first (fake `CPGWClient`, fake `login`, fake notifier, temp `logs/` dir, fake clock):
  - an already authenticated gateway means no login;
  - a drop then success writes the session and sends one info alert;
  - `SecondFactorRequired` sends one alert, locks out, and a new `Keeper` instance (restart) does not call `login`;
  - `--reset-lockout` clears the state;
  - a non-`DU` account calls `logout`, locks out and alerts;
  - an env password makes start fail with a message that does not contain it;
  - after a scripted failure run, the password and username appear nowhere in captured stdout, stderr, fake storage events or notifier texts;
  - the existing `--once` path still works.
- Commit: `feat: keeper logs in again after resets with backoff, lockout guard and alerts`.

depends on: 3, 4, 7, 8

### Task 10. Optional pinned TLS in the CPGW client

- Files: `core/cpgw_client.py`, `tests/test_cpgw_client_tls.py` (new).
- Tests first: without `CPGW_CA_FILE` the client still uses `verify=False` (inspect the transport's SSL context or a monkeypatched `httpx.Client` call); with the fixture cert it passes an `SSLContext` with `check_hostname` on; a set but missing path raises `CPGWError`. Uses `tests/fixtures/gateway_cert.pem` from Task 7.
- Commit: `feat: verify the gateway certificate when CPGW_CA_FILE is set`.

depends on: 7

### Task 11. Sentiment data health check

- Files: `core/sentiment_health.py` (new), `core/sentiment.py` (`SentimentDataUnavailable`), `live/sentiment_worker.py` (`--check`, health check in `_tick`, no zero rows on failure, Telegram `health` alert), `tests/test_sentiment_health.py` (new), `tests/test_sentiment_worker.py` (new).
- Tests first:
  - an `Information` payload (premium text) and a `Note` payload raise `SentimentDataUnavailable`, and the message contains no `apikey`;
  - an empty feed and all-zero article counts fail the check;
  - stub passes with its message;
  - `main(["--check"])` returns 2 on failure and 0 on success with a fake provider;
  - the loop writes an `error` event with source `sentiment_health` and no `sentiment` rows on failure;
  - the notifier is called once per day per reason;
  - the existing fixture test in `tests/test_sentiment.py` stays green.
- Commit: `feat: fail loudly when the sentiment provider returns no data`.

depends on: 4

### Task 12. Portfolio runner notifications and daily summary

- Files: `live/portfolio_runner.py` (notifier in `__init__`, `_notify_order` after each `log_order` in both legs, error alert in `loop`, `_maybe_send_daily_summary(now)` at the end of `_cycle`, `start()` accepts an empty cookie when the session file exists), `live/storage.py` (`has_event(source, message, since)` or `last_event(source, prefix)` helper, read only, no schema change), `tests/test_portfolio_notify.py` (new).
- Tests first (fake notifier injected):
  - a dry-run core rebalance sends one `[DRY-RUN]` message per order;
  - a failed placement sends a `[PAPER] FAILED` message with redacted, truncated error text;
  - the summary is sent once per Eastern date at or after 17:00 ET, not at 16:59, and not twice after a restart (event in SQLite);
  - summary text contains counts and no cookie;
  - a cycle error sends one deduped alert;
  - an empty cookie session starts the runner;
  - `--live` and dry-run flags are unchanged (existing `parse_args` tests stay green).
- Commit: `feat: send order, error and daily summary alerts from the portfolio runner`.

depends on: 4, 5

### Task 13. Gateway: vendored release, own keystore, sslPwd from a secret, exact allow list

- Files: `scripts/fetch_gateway.sh` (new), `docker/gateway/gateway.sha256` (new), `docker/gateway/Dockerfile` (fetch stage copies from the `vendor` context and runs `sha256sum -c`; runtime stage installs `openssl`, removes both default `vertx.jks`, creates `tls/` and `run/` owned by `gateway`, `ENTRYPOINT ["./entrypoint.sh"]`), `docker/gateway/entrypoint.sh` (new), `docker/gateway/conf.yaml` (template), `docker-compose.yml` (gateway service only: `additional_contexts`, `secrets`, `tmpfs`, `ibkr_gateway_tls` volume, top-level `secrets:` and `volumes:`), `tests/test_deploy_config.py`.
- Tests first:
  - the hash file equals the pinned SHA-256 and the asset name;
  - the Dockerfile has no `curl` of a GitHub URL, copies from `vendor`, runs `sha256sum -c gateway.sha256`, deletes `vertx.jks`, has a `USER` line and digest-pinned `FROM` lines;
  - the fetch script has `set -euo pipefail`, reads `docker/gateway/gateway.sha256`, writes under `vendor/ibkr-gateway/` and has no `unzip`;
  - the entrypoint uses `-passin file:` and `-passout file:`, never `set -x`, never `echo`s the password variable, and checks the password pattern;
  - `conf.yaml` has `sslPwd: "__SSL_PWD__"`, no `mywebapi`, and `ips.allow == ["127.0.0.1", "172.31.250.1"]`, inside the compose subnet;
  - the gateway service mounts the secret, the tmpfs and the volume, and keeps the loopback publish and lockdown.
  - Existing gateway tests are updated to the new paths, not deleted.
- Commit: `feat: gateway uses a vendored pinned release, its own TLS key and a secret sslPwd`.

depends on: 1

### Task 14. Keeper image, compose secrets and memory limits

- Files: `docker/Dockerfile` (`deps`, `keeper`, `app` stages), `docker-compose.yml` (bot services: `target`, keeper image, `ibkr_login` secret, tls volume and `CPGW_CA_FILE` for keeper and portfolio, `mem_limit` and `memswap_limit` everywhere, `sentiment` to profile `research`, header comments without `IBKR_USERNAME`), `.env.example` (remove `IBKR_USERNAME`, `IBKR_PASSWORD`; add `TELEGRAM_BOT_TOKEN=`, `TELEGRAM_CHAT_ID=`, `IBKR_LOGIN_SECRET_FILE=`, `CPGW_CA_FILE=` with comments), `tests/test_deploy_config.py`.
- Tests first:
  - every service has `mem_limit` equal to the table in section H and `memswap_limit` equal to it, and the sum of the services in `auto` plus `gateway` is at most 2560m;
  - only `cpgw_keeper` has the `ibkr_login` secret and the `keeper` target;
  - the Dockerfile has exactly the three named stages and `app` is last;
  - the keeper stage installs with `--only-shell chromium` and never mentions `xvfb`;
  - no compose file or Dockerfile contains `--live`, `0.0.0.0`, `IBKR_PASSWORD` or `xvfb`;
  - `.env.example` keeps `PAPER=true`, `DRY_RUN=true`, `IB_PORT=5000`, `DASHBOARD_HOST=127.0.0.1` and has no `IBKR_PASSWORD`;
  - the existing `test_portfolio_command_is_dry_run` and `test_bot_services_keep_host_network` stay green.
- Commit: `feat: keeper image with headless Chromium, compose secrets and memory limits`.

depends on: 7, 13

### Task 15. Docs, ADR and changelog

- Files: `deploy/README.md` (new section "Phase 1 upgrade (paper account only)", text in section 5 below; update section 0A steps 3, 4 and 8 to the secret file and the new private file), `SECURITY.md` (secrets, keystore, Telegram token, redaction, lockout, paper guard), `docs/TECH_STACK.md` (Playwright, Telegram, keeper policy, image targets), `docs/adr/0003-headless-gateway-login-and-file-secrets.md` (new), `CHANGELOG.md`, `README.md` (only if it mentions `IBKR_PASSWORD` in `.env`; check with `rg`).
- Tests: `tests/test_deploy_config.py` gets a check that `deploy/README.md` mentions `deploy/backup.sh` before the first `docker compose` command of the new section, and no `--live`. Run ruff, because ruff 0.17.4 formats Python blocks inside Markdown.
- Commit: `docs: phase 1 server steps, ADR 0003 and changelog`.

depends on: 9, 10, 11, 12, 13, 14

### Task 16. Security review (no new features)

- The lead runs `kit:security-reviewer` on:
  - `core/gateway_login.py`, `core/secret_store.py`, `core/auth_types.py`, `core/ibkr_auth.py`;
  - `live/cpgw_keeper.py`, `live/keeper_policy.py`, `live/telegram_notifier.py`;
  - `deploy/ibkr_auth_private.py.example`;
  - `docker/gateway/` (Dockerfile, `entrypoint.sh`, `conf.yaml`, `gateway.sha256`), `scripts/fetch_gateway.sh`, `docker/Dockerfile`, `docker-compose.yml`, `.gitignore`, `.dockerignore`, `.github/workflows/ci.yml`.
- The user's real `core/ibkr_auth_private.py` is not in the repo. It should match the example; the user confirms that.
- Each finding is fixed in its own `fix:` commit with a regression test, or recorded as a follow-up issue with the reason. No ship before this is done.

depends on: 9, 10, 11, 12, 13, 14

### Task 17 (needs the user's approval). Project CLAUDE.md

CLAUDE.md is project configuration, so no agent edits it without the user's yes. Proposed changes for the user:

- Gotchas: credentials now live in `secrets/ibkr_login` (not `.env`); `core/ibkr_auth_private.py` is a thin wiring file over `core/gateway_login.py`.
- Gotchas: run `scripts/fetch_gateway.sh` before building the gateway.
- Gotchas: Telegram variables.
- Gotchas: the `sentiment` service is in profile `research`.
- Gotchas: `--reset-lockout`.
- Architecture: list the new modules.
- Test list: add the new areas.

depends on: 15

## 5. Tests

Write first, in this order: `tests/test_private_files.py`, `tests/test_auth_types.py`, `tests/test_secret_store.py`, then the notifier, policy and login flow tests, which only need fakes. The keeper wiring tests come last because they combine the others.

Secret leak tests (shared helper `tests/leak.py` with `assert_no_secret(texts, secrets)`):

- Fixed fake values: `FAKE_USER = "paperuser-x1"`, `FAKE_PASSWORD = "not-a-real-password-7f3a"`, `FAKE_TOKEN = "123456:fake-telegram-token"`, `FAKE_COOKIE = "x-sess-uuid=fake"`.
- Collected text: `capsys` output, `caplog` records, every message passed to a fake `Storage.log_event`, every text the fake Telegram transport receives, `str`, `repr` and `traceback.format_exception` of every raised error, and the written `logs/*.json` files.
- Scenarios: bad secret file, bad credentials, second factor, Playwright error containing the value, transport error containing the Telegram URL, paper guard failure.

Edge cases:

- Window boundaries and DST nights.
- A restart while locked out.
- A corrupt state file.
- Two keepers (dashboard-spawned plus the container) are out of scope, but the lockout file still blocks both.
- An empty cookie.
- A cert with a different key: the pin differs, so the test asserts the pin changes.
- Alpha Vantage premium text.
- A secret file with CRLF line endings: accepted, the CR is stripped.
- A trailing newline in the hex password file.

Fixtures:

- `tests/fixtures/gateway_cert.pem` (public cert only, generated once, documented in a comment in the test).
- `tests/fixtures/alpha_vantage_premium.json` (a hand-written `Information` payload, not copied from the API).

Manual checks the user runs on the server (section 5 of the deploy doc) are the only checks of the real login page, the real gateway TLS and `--conf` handling. They are listed in Risks.

### Server steps for `deploy/README.md` (Task 15 writes this text)

All on the server over SSH unless marked laptop. Paper account only.

1. Backup first:
   - run `~/trading-bot/deploy/backup.sh`;
   - then `umask 077; mkdir -p ~/backups/tradingbot/manual-$(date -u +%Y%m%d)`;
   - copy `.env`, `core/ibkr_auth_private.py` and `git rev-parse HEAD` output there;
   - `ls -l` the folder to check the files are mode 600.
2. Check memory and the current state: `free -m` (expect about 3.6 GB available), `docker stats --no-stream`, `docker compose --profile gateway --profile auto ps`.
3. Stop the stack: `docker compose --profile gateway --profile auto down`.
4. Update the code: `git fetch && git checkout main && git pull` (after the user merged the PR).
5. Fetch the gateway: `scripts/fetch_gateway.sh`. It must print the hash check OK. Then `git status --short` must print nothing.
6. Secrets:
   - `mkdir -p secrets && chmod 700 secrets`;
   - `umask 077; cp secrets/ibkr_login.example secrets/ibkr_login; nano secrets/ibkr_login` (paper username and password);
   - `openssl rand -hex 24 > secrets/gateway_ssl_password`;
   - `sudo chown 10001:10001 secrets/gateway_ssl_password`;
   - `chmod 600 secrets/ibkr_login`;
   - `git check-ignore -v secrets/ibkr_login secrets/gateway_ssl_password`, which must print the rules.
7. `.env`: delete the `IBKR_USERNAME` and `IBKR_PASSWORD` lines, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (the user creates the bot with BotFather on the phone).
8. Private module: replace it with the content of `deploy/ibkr_auth_private.py.example`, then `chmod 600 core/ibkr_auth_private.py`.
9. Gateway:
   - `docker compose --profile gateway up -d --build ibkr_gateway`, then wait for healthy;
   - `curl -sk https://127.0.0.1:5000/ | head -c 300` must not say "Access Denied";
   - `openssl s_client -connect 127.0.0.1:5000 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256` must match `openssl x509 -in <(docker compose --profile gateway exec ibkr_gateway cat tls/cert.pem) -noout -fingerprint -sha256`;
   - the laptop LAN and tailnet checks from section 0A step 5 must still fail to connect.
10. Telegram test: `docker compose run --rm portfolio python -m live.telegram_notifier --test`. Check the message on the phone.
11. Build the stack: `docker compose --profile gateway --profile auto build`, then `docker image ls | grep tradingbot` and note the keeper image size.
12. First login, watched:
    - `docker compose --profile gateway --profile auto run --rm cpgw_keeper python -m live.cpgw_keeper --once`;
    - expect `auth=private`, a masked username and a `DU` account;
    - leak check: `docker compose logs cpgw_keeper | grep -c -F -f <(sed -n 's/^IBKR_PASSWORD=//p' secrets/ibkr_login)` must print 0. Process substitution keeps the password out of the command line and shell history.
13. Start: `docker compose --profile gateway --profile auto up -d`, then `docker stats --no-stream` (every service under its limit) and `curl -fsS http://127.0.0.1:8765/api/health` (`paper` true, `dry_run` true, `ib_port` 5000).
14. Sentiment check: `docker compose --profile research run --rm sentiment python -m live.sentiment_worker --check`. With Alpha Vantage it is expected to exit 2 and send one Telegram health alert. That proves the loud failure.
15. Watch the first night (the 01:00 gap), the first Saturday and the first 24 h reset in the dashboard events and Telegram. If the keeper locks out: read the alert, fix the cause, then `docker compose run --rm cpgw_keeper python -m live.cpgw_keeper --reset-lockout`.
16. After a day: `docker stats --no-stream` again; adjust `mem_limit` if a service sits above 80%.

Rollback:

- `docker compose --profile gateway --profile auto down`;
- `git checkout <commit from step 1>`;
- restore `.env` and the old private module from the step 1 folder;
- `docker compose --profile gateway --profile auto up -d --build`.

The `ibkr_gateway_tls` volume and `secrets/` can stay; the old code ignores them.

## 6. Risks

- Login page markup is unknown offline. The selectors and markers are defaults confirmed in step 12. If they are wrong, the first attempt ends in `ManualStepRequired` and locks out after one attempt, which is safe but needs a selector override in the private file and `--reset-lockout`.
- Account lockout. Mitigations: never log in while authenticated, no logins in the windows or while the gateway is down, at most 3 submitted attempts per 24 h, a persisted lockout that survives restarts, immediate lockout on bad credentials or a second factor.
- PKCS12 keystore. If the native gateway does not accept a PKCS12 file as `sslCert`, TLS does not start (fails closed, nothing exposed). Fallback: generate a JKS with `keytool` from a digest-pinned JRE image in a one-off container, as a follow-up task.
- `--conf ../run/conf.yaml` and the relative `sslCert` path are as unverified as the current `--conf` (ADR 0001). Both fail closed. Step 9 checks them.
- Exact allow list. If Docker's proxy source is not `172.31.250.1` on the server, the gateway answers "Access Denied" (fails closed). Fallback: `172.31.250.*`, with the user's approval.
- SPKI pin flag. If the pinned Chromium build ignores `--ignore-certificate-errors-spki-list` in headless shell, navigation fails with a certificate error (fails closed). Fallback: a follow-up decision, never `ignore_https_errors`.
- Python 3.13 strict X.509 checks could reject the self-signed cert in `CPGWClient`. The project runs 3.11 and 3.12; the entrypoint adds the extensions strict mode wants.
- Memory. Chromium peaks during a login. An OOM kill during a login counts as a submitted attempt (it is recorded before the click), which is the conservative side. Watch `docker stats` and the kernel log (`journalctl -k | grep -i oom`).
- Supply chain: Playwright downloads Chromium at image build time from Playwright's CDN. The version is pinned through the `playwright` package pin; no per-file hash. Stated in ADR 0003. The gateway binary risk from ADR 0001 is unchanged; vendoring adds no new trust.
- Secrets in the image. `.dockerignore` excludes `secrets/` (tested). The private module stays in the image as before; it now holds no secret.
- Compose may refuse to start if a declared secret file is missing, even for an unrelated service. The deploy steps create the files first; `secrets/README.md` says so.
- Telegram is an outward-facing service. Only redacted summaries leave the server. The token in `.env` is still visible to every container (follow-up: `TELEGRAM_BOT_TOKEN_FILE`).
- Deployment is manual and reversible (rollback above). There is no database migration: `logs/bot.sqlite` gains no table; the new state files are created on first run.
- The cookie in `logs/cpgw_session.json` is still in the nightly backup (unchanged, documented in SECURITY.md).
- Gateway CORS stays `origin.allowed: "*"` (IBKR default). The reviewer in Task 16 decides whether to narrow it; that is a follow-up if it needs testing on the server.

## 7. CHANGELOG lines (`[Unreleased]`)

Added:

- Headless login to the gateway's own login page with Playwright (`core/gateway_login.py`), wired through the private `core/ibkr_auth_private.py`. No X server or Xvfb.
- Credentials from a chmod 600 secret file or a Docker secret (`secrets/ibkr_login`); the keeper refuses group or world readable files.
- Keeper session policy: tickle every 60 s, log in again after the nightly gap, Saturday maintenance and the 24 h reset, backoff capped at 30 minutes, and a persisted lockout after 3 failed logins in 24 h or any second-factor prompt. New flag `--reset-lockout`.
- Paper account guard: the keeper logs out and stops when an account id does not start with `DU`.
- Telegram notifier (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`): daily summary, every order, errors, session lost, sentiment data missing. Deduped and rate capped.
- `python -m live.sentiment_worker --check` exits 2 when the provider returns no data.
- CI step that fails when the private login module or files under `secrets/` or `vendor/` are tracked.
- `scripts/fetch_gateway.sh` downloads the pinned gateway release into the git-ignored `vendor/` folder.
- Memory limits for every compose service.
- ADR 0003: headless gateway login and file secrets.

Changed:

- The sentiment satellite is in `status: research` with `allocation_pct: 0`, because Alpha Vantage `NEWS_SENTIMENT` is now premium. 30% of equity stays unallocated.
- The `sentiment` compose service moved to profile `research`.
- The gateway uses its own TLS key, `sslPwd` from a Docker secret, and allows only `127.0.0.1` and `172.31.250.1`.
- The keeper runs from its own image target with headless Chromium.
- `CPGWClient` verifies the gateway certificate when `CPGW_CA_FILE` is set.
- `IbkrCredentials` hides the password in `repr`. The keeper banner masks the username.
- **Upgrade note:** move `IBKR_USERNAME` and `IBKR_PASSWORD` from `.env` into `secrets/ibkr_login` (chmod 600), create `secrets/gateway_ssl_password`, run `scripts/fetch_gateway.sh`, and replace `core/ibkr_auth_private.py` with `deploy/ibkr_auth_private.py.example`. See `deploy/README.md`.

Security:

- No secret in logs, events, exceptions or Telegram messages, with tests for each path.
- IBKR's shared default gateway keystore is no longer used.

## 8. ADR 0003 outline (`docs/adr/0003-headless-gateway-login-and-file-secrets.md`)

- Status: Proposed, then Accepted when the user approves this plan.
- Context: unattended paper login on a headless server, the public default keystore, credentials in `.env` visible to every container, lockout risk, CI cannot see the private file.
- Decision: sections A to E of this plan in short form, including the tracked or private split chosen in Open question 1.
- Consequences: a bigger keeper image, a manual vendor step, unverified page selectors, PKCS12 and SPKI pin checks on the server, lockout needs a manual reset.
- Alternatives: the rejected list from section 3.

## 9. Open questions (with recommended defaults)

1. Login code location. A: generic flow in tracked `core/gateway_login.py` with offline tests, private file is about 5 lines of wiring (Recommended: CI tests the parts that keep secrets safe). B: everything in the private file, with tests that `importorskip` it (CI never runs them). Blocks Task 7.
2. Allow list. A: exact `172.31.250.1` (Recommended: smallest surface, fails closed). B: `172.31.250.*` as today.
3. `IBKR_PASSWORD` in `.env`. A: the keeper refuses to start until it is removed (Recommended: forces the move out of a file every container sees). B: warn and ignore.
4. Gmail. A: keep for `live/runner.py`, Telegram everywhere else (Recommended: no behaviour removed). B: replace Gmail with Telegram now.
5. Order alerts. A: every order including dry-run, tagged (Recommended: in dry run the user still sees what would trade; volume is a few orders a week). B: only `--live` paper orders.
6. Keeper windows. Default `America/New_York`, nightly gap 00:45 to 01:30, Saturday 00:00 to 23:59. The user knows which time zone their gateway's resets follow; if unsure, keep the defaults (wide windows only delay logins, they never add attempts).
7. Paper account guard on `DU`. A: on (Recommended: a second, independent check against connecting to a real account). B: off.
8. `sentiment` service. A: profile `research` (Recommended: no hourly calls to a premium endpoint). B: stay in `auto` with the stub provider.
9. Old simulated sentiment holdings. A: warn once at start (Recommended: no data deleted). B: delete them with a one-off command.
10. Gateway binary. A: `vendor/` plus `additional_contexts`, hash checked twice (Recommended: the user asked for it; no build-time download). B: keep the build-time download.
11. Login failure budget: 3 submitted attempts per 24 h (Recommended). Lower is safer, higher retries more.
12. Username in logs: masked (Recommended), or shown in full as today.

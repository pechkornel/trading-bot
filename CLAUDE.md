# Trading bot

Self-hosted, paper-trading-only portfolio bot for Interactive Brokers (Client Portal Gateway). A multi-sleeve portfolio (Core ETF allocation 70%, Sentiment satellite in `status: research` at 0%, Tactical XGBoost/LSTM, off by default) with a FastAPI + HTMX dashboard. Rules are in `docs/TRADING_LOGIC.md`, library choices in `docs/TECH_STACK.md`.

## Commands

`pyproject.toml` holds ruff and pytest config only (no `[project]` table). ruff 0.17.4 is pinned in `requirements-dev.txt`, and `core/ibkr_auth.py` is excluded from ruff. CI in `.github/workflows/ci.yml` runs `ruff format --check`, `ruff check` and pytest on Python 3.11 and 3.12 (push to main, pull requests, weekly, manual). CI also fails if `core/ibkr_auth_private.py`, anything in `secrets/` (except `*.example` and `README.md`) or `vendor/` is tracked. Dependencies live in `requirements.txt`, `requirements-train.txt`, `requirements-dev.txt` (pytest, ruff), `requirements-login.txt` (keeper image only, pins Playwright) and `requirements-backtest.txt`. Tests are in `tests/` (see Gotchas for the areas).

```sh
# Setup (Windows venv python: .venv\Scripts\python, Linux: .venv/bin/python)
uv venv --python 3.11
uv pip install -r requirements.txt
uv pip install -r requirements-train.txt   # only for LSTM training (adds TensorFlow)
uv pip install -r requirements-dev.txt     # pytest and ruff
cp .env.example .env                       # then fill in your own values

# Lint, format and tests (Windows: .venv\Scripts\...)
.venv/bin/ruff format .                    # apply formatting
# CI runs exactly these three:
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/python -m pytest -q

# Portfolio runner (dry run is the default; --live sends paper orders)
python -m live.portfolio_runner --once               # one dry-run cycle of every enabled sleeve
python -m live.portfolio_runner [--interval SEC] [--live]

# Other long-running workers
python -m live.cpgw_keeper [--once]        # CPGW login + keepalive, writes logs/cpgw_session.json
python -m live.cpgw_keeper --reset-lockout # clear a login lockout after fixing the cause (logs/keeper_state.json)
python -m live.sentiment_worker [--once] [--interval SEC]
python -m live.sentiment_worker --check    # data health check: exit 0 healthy, 2 no data
python -m live.telegram_notifier --test    # send one Telegram test message (needs TELEGRAM_* in .env)
python -m live.runner --symbol SPY --model <registry_name> [--quantity N] [--live]   # single-symbol tactical runner

# Dashboard (port 8765 everywhere: README, Dockerfile, docker-compose, systemd, deploy docs; loopback by default)
uvicorn dashboard.app:app --host 127.0.0.1 --port 8765

# Training (needs a valid CPGW session first, see Gotchas)
python -m scripts.train_symbol --symbol SPY --kind xgboost [--classifier binary|multiclass] [--name NAME] [--no-smote]
python -m scripts.train_symbol --symbol SPY --kind lstm
python -m scripts.inspect_legacy [--file NAME]       # inspect models/artifacts/legacy/*

# Docker (home server)
docker compose up -d --build                         # dashboard only
scripts/fetch_gateway.sh                             # once, before building the gateway: pinned zip into vendor/
docker compose --profile gateway --profile auto up -d --build   # gateway + dashboard + cpgw_keeper + portfolio
docker compose --profile research up -d sentiment    # sentiment worker (research only, off by default)
docker compose --profile auto logs -f portfolio
```

Run all `python -m ...` commands from the repo root (modules import as `core.*`, `live.*`, etc.).

## Architecture

```
config/symbols.yaml   universe, per-symbol overrides, portfolio and sleeve settings (loaded by core/config.py)
core/                 config.py (env + yaml, assert_paper_mode, KeeperConfig), cpgw_client.py (HTTPS client for CPGW, CPGW_CA_FILE pin),
                      ibkr_auth.py (stub + private loader), auth_types.py (auth errors, IbkrCredentials),
                      secret_store.py (chmod 600 secret file loader, Redactor), gateway_login.py (headless Playwright login flow),
                      indicators.py, features.py, scoring.py, macro.py, sentiment.py, sentiment_health.py,
                      signal_fusion.py, sizing.py, market_hours.py, sleeves/{core,sentiment_satellite,tactical,long_term}.py
models/               train.py (train_xgboost / train_lstm), registry.py (save/load), artifacts/<name>/{model.pkl|model.keras,scaler.gz,metadata.json}
live/                 portfolio_runner.py (main loop), runner.py (single symbol), cpgw_keeper.py, keeper_policy.py (pure session
                      and lockout rules), sentiment_worker.py, storage.py (SQLite at logs/bot.sqlite), account_equity.py,
                      telegram_notifier.py (Telegram alerts), notifier.py (Gmail SMTP, live/runner.py only)
docker/               Dockerfile (stages deps, keeper with headless Chromium, app), gateway/ (Dockerfile, entrypoint.sh, conf.yaml, gateway.sha256)
secrets/              ibkr_login and gateway_ssl_password (git-ignored, filled by hand); only *.example and README.md are tracked
dashboard/            app.py (FastAPI routes, /api/health), process.py (spawns runner/keeper/worker as subprocesses), templates/, static/
scripts/              train_symbol.py, inspect_legacy.py, fetch_gateway.sh
```

Data flow: CPGW daily bars and Alpha Vantage news go through `core` (indicators, features, scoring, sentiment) into each sleeve in `core/sleeves/`. Sleeves produce target weights, `core/sizing.py` applies volatility targeting and caps, and `live/portfolio_runner.py` turns targets into orders (logged only unless `--live`). Everything is written to SQLite via `live/storage.py`, and the dashboard reads that DB and can start or stop the workers. The tactical sleeve loads models through `models/registry.py` and fuses model confidence with sentiment in `core/signal_fusion.py`.

## Gotchas

- Safety: `PAPER=true` and `IB_PORT=5000` are both required. `core.config.assert_paper_mode()` raises before any broker connection otherwise (only 5000, the Client Portal Gateway port, is allowed: `PAPER_PORTS = {5000}`). `DRY_RUN` defaults to true, and orders are sent only when `--live` is passed. The dashboard still starts when the lock fails, so you can see the misconfiguration.
- The IBKR Client Portal Gateway listens on `https://localhost:5000`, loopback only. Either run IBKR's Java daemon separately on the host, or run the `ibkr_gateway` compose service (profile `gateway`, see Deployment). Use one or the other, both want port 5000. Container bot services use `network_mode: host` to reach it.
- `core/ibkr_auth.py` is a stub in this public repo (the SRP login is not published). At the bottom of the module it imports `core.ibkr_auth_private` if that git-ignored file exists, and its `login(creds, *, port, **kwargs) -> str` replaces the stub. Only a missing `core.ibkr_auth_private` falls back to the stub: if the file exists but fails to import, the error is raised. `core.ibkr_auth.AUTH_BACKEND` is `"private"` or `"stub"`, and the keeper banner prints `auth=<backend>`. Never commit the private file or `git add -f` it. The private file is thin wiring copied from `deploy/ibkr_auth_private.py.example`: the login flow itself is the tracked, tested `core/gateway_login.py` (headless Chromium on the gateway's own login page, no X server or Xvfb, Playwright imported lazily so only the keeper image needs it). With the stub, the keeper treats the login as a manual step and locks out. Training and the runners need `logs/cpgw_session.json` written by the keeper (the cookie may be empty).
- Credentials live only in `secrets/ibkr_login` (`IBKR_USERNAME=` and `IBKR_PASSWORD=` lines), mode 600 and owned by the reading uid (1000 in the keeper container), or a Docker secret at `/run/secrets/ibkr_login`, or the path in `IBKR_LOGIN_SECRET_FILE`. `core/secret_store.py` refuses anything else. The keeper refuses to start if `IBKR_PASSWORD` is set in the environment. No secret may reach logs, events, exceptions or Telegram: use the `Redactor` and test it.
- Keeper login safety (`live/keeper_policy.py`, `keeper:` block in `config/symbols.yaml`): it never logs in while the gateway is authenticated (checked with and without the stored cookie) or down, waits out the nightly gap and Saturday maintenance (America/New_York), backs off 60 s to 30 min, and locks out after 3 failed submits or 6 total logins in 24 h, or at once on bad credentials, a second factor, a manual step or a non-`DU` account. The lockout is saved in `logs/keeper_state.json` and survives restarts; a corrupt file counts as locked out. Clear it with `--reset-lockout`.
- `.env` variable names (see `.env.example`): `PAPER`, `DRY_RUN`, `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`, `IBKR_LOGIN_SECRET_FILE`, `CPGW_CA_FILE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_RECIPIENTS`, `ALPHA_VANTAGE_API_KEY`, `SENTIMENT_PROVIDER`, `DASHBOARD_HOST`. Telegram is off when the token or chat id is empty. The Alpha Vantage key is only needed if the effective sentiment provider is `alpha_vantage`; use the `stub` provider for testing.
- `SENTIMENT_PROVIDER` (`stub` or `alpha_vantage`) overrides `provider` in the `sentiment_provider:` block of `config/symbols.yaml`, read by `core.config.load_sentiment_config`. Empty or unset uses the YAML. Any other value raises `ValueError`, so the worker and runner fail at start.
- `DASHBOARD_HOST` is read by the launchers (compose, the Dockerfile `CMD`, the systemd dashboard unit), not by Python. Default `127.0.0.1`. Only `127.0.0.1` and `0.0.0.0` are supported, and `0.0.0.0` is not recommended because the dashboard has no auth and compose uses `network_mode: host`.
- The sentiment satellite is `status: research` with `allocation_pct: 0` (Alpha Vantage `NEWS_SENTIMENT` is premium), so 30% of equity stays unallocated and the `sentiment` compose service is in profile `research`. `live.sentiment_worker --check` exits 2 when the provider returns no data.
- Git-ignored: `.env`, `core/ibkr_auth_private.py`, `secrets/*` (except `*.example` and `README.md`), `vendor/`, `logs/*.json`, `.venv/`, `logs/*.sqlite`, `logs/*.log`, `models/artifacts/*/model.pkl`, `model.keras`, `scaler.gz`. A fresh clone has no trained models and no `logs/` directory, so train or copy artifacts before enabling the tactical sleeve. Only `metadata.json` and the legacy README are tracked.
- Legacy models in `models/artifacts/legacy/` have unknown provenance (no registry metadata); treat them as baselines, not production models.
- TensorFlow is only in `requirements-train.txt`. The Docker image installs `requirements.txt` only, so LSTM models cannot be loaded or trained there.
- The test suite (`.venv/bin/python -m pytest -q`) covers scoring, the sleeves and sleeve status, signal fusion, sizing, Alpha Vantage parsing and the sentiment health check, the paper lock, the backtest, the secret store, the login flow (with a fake page), the keeper policy and wiring, the Telegram notifier, runner notifications, CPGW client TLS, private-file tracking and the deploy files (compose, Dockerfiles, gateway config, deploy README). It runs offline: `tests/conftest.py` blocks the network and isolates `.env`. Also verify changes by running a dry-run cycle (`--once`) against a paper gateway, never `--live`.
- `pyproject.toml` exists without a `[project]` table, so `uv run` and `uv sync` treat the repo as a project and create a `uv.lock`. Use `.venv/bin/python` and `.venv/bin/ruff` (Windows: `.venv\Scripts\...`) instead, and do not commit `uv.lock`.
- Windows vs Linux: use `.venv\Scripts\python` on Windows and `.venv/bin/python` on Linux. `deploy/backup.sh` and the systemd units are Linux only. `dashboard/process.py` launches workers with `sys.executable`, so start uvicorn from the venv.
- Deploy docs, systemd units and `deploy/backup.sh` assume the repo is cloned at `/opt/trading-bot` (the repo root is the project directory, there is no `bot/` subfolder). Edit the unit paths if you clone elsewhere.
- The dashboard has no authentication. Never expose its port to the public internet. Remote access is `sudo tailscale serve --bg 8765` (tailnet only, HTTPS on `https://<machine>.<tailnet>.ts.net`, proxies to `127.0.0.1:8765`). Never run `tailscale funnel`, which publishes to the internet.
- The private auth module is baked into the local images (`tradingbot:local`, `tradingbot-keeper:local`) by `COPY . /app`. It holds no secret now, but build the images on the server and never push them or `docker save` them.

## Deployment

- Documented target is an Ubuntu 22.04 or 24.04 home server, reached over Tailscale. Full walkthroughs: `deploy/README.md` and `deploy/UBUNTU_SETUP.md`.
- Docker path (recommended): `docker/Dockerfile` (python:3.11-slim, non-root uid 1000, stages `deps`, `keeper` with Playwright headless Chromium, `app`) and `docker-compose.yml`. Services: `dashboard` (always on, host port 8765 bound to `DASHBOARD_HOST`, default `127.0.0.1`, healthcheck on `/api/health`), under profile `auto`: `cpgw_keeper` (keeper image, the only service with the `ibkr_login` secret, `cap_drop: [ALL]`) and `portfolio`, and under profile `research`: `sentiment`. These use `network_mode: host`, load `.env`, and mount `./logs`, `./models/artifacts`, and `./config` (read only). The keeper and portfolio also mount the `ibkr_gateway_tls` volume read only at `/app/gateway_tls`. Compose does not set `CPGW_CA_FILE`: put `CPGW_CA_FILE=/app/gateway_tls/cert.pem` in `.env` when you run the `ibkr_gateway` service (with the host Java gateway, point it at that gateway's exported certificate). Without it the keeper login stops with a setup error. With it set, a worker started from the dashboard container fails at start, because that container has no tls volume (fails closed). Every service has `mem_limit` equal to `memswap_limit` (gateway 768m, keeper 640m, portfolio 640m, dashboard 512m, sentiment 256m); `tests/test_deploy_config.py` pins these numbers.
- Gateway container: service `ibkr_gateway` under profile `gateway` (`docker/gateway/`), a pinned third-party native build of the IBKR Client Portal Gateway (release `v1.0.0`). Run `scripts/fetch_gateway.sh` first: it downloads the zip into the git-ignored `vendor/ibkr-gateway/` and checks the SHA-256 in `docker/gateway/gateway.sha256`; the build checks it again and never downloads. The entrypoint generates the gateway's own TLS key and keystore (SAN `DNS:localhost,IP:127.0.0.1`, both needed) into the `ibkr_gateway_tls` volume, with `sslPwd` from the `gateway_ssl_password` secret (32 to 128 letters and digits, readable by uid 10001); IBKR's public default `vertx.jks` is removed. It runs non-root with `cap_drop: [ALL]` on its own bridge network `172.31.250.0/24` and publishes `127.0.0.1:5000:5000` only, never `5000:5000`. `ips.allow` is exactly `127.0.0.1` and `172.31.250.1`; if you change the subnet, change the allow list too. The keystore format, `--conf ../run/conf.yaml` and the allow-list address are unverified on the server (each fails closed); see `deploy/README.md` section 0B. The binary may need a CPU with AVX2. Rationale: `docs/adr/0001-native-ibkr-gateway-container.md` and `docs/adr/0003-headless-gateway-login-and-file-secrets.md`.
- Paths: the Docker home server install uses `~/trading-bot`, the systemd path and `deploy/UBUNTU_SETUP.md` use `/opt/trading-bot`.
- The `portfolio` service runs dry by default. To send paper orders, add `--live` to its `command` in `docker-compose.yml`, then `docker compose --profile auto up -d`.
- systemd path: units in `deploy/systemd/` (`tradingbot-dashboard`, `tradingbot-portfolio`, `tradingbot-sentiment`, `tradingbot-cpgw-keeper`), installed as templates (`name@USER.service`) with a venv at `.venv`.
- Backups: `deploy/backup.sh` archives `logs/`, `models/artifacts/` and `config/symbols.yaml` to `~/backups/tradingbot/` (keeps 14, override with `TRADINGBOT_BACKUP_KEEP` and `TRADINGBOT_BACKUP_DIR`), scheduled nightly via cron.
- Backups do not include `.env`, `secrets/` or `core/ibkr_auth_private.py`; copy those outside the repo by hand (deploy/README.md section 0B starts with this).
- Updates: `git pull && docker compose --profile gateway --profile auto up -d --build`.

# Trading bot

Self-hosted, paper-trading-only portfolio bot for Interactive Brokers (Client Portal Gateway). A multi-sleeve portfolio (Core ETF allocation, Sentiment satellite, Tactical XGBoost/LSTM, off by default) with a FastAPI + HTMX dashboard. Rules are in `docs/TRADING_LOGIC.md`, library choices in `docs/TECH_STACK.md`.

## Commands

`pyproject.toml` holds ruff and pytest config only (no `[project]` table). ruff 0.17.4 is pinned in `requirements-dev.txt`, and `core/ibkr_auth.py` is excluded from ruff. CI in `.github/workflows/ci.yml` runs `ruff format --check`, `ruff check` and pytest on Python 3.11 and 3.12 (push to main, pull requests, weekly, manual). Dependencies live in `requirements.txt`, `requirements-train.txt` and `requirements-dev.txt` (pytest, ruff). Tests are in `tests/` (scoring, core and sentiment satellite sleeves, signal fusion, sizing, Alpha Vantage parsing from a fixture, paper lock).

```sh
# Setup (Windows venv python: .venv\Scripts\python, Linux: .venv/bin/python)
uv venv --python 3.11
uv pip install -r requirements.txt
uv pip install -r requirements-train.txt   # only for LSTM training (adds TensorFlow)
uv pip install -r requirements-dev.txt     # pytest and ruff
cp .env.example .env                       # then fill in your own values

# Lint, format and tests (the form CI uses; Windows: .venv\Scripts\...)
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/python -m pytest -q

# Portfolio runner (dry run is the default; --live sends paper orders)
python -m live.portfolio_runner --once               # one dry-run cycle of every enabled sleeve
python -m live.portfolio_runner [--interval SEC] [--live]

# Other long-running workers
python -m live.cpgw_keeper [--once]        # CPGW login + keepalive, writes logs/cpgw_session.json
python -m live.sentiment_worker [--once] [--interval SEC]
python -m live.runner --symbol SPY --model <registry_name> [--quantity N] [--live]   # single-symbol tactical runner

# Dashboard (port 8765 everywhere: README, Dockerfile, docker-compose, systemd, deploy docs)
uvicorn dashboard.app:app --port 8765

# Training (needs a valid CPGW session first, see Gotchas)
python -m scripts.train_symbol --symbol SPY --kind xgboost [--classifier binary|multiclass] [--name NAME] [--no-smote]
python -m scripts.train_symbol --symbol SPY --kind lstm
python -m scripts.inspect_legacy [--file NAME]       # inspect models/artifacts/legacy/*

# Docker (home server)
docker compose up -d --build                         # dashboard only
docker compose --profile auto up -d --build          # dashboard + cpgw_keeper + portfolio + sentiment
docker compose --profile auto logs -f portfolio
```

Run all `python -m ...` commands from the repo root (modules import as `core.*`, `live.*`, etc.).

## Architecture

```
config/symbols.yaml   universe, per-symbol overrides, portfolio and sleeve settings (loaded by core/config.py)
core/                 config.py (env + yaml, assert_paper_mode), cpgw_client.py (HTTPS client for CPGW),
                      ibkr_auth.py (stub), indicators.py, features.py, scoring.py, macro.py, sentiment.py,
                      signal_fusion.py, sizing.py, market_hours.py, sleeves/{core,sentiment_satellite,tactical,long_term}.py
models/               train.py (train_xgboost / train_lstm), registry.py (save/load), artifacts/<name>/{model.pkl|model.keras,scaler.gz,metadata.json}
live/                 portfolio_runner.py (main loop), runner.py (single symbol), cpgw_keeper.py, sentiment_worker.py,
                      storage.py (SQLite at logs/bot.sqlite), notifier.py (Gmail SMTP)
dashboard/            app.py (FastAPI routes, /api/health), process.py (spawns runner/keeper/worker as subprocesses), templates/, static/
scripts/              train_symbol.py, inspect_legacy.py
```

Data flow: CPGW daily bars and Alpha Vantage news go through `core` (indicators, features, scoring, sentiment) into each sleeve in `core/sleeves/`. Sleeves produce target weights, `core/sizing.py` applies volatility targeting and caps, and `live/portfolio_runner.py` turns targets into orders (logged only unless `--live`). Everything is written to SQLite via `live/storage.py`, and the dashboard reads that DB and can start or stop the workers. The tactical sleeve loads models through `models/registry.py` and fuses model confidence with sentiment in `core/signal_fusion.py`.

## Gotchas

- Safety: `PAPER=true` and `IB_PORT=5000` are both required. `core.config.assert_paper_mode()` raises before any broker connection otherwise (only 5000, the Client Portal Gateway port, is allowed: `PAPER_PORTS = {5000}`). `DRY_RUN` defaults to true, and orders are sent only when `--live` is passed. The dashboard still starts when the lock fails, so you can see the misconfiguration.
- The IBKR Client Portal Gateway is IBKR's Java daemon, run separately on the host at `https://localhost:5000` (not part of this repo or compose). Container services use `network_mode: host` to reach it.
- `core/ibkr_auth.py` is a stub in this public repo (the SRP login is not published). `live/cpgw_keeper.py` therefore cannot log in automatically here. Log in to the gateway manually in a browser, or supply your own `login()`. Training and the runners need `logs/cpgw_session.json` written by the keeper.
- `.env` variable names (see `.env.example`): `PAPER`, `DRY_RUN`, `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`, `IBKR_USERNAME`, `IBKR_PASSWORD`, `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_RECIPIENTS`, `ALPHA_VANTAGE_API_KEY`. The Alpha Vantage key is only needed if `sentiment.provider: alpha_vantage` in `config/symbols.yaml`; use the `stub` provider for testing.
- Git-ignored: `.env`, `.venv/`, `logs/*.sqlite`, `logs/*.log`, `models/artifacts/*/model.pkl`, `model.keras`, `scaler.gz`. A fresh clone has no trained models and no `logs/` directory, so train or copy artifacts before enabling the tactical sleeve. Only `metadata.json` and the legacy README are tracked.
- Legacy models in `models/artifacts/legacy/` have unknown provenance (no registry metadata); treat them as baselines, not production models.
- TensorFlow is only in `requirements-train.txt`. The Docker image installs `requirements.txt` only, so LSTM models cannot be loaded or trained there.
- The test suite (`.venv/bin/python -m pytest -q`) covers scoring, the core and sentiment satellite sleeves, signal fusion, sizing, Alpha Vantage parsing from a fixture, and the paper lock. It runs offline: `tests/conftest.py` blocks the network and isolates `.env`. Also verify changes by running a dry-run cycle (`--once`) against a paper gateway, never `--live`.
- `pyproject.toml` exists without a `[project]` table, so `uv run` and `uv sync` treat the repo as a project and create a `uv.lock`. Use `.venv/bin/python` and `.venv/bin/ruff` (Windows: `.venv\Scripts\...`) instead, and do not commit `uv.lock`.
- Windows vs Linux: use `.venv\Scripts\python` on Windows and `.venv/bin/python` on Linux. `deploy/backup.sh` and the systemd units are Linux only. `dashboard/process.py` launches workers with `sys.executable`, so start uvicorn from the venv.
- Deploy docs, systemd units and `deploy/backup.sh` assume the repo is cloned at `/opt/trading-bot` (the repo root is the project directory, there is no `bot/` subfolder). Edit the unit paths if you clone elsewhere.
- The dashboard has no authentication. Never expose its port to the public internet.

## Deployment

- Documented target is an Ubuntu 22.04 or 24.04 home server, reached over Tailscale. Full walkthroughs: `deploy/README.md` and `deploy/UBUNTU_SETUP.md`.
- Docker path (recommended): `docker/Dockerfile` (python:3.11-slim, non-root user, `requirements.txt` only) and `docker-compose.yml`. Services: `dashboard` (always on, host port 8765, healthcheck on `/api/health`), and under profile `auto`: `cpgw_keeper`, `portfolio`, `sentiment`. All use `network_mode: host`, load `.env`, and mount `./logs`, `./models/artifacts`, and `./config` (read only).
- The `portfolio` service runs dry by default. To send paper orders, add `--live` to its `command` in `docker-compose.yml`, then `docker compose --profile auto up -d`.
- systemd path: units in `deploy/systemd/` (`tradingbot-dashboard`, `tradingbot-portfolio`, `tradingbot-sentiment`, `tradingbot-cpgw-keeper`), installed as templates (`name@USER.service`) with a venv at `.venv`.
- Backups: `deploy/backup.sh` archives `logs/`, `models/artifacts/` and `config/symbols.yaml` to `~/backups/tradingbot/` (keeps 14, override with `TRADINGBOT_BACKUP_KEEP` and `TRADINGBOT_BACKUP_DIR`), scheduled nightly via cron.
- Updates: `git pull && docker compose --profile auto up -d --build`.

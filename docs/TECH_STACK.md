# Tech stack

Every moving part in the bot, what it does, and why it's there. Roughly organized by layer, from bottom (data + auth) up to top (UI + deployment).

---

## At a glance

```
                          ┌────────────────────────────────────────────────┐
                          │  IBKR Client Portal Gateway  (Java, host)      │
                          │  https://localhost:5000   ← self-signed cert   │
                          └────────────────────────────────────────────────┘
                                              ▲
                                       SRP-6a │ HTTPS REST
                                              │
              ┌───────────────────────────────┼───────────────────────────┐
              │                               │                           │
       ┌──────┴──────┐                ┌───────┴────────┐          ┌───────┴────────┐
       │ cpgw_keeper │                │  portfolio &   │          │   training     │
       │   (auth)    │  cookie file   │ single runners │          │   scripts      │
       └──────┬──────┘ ─────────────► └───────┬────────┘          └───────┬────────┘
              │                               │                           │
              │                               ▼                           ▼
              │                       ┌───────────────┐         ┌────────────────┐
              │                       │ live/storage  │◄────────│ models/        │
              │                       │   (SQLite)    │         │ registry+train │
              │                       └───────┬───────┘         └────────────────┘
              │                               │
              │                       ┌───────┴────────┐
              └──── sentiment_worker  │   dashboard    │  ← Tailnet only
                    (Alpha Vantage)   │ FastAPI + HTMX │
                                      └────────────────┘
```

All five Python processes (keeper, portfolio runner, single-symbol runner, sentiment worker, dashboard) share **one** SQLite file as the source of truth. They start independently, recover independently, and the dashboard is read-mostly so a crash in any of them doesn't take the others down.

---

## Language + runtime

| What | Version | Why |
|---|---|---|
| **Python** | 3.11 | Modern type hints, dataclass keyword-only args, faster startup than 3.10. Pinned in `Dockerfile` (python:3.11-slim). |
| **Bash** (deploy scripts) | any POSIX-ish | Backup script + systemd helpers only. No clever shell needed. |
| **Java** (CPGW only) | 17 (OpenJDK) | IBKR's gateway is a Java app. We don't touch it - just run it. |

The whole bot is Python. One language end-to-end = one venv, one dep tree, one set of types. Less rope to hang ourselves with.

---

## Data + numerics

| Library | Version | Used for |
|---|---|---|
| **pandas** | ≥ 2.0 | OHLCV DataFrames, indicator calc, signal alignment |
| **numpy** | ≥ 1.24 | Array math under pandas; manual ops in sizing |
| **scipy** | ≥ 1.10 | `linregress` for the linear-regression channel indicator |

No `polars`, no `xarray`, no fancy data frameworks. The whole bot works on at most a few thousand rows per cycle - pandas is fast enough and everyone reads it.

---

## Machine learning

| Library | Version | Role |
|---|---|---|
| **scikit-learn** | ≥ 1.3 | Train/test split, metrics, MLPClassifier (legacy NN train fn), MinMaxScaler |
| **xgboost** | ≥ 2.0 | **Primary** classifier - multiclass (down/flat/up) or binary, grid-searched per-symbol |
| **imbalanced-learn** | ≥ 0.11 | SMOTE oversampling for class balance in `train_xgboost(..., use_smote=True)` |
| **joblib** | ≥ 1.3 | Pickling models to `models/artifacts/<name>/model.pkl` |
| **tensorflow** *(optional)* | ≥ 2.15 | Only needed for `train_lstm()`. Lives in `requirements-train.txt`, not in the production Docker image - adds ~500 MB and isn't needed for inference. |

The default trained model is **XGBoost multiclass** with a small grid search over learning rate, depth, regularization, and subsampling. Per-symbol; saved to disk with its full metadata (features, threshold, accuracy) so we can score it later with zero ambiguity about what it expects.

---

## IBKR connectivity

This is the layer that changed in Phase 7. Old TWS API stack is gone.

| Library | Why |
|---|---|
| **httpx** ≥ 0.27 | HTTP/1.1 client for CPGW REST. Synchronous mode - easier to reason about than async, fast enough. Self-signed cert handled with `verify=False` (URL is pinned to `localhost:5000`). |
| **cryptography** ≥ 42 | Just RSA-PKCS1v1.5 encryption of the SRP session key K (IBKR's non-standard "RSA-K" extension). PyCA is the de-facto standard; no exotic crypto libs. |
| **hashlib + secrets** (stdlib) | SHA-1 + CSPRNG for the SRP-6a math itself. Already in Python. |
| ~~ibapi~~ | **Removed.** TWS binary API replaced by the REST API above. |

The whole IBKR layer is three files:

- `core/ibkr_auth.py` - broker login. Not included in the public version (stub only).
- `core/cpgw_client.py` - REST wrapper. One class, one method per endpoint we care about. Handles cookie rotation, auto-confirms order-warning dialogs, caches symbol→conid lookups.
- `live/cpgw_keeper.py` - long-running daemon that does the login + a `POST /tickle` every 60s + a deeper `auth/status` check every 5 min. Writes the session cookie to `logs/cpgw_session.json` (chmod 600) so other processes can pick it up.

---

## Storage

| What | Tech | Notes |
|---|---|---|
| Time series (predictions, orders, targets, holdings, rebalances, sentiment, events, equity) | **SQLite** (stdlib `sqlite3`) | One file at `logs/bot.sqlite`. Multiple readers + sequential writers - totally fine at this volume. Schema in `live/storage.py`. |
| Trained models | Files on disk | `models/artifacts/<name>/{model.pkl, metadata.json, scaler.gz?}`. Loadable by name from the registry. |
| Session cookie | One JSON file | `logs/cpgw_session.json`, atomically written, `chmod 600`. |
| Process tracking | One JSON file | `logs/processes.json` - what subprocesses the dashboard has started, with PIDs. |
| Config | YAML + .env | `config/symbols.yaml` for declarative stuff (universe, weights, sizing), `.env` for secrets (`chmod 600`). |

No Postgres, no Redis, no message broker. SQLite handles 99% of home-bot use cases at zero ops cost. If you ever push past a few hundred trades/day, swap to Postgres - `core/storage` is small enough to rewrite.

---

## Web stack (dashboard)

| Library / asset | Why |
|---|---|
| **FastAPI** ≥ 0.110 | Routes, JSON APIs, form handlers. Auto-generates `/docs` OpenAPI page. |
| **uvicorn[standard]** | ASGI server. `--reload` for dev, plain mode in Docker. |
| **Jinja2** | Server-rendered HTML templates. No SPA, no React - saves ~2 MB of JS and a build step. |
| **python-multipart** | FastAPI requires it for HTML form submissions (Control page). |
| **Tailwind CSS** via CDN | All the dark-theme styling. No build pipeline. CDN is fine for a home dashboard; we accept the runtime warning. |
| **HTMX** via CDN | The bits that auto-refresh (status badge, recent predictions, orders, events) hit `/partials/*` endpoints every 5 s and swap in the HTML. No client-side state. |
| **Chart.js** via CDN | Confidence-over-time line chart on the overview page. |

All three CDN scripts are pinned to specific versions in `templates/base.html`. The bot works offline too - none of them are load-blocking; the page just degrades gracefully if you're disconnected from the public internet.

---

## Sentiment

| What | Notes |
|---|---|
| **Provider**: Alpha Vantage News & Sentiment API | Free tier: 25 calls/day, 5/min. We batch all tactical symbols into one call → fits comfortably under the limit. |
| **Backend abstraction**: `core/sentiment.py` | `SentimentProvider` Protocol with two concrete implementations: `StubProvider` (returns 0 for everything, for testing) and `AlphaVantageProvider`. Easy to add FinBERT or LLM-scored news later. |
| **Worker**: `live/sentiment_worker.py` | Standalone daemon, polls every hour by default, writes to the `sentiment` SQLite table. Trading processes only read from SQLite - never call the API themselves. Decoupling = trading loop stays fast + we don't blow the rate limit. |

---

## Process orchestration + deployment

| Tool | Used for |
|---|---|
| **Docker** + **docker compose** | Production deployment. One image, four services (`dashboard`, `cpgw_keeper`, `portfolio`, `sentiment`). `network_mode: host` so the containers reach CPGW at `localhost:5000`. |
| **systemd** | Alternative for users who don't want Docker. Five unit templates: `cpgw.service` (Java CPGW on host) + four for the Python services. Hardened with `ProtectSystem=full`, `ProtectHome=read-only`, `ReadWritePaths` scoped to logs + artifacts. |
| **Tailscale** | Private remote access. Dashboard binds to `127.0.0.1:8765` (in dev) or `0.0.0.0:8765` (in Docker, behind `network_mode: host`); only Tailnet peers can reach it. Public internet exposure is explicitly **never** part of the design. |
| **cron** | Nightly backup at 03:15 (`deploy/backup.sh`). |

Why this exact split (Docker + Tailscale + systemd-for-Java-only): the bot itself benefits from containerization (clean Python env, one-command upgrade). CPGW is a stateful Java app that wants persistent disk + a JVM; running it natively under systemd is simpler than wrapping it. Tailscale beats every other "expose my dashboard" option for a home server (no auth needed, no port forwards, no public surface).

---

## Notification / alerting

| What | Notes |
|---|---|
| **smtplib + Gmail App Password** | Emails on every tactical trade. Code in `live/notifier.py`. Graceful no-op if EMAIL_USER/PASS aren't set (so dev doesn't spam your inbox). |
| **Telegram** | Sketched but not wired. Will land in a future phase if you want real-time push instead of email. |

---

## Trading-specific libraries we deliberately did NOT add

| Library | Why not |
|---|---|
| `backtrader`, `zipline`, `vectorbt` | Big abstractions for backtesting we don't need yet. Phase 8+ if we add a real backtester. |
| `pyfolio`, `quantstats` | Performance analytics; same - when there's enough live history to analyse. |
| `riskfolio-lib`, `pypfopt` | Portfolio optimizer libs. Our All-Weather sleeve is fixed-weight; risk parity / mean-variance would be the trigger to pull these in. |
| `ta-lib` (C extension) | Pure-Python indicators in `core/indicators.py` are fast enough; ta-lib needs a system library and adds install pain. |
| `ib_insync` | Wraps the *old* TWS API. We deliberately moved off it to use REST. |

The full philosophy: **avoid frameworks until you've felt the pain they solve.** Right now the bot is small enough that having explicit, readable code beats inheriting someone else's abstractions.

---

## File / directory layout

```
bot/
├── core/                     ← pure logic, no I/O side effects beyond DB/HTTP wrappers
│   ├── config.py             ← all settings (env + YAML); paper-mode lock lives here
│   ├── indicators.py         ← ADX, ATR, BB, MACD, OBV, RSI, LRC, ...
│   ├── features.py           ← named feature sets (full / indicators_only / ohlcv)
│   ├── sizing.py             ← vol-target × signal-weight, gross + per-symbol caps
│   ├── signal_fusion.py      ← TA confidence × sentiment → combined_confidence
│   ├── sentiment.py          ← AlphaVantage + Stub providers
│   ├── ibkr_auth.py          ← broker login (stub in the public version)
│   ├── cpgw_client.py        ← REST wrapper for the gateway
│   └── sleeves/
│       ├── tactical.py       ← short/mid-term, TA × sentiment, vol-target sizing
│       └── long_term.py      ← All-Weather, fixed weights, calendar/drift rebalance
│
├── models/
│   ├── train.py              ← XGBoost + LSTM training fns
│   ├── registry.py           ← save/load/list trained artifacts
│   └── artifacts/            ← per-model directories with model + metadata
│
├── live/                     ← long-running processes
│   ├── cpgw_keeper.py        ← SRP login + tickle keepalive
│   ├── portfolio_runner.py   ← multi-sleeve runner, the heart of the bot
│   ├── runner.py             ← single-symbol runner (debug / legacy)
│   ├── sentiment_worker.py   ← Alpha Vantage poller
│   ├── storage.py            ← SQLite schema + read/write helpers
│   └── notifier.py           ← email sender
│
├── dashboard/
│   ├── app.py                ← FastAPI routes + JSON APIs + form handlers
│   ├── process.py            ← subprocess manager (start/stop daemons from /control)
│   ├── templates/            ← Jinja2 (base / index / sleeves / targets / control / models / logs)
│   └── static/               ← minimal CSS
│
├── config/
│   └── symbols.yaml          ← universe, weights, sleeve params, sentiment cadence
│
├── scripts/
│   ├── train_symbol.py       ← train one model from the CLI
│   └── inspect_legacy.py     ← probe the pre-Phase-1 .pkl files
│
├── docker/Dockerfile         ← python:3.11-slim, non-root, ~150 MB
├── docker-compose.yml        ← 4 services, healthchecks, network_mode: host
├── deploy/
│   ├── UBUNTU_SETUP.md       ← step-by-step headless install
│   ├── README.md             ← Day-2 operations + alternative paths
│   ├── backup.sh             ← nightly tar.gz to ~/backups/
│   └── systemd/              ← 5 unit templates for non-Docker users
│
├── docs/
│   ├── TECH_STACK.md         ← this file
│   └── TRADING_LOGIC.md      ← what the bot actually does and why
│
├── requirements.txt          ← production deps (slim, no TF)
├── requirements-train.txt    ← adds TF for LSTM training
├── SECURITY.md               ← audit of the SRP auth + integration concerns
└── README.md                 ← phase-by-phase project log
```

---

## Versioning + upgrades

- **Python deps**: pinned with `>=` minimums in `requirements.txt`. Rebuild the Docker image (`docker compose --profile auto up -d --build`) to pick up new versions.
- **Models**: every artifact has its training metadata in `metadata.json`. Replacing a model is `scripts/train_symbol.py --name <same>` - the file is overwritten in place, the registry picks up the new version on next runner restart.
- **Config (`symbols.yaml`)**: hot-reloaded on every cycle by the runners (they `load_symbol` per cycle). Editing it doesn't need a restart.
- **`.env`**: not hot-reloaded. Change → `docker compose --profile auto restart`.
- **Schema migrations**: the SQLite schema is created with `IF NOT EXISTS` so adding a new table is a no-op restart. Adding a column to an existing table needs a manual `ALTER TABLE` for the moment.

---

## Lines of code (rough, May 2026)

| Layer | LoC |
|---|---|
| `core/` (config, indicators, features, sizing, fusion, sentiment, auth, CPGW client, sleeves) | ~1,800 |
| `live/` (runner, portfolio_runner, sentiment_worker, cpgw_keeper, storage, notifier) | ~1,500 |
| `models/` (train, registry) | ~400 |
| `dashboard/` (app + process manager + templates) | ~1,200 |
| `scripts/` | ~250 |
| Deployment (Dockerfile, compose, systemd, backup, README) | ~600 |
| Docs (this file + TRADING_LOGIC + SECURITY + READMEs) | ~1,500 |
| **Total** | **~7,200** |

That's about 1/3 the size of a typical "trading bot framework" - by design.

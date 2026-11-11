# Plan: portfolio polish (issue #4)

Branch: `chore/portfolio-polish`. Plans live in `docs/plans/`; this file is the only planning artifact.

## Decisions (approved 2026-11-11)

Approved with the defaults below, except:
- Q4: one `test:` commit per module (see task 4).
- Q6: the implementer updates the stale line in the repo `CLAUDE.md` (new task 8). The user explicitly authorised this edit.
- Q7: yes, CI also runs on a weekly `schedule` (see Design C and task 5).
- Branch: `chore/portfolio-polish`.
- At ship: open the follow-up issues from Risks as separate GitHub issues and link them in the PR.

## Open questions (answered, kept for the record)

1. Task 1 split. The issue says task 1 is "its own commit" and also that the format commit has "no other changes". This plan splits issue task 1 into three commits: config (`chore:`), format only (`style:`), lint autofixes (`style:`). Is that acceptable, or should config and format land together? Default if no answer: three commits as planned.
2. Lint rule set. The plan selects ruff's default rules plus isort (`E4, E7, E9, F, I`). `UP` (pyupgrade) and `B` (bugbear) are left for a follow-up issue. OK? Default: yes.
3. Alpha Vantage fixture. The plan uses a small hand-built JSON file that follows the real response shape (numbers as strings, `feed` list, `ticker_sentiment` lists), with made-up titles and `example.com` URLs. A real captured response would need an API key and network once, and contains third-party article text. Is a hand-built fixture acceptable as "a saved Alpha Vantage JSON response"? Default: hand-built, shape checked against the docstring in `core/sentiment.py` and the public API docs.
4. Issue task 2 is one commit. The test suite will be roughly 400 to 600 lines. Keep it as one `test:` commit as the issue asks, or allow one `test:` commit per module? Default: one commit.
5. `docs/TRADING_LOGIC.md` and code disagree in places (see Design, section D). The issue says to describe the rules "as they are in the config and code". This plan documents what the code does, and adds a short "Known gaps" section that points to follow-up issues. It does not fix the code. OK?
6. Repo `CLAUDE.md` says "No pyproject.toml, no lint/format config, no `.github/workflows`". That line becomes stale after this work. The plan does not touch `CLAUDE.md` (agents must not edit it). The user should update it, or say if an implementer may.

## 1. Goal

Make the repo read well as a portfolio project: one code style enforced by ruff, an offline pytest suite for the pure decision logic, GitHub Actions CI on Python 3.11 and 3.12 with a README badge, a `docs/TRADING_LOGIC.md` that matches `config/symbols.yaml` and the current code, and a CHANGELOG entry. `core/ibkr_auth.py` must stay byte-for-byte unchanged (`git diff main -- core/ibkr_auth.py` is empty).

Out of scope:
- Any behaviour change in `core/`, `live/`, `dashboard/`, `models/`, `scripts/`. Only formatting and safe lint autofixes (unused imports, import order) touch code.
- Fixing the mismatches found while planning (Design, section D). They become follow-up issues.
- Tests for `live/`, `dashboard/`, `core/cpgw_client.py`, `core/macro.py` fetchers, `core/indicators.py`, `models/`.
- README "Run it" section (still uses `pip`), `CLAUDE.md`, ADRs, `.git-blame-ignore-revs`, dependency pinning in `requirements.txt`.

## 2. Current state

- No `pyproject.toml`, no `.gitattributes`, no `.github/`. All tracked files are already LF (checked: no CRLF in any `.py`).
- Dependencies: `requirements.txt` (pandas, numpy, scipy, scikit-learn, xgboost, imbalanced-learn, joblib, python-dotenv, pyyaml, httpx, cryptography, fastapi, uvicorn, jinja2, python-multipart), `requirements-dev.txt` (`-r requirements.txt`, `pytest>=8`), `requirements-train.txt` (adds tensorflow). Local `.venv` is Python 3.11.5 with pytest 9.2.0, pandas 3.1.1, numpy 2.4.6.
- Tests: `tests/__init__.py`, `tests/test_config.py` (paper lock: env port 5000 passes, 4002 refused, default `AppConfig()` passes, `paper=False` refused, legacy ports refused without naming other legacy ports).
- `core/ibkr_auth.py`: 38-line stub (`IBKRAuthError`, `MaxLoginAttempts`, `IbkrCredentials`, `login()` raising `NotImplementedError`). Must not change.
- Lint estimate (read-only AST scan, ruff itself not installed): about 18 unused imports (F401) in `core/scoring.py` (`np`), `core/cpgw_client.py` (`Iterable`), `core/macro.py` (`csv`, `timedelta`, `Any`), `core/sleeves/core.py` (`ScoringResult`), `dashboard/app.py` (`LOGS_DIR`), `live/portfolio_runner.py` (several, plus a duplicate `from datetime import datetime, timezone`, F811), `live/notifier.py` (`Path`), `live/cpgw_keeper.py` (`Path`), `live/storage.py` (`Iterable`). No bare `except`, no lambda assignments, no star imports, no E402 found. 111 lines exceed 88 chars, 22 exceed 100, 1 exceeds 120.
- Modules under test and their imports (all safe offline, nothing reads env or touches the network at import time):
  - `core/config.py`: `yaml`, `dotenv`. `load_app_config()` calls `load_dotenv(ROOT / ".env")`, which copies a developer's `.env` into `os.environ` for the rest of the session. `assert_paper_mode(cfg)` checks `cfg.paper` then `cfg.ib.port in PAPER_PORTS` (`{5000}`).
  - `core/scoring.py`: `score_universe`, `select_holdings`, private `_macro_score`, `_trend_score`, `_check_risk_off`, `_rank_to_signed`, `_clip`, `_norm`; imports `core.config.CoreSleeveConfig`, `core.macro.MacroSnapshot`.
  - `core/macro.py`: `MacroSnapshot` dataclass (11 required fields); network only inside `MacroFetcher` methods (`urlopen`).
  - `core/sleeves/core.py`: `CoreSleeve.__init__` (weights must sum to 1.0), `should_evaluate(...)`, `_max_drift`, `evaluate(...)`. Importing it runs `core/sleeves/__init__.py`, which also imports `long_term.py`, `sentiment_satellite.py`, `tactical.py` (all pure; `tactical.py` imports `signal_fusion` and `sizing`, no models, no TensorFlow).
  - `core/sleeves/sentiment_satellite.py`: `SentimentSatelliteSleeve.evaluate(...)`, `_entry_check`, `_maybe_exit_reason`; dataclasses `SentimentInput`, `Position`, `SatelliteEntry`, `SatelliteExit`, `SatelliteCycleResult`.
  - `core/signal_fusion.py`: `fuse(...)`, `FusedSignal`, `BULLISH_CLASSES`.
  - `core/sizing.py`: `realized_vol`, `_size_one`, `size_portfolio`, `SymbolSignal`, `SizingDecision`, `PortfolioParams`.
  - `core/sentiment.py`: `StubProvider`, `AlphaVantageProvider` (`__init__` reads `ALPHA_VANTAGE_API_KEY` and raises `ValueError` when missing; `fetch_batch` calls module-level `urlopen`; `_aggregate`; `_normalize`), `get_provider(name)`.
- CI needs: `requirements-dev.txt` already pulls `requirements.txt`, which covers every import above. TensorFlow is never imported by these modules (`models/registry.py` and `models/train.py` import it lazily and are not imported by tests).
- `docs/TRADING_LOGIC.md` describes an older two-sleeve layout (80% All-Weather `long_term` plus 20% tactical, 60-second cycles, 10-minute cooldown, 2-hour sentiment staleness). `config/symbols.yaml` has core 70%, sentiment 20%, tactical `enabled: false` with `allocation_pct: 0.0`, hourly cycles, no `long_term` block.
- `CHANGELOG.md` has an `[Unreleased]` section with `Added`, `Changed`, `Fixed`.
- Remote: `https://github.com/pechkornel/trading-bot`.

## 3. Design

### A. Ruff and line endings (issue task 1)

`pyproject.toml` holds tool config only. No `[project]` table, so `uv sync` and `uv run` do not treat the repo as a package or try to sync it. The exact content (load-bearing):

```toml
[tool.ruff]
target-version = "py311"
line-length = 100
# extend-exclude keeps ruff's default excludes (.venv, .git, __pycache__, ...).
# `exclude` would replace them.
extend-exclude = ["core/ibkr_auth.py"]
# Without this, `ruff format core/ibkr_auth.py` (explicit path, editors,
# pre-commit) would still format the file.
force-exclude = true

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I"]

[tool.ruff.lint.isort]
known-first-party = ["core", "dashboard", "live", "models", "scripts", "tests"]

[tool.ruff.format]
quote-style = "double"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

Top-level `[tool.ruff]` exclusion applies to both `ruff format` and `ruff check`, so one entry covers both.

`.gitattributes`: one line, `* text=auto eol=lf`. No tracked file has CRLF today, so this causes no renormalisation churn.

Ruff is pinned to one exact version in `requirements-dev.txt` (`ruff==<version used for the style commit>`), so local runs and CI format identically. A newer ruff can change formatting and break `ruff format --check`.

Rejected:
- `exclude = [...]`: replaces ruff's default exclude list, so `.venv` would be linted.
- `# fmt: off` or `# noqa` inside `core/ibkr_auth.py`: that edits the file.
- `ruff.toml` instead of `pyproject.toml`: the issue asks for `pyproject.toml`, and pytest config fits there too.
- `line-length = 88`: more reflow churn (111 lines over 88, 22 over 100) for no gain in a repo with long aligned comments.
- Selecting `UP` and `B` now: larger behaviour-adjacent diff; `B905` would flag a real bug in `size_portfolio` (section D) that is out of scope here.
- `ruff` via `uvx` in CI: unpinned unless you pass a version; the requirements pin is simpler and shared with local use.

### B. Tests (issue task 2)

Layout:

```
tests/
  conftest.py                         offline guard, .env isolation, shared factories
  fixtures/alpha_vantage_news_sentiment.json
  test_config.py                      extended (paper lock env parsing)
  test_scoring.py
  test_sleeve_core.py
  test_sleeve_sentiment_satellite.py
  test_signal_fusion.py
  test_sizing.py
  test_sentiment.py
```

`tests/conftest.py`:
- Autouse fixture `_no_network`: `monkeypatch.setattr` on `socket.socket.connect`, `socket.socket.connect_ex` and `socket.getaddrinfo` with a function that raises `RuntimeError("network access in tests")`. This proves the suite is offline: any accidental `urlopen` fails loudly.
- Autouse fixture `_no_dotenv`: `monkeypatch.setattr("core.config.load_dotenv", lambda *a, **k: False)` so tests never read a developer's `.env` and never leak its values into `os.environ`.
- Autouse fixture that `monkeypatch.delenv(..., raising=False)` for `PAPER`, `IB_PORT`, `IB_HOST`, `IB_CLIENT_ID`, `DRY_RUN`, `ALPHA_VANTAGE_API_KEY`, so results do not depend on the shell.
- Helpers (plain functions or fixtures): `make_macro(**overrides) -> MacroSnapshot` with every field `None`, `complete=False`, `as_of=datetime(2026, 11, 13, tzinfo=timezone.utc)`; `series(values) -> pd.Series`; `linear_closes(start, step, n) -> pd.Series`.

Testing approach: pure functions, synthetic pandas series, explicit `now=` datetimes, `pytest.approx` for floats. Use the public API first; call private helpers (`_rank_to_signed`, `_macro_score`, `_trend_score`, `_check_risk_off`, `_aggregate`) only where the public path cannot isolate a rule. Use small configs (for example `CoreSleeveConfig(universe=["SPY", "TLT", "GLD"], hold_top_n=2, momentum_lookback_days=10, trend_ma_days=5, risk_off_spy_ma_days=5, risk_off_universe=["TLT", "IEF"])`) so series stay short.

Alpha Vantage tests patch `core.sentiment.urlopen` with a fake that returns a context manager whose `read()` returns the fixture bytes, and records the `Request` URL. Construct the provider with `AlphaVantageProvider(api_key="test-key")`.

Fixture content (`tests/fixtures/alpha_vantage_news_sentiment.json`), shaped like the real API: top-level `"items"`, `"sentiment_score_definition"`, `"relevance_score_definition"`, `"feed"`. Each feed item has `title`, `url` (`https://example.com/...`), `time_published`, `source`, `overall_sentiment_score` (number), `overall_sentiment_label`, `ticker_sentiment` (list of objects with string `ticker`, `relevance_score`, `ticker_sentiment_score`, `ticker_sentiment_label`). Requested symbols in the tests: `["AAPL", "MSFT", "NVDA", "GOOGL"]`. Articles chosen so results are hand-computable:

| Article | ticker_sentiment entries |
|---|---|
| 1 | AAPL score "0.30" rel "0.80"; MSFT score "-0.10" rel "0.50"; TSLA score "0.90" rel "1.0" (not requested, ignored) |
| 2 | AAPL score "0.10" rel "0.20" |
| 3 | AAPL score "n/a" rel "0.50" (malformed, skipped, not counted) |
| 4 | MSFT score "0.40" rel "0.0" (counted, zero weight) |
| 5 | GOOGL score "0.80" rel "1.0" (clamp case) |

Expected: AAPL score 0.52, article_count 2, confidence 0.1. MSFT score -0.2, count 2, confidence 0.1. NVDA score 0.0, count 0, confidence 0.0. GOOGL score 1.0 (raw 0.8 times 2 clamped), count 1, confidence 0.05. Output order equals the requested symbol order; `source == "alpha_vantage"`; `raw_json is None`.

Rejected:
- `responses`, `pytest-httpx`, `requests-mock`: `core/sentiment.py` uses stdlib `urllib`; a monkeypatched `urlopen` needs no new dependency.
- `pytest-socket`: new dependency for something ten lines of conftest do.
- `freezegun`: every time-dependent function already takes `now=`.
- Hypothesis property tests: nice, but new dependency and out of scope.

### C. CI (issue task 3)

`.github/workflows/ci.yml`:
- Triggers: `push` to `main`, `pull_request`, and a weekly `schedule` (`cron: "0 6 * * 1"`, Monday 06:00 UTC) to catch dependency drift. Also `workflow_dispatch` for manual runs.
- `permissions: contents: read`.
- One job `test`, `runs-on: ubuntu-latest`, `strategy.matrix.python-version: ["3.11", "3.12"]`, `fail-fast: false`.
- Steps: `actions/checkout` (current major), `astral-sh/setup-uv` (current major, `enable-cache: true`, `cache-dependency-glob: "requirements*.txt"`), then:
  ```sh
  uv venv --python ${{ matrix.python-version }}
  uv pip install -r requirements-dev.txt
  .venv/bin/ruff format --check .
  .venv/bin/ruff check .
  .venv/bin/python -m pytest -q
  ```
  Calling `.venv/bin/...` directly avoids any uv project discovery around a `pyproject.toml` with no `[project]` table.
- No extra CI step guards `core/ibkr_auth.py`: `force-exclude` keeps ruff off it, and the reviewer runs the Done-when check `git diff main -- core/ibkr_auth.py`.

README badge, directly under `# Trading bot`:

```markdown
[![CI](https://github.com/pechkornel/trading-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/pechkornel/trading-bot/actions/workflows/ci.yml)
```

Rejected:
- Separate lint job: the issue asks for all three checks on both versions; one job is simpler. Lint is cheap.
- `actions/setup-python` plus `pip`: user convention is uv everywhere.
- Installing `requirements-train.txt`: TensorFlow is not needed by any test and adds about 500 MB.

### D. `docs/TRADING_LOGIC.md` rewrite (issue task 4)

Rewrite from scratch. Plain sentences, no em or en dashes, ASCII diagrams allowed. Every number must come from `config/symbols.yaml` (or the dataclass default in `core/config.py` when the YAML does not set it), and every rule from the code below. Sections:

1. Portfolio split. Total equity is the static `portfolio.starting_equity` (100,000), not the live IBKR account value. Sleeve equity is `starting_equity * allocation_pct`: core 70% (70,000), sentiment 20% (20,000), tactical disabled at 0%. The remaining 10% is never traded. Sleeves keep separate budgets and separate rows in the `holdings` table; the same symbol (for example SPY) can be held by both core and sentiment. Holdings are also written in dry-run mode, so a dry run simulates positions in SQLite; in `--live` mode holdings are updated when the order is placed, not on fill.
2. Cycle and market hours (`live/portfolio_runner.py`, `core/market_hours.py`). One cycle per `cycle_interval_sec` (3600). Regimes in US Eastern time: OPEN (Mon to Fri 09:30 to 16:00), PRE_OPEN, AFTER_CLOSE, WEEKEND. DST is approximated by date and holidays are not handled. Sentiment entries run in OPEN and AFTER_CLOSE (`should_run_entries`); exits run in every regime (`should_run_exits`). The core sleeve is skipped in WEEKEND and PRE_OPEN.
3. Core sleeve scoring (`core/scoring.py`). Universe of 10 ETFs from `sleeves.core.universe` with their buckets (`ASSET_BUCKET`). Composite = 0.40 momentum + 0.30 macro + 0.30 trend (weights must sum to 1.0, checked in `CoreSleeve.__init__`).
   - Momentum: return over `momentum_lookback_days` (252), ranked across the universe to [-1, 1] (best +1, worst -1, ties averaged). Assets with too little history get 0. Fewer than two valid assets gives everyone 0.
   - Trend: distance of the last close from its `trend_ma_days` (200) moving average, scaled so plus or minus 10% maps to plus or minus 1, clipped.
   - Macro: per-bucket rules averaged over available inputs and clipped to [-1, 1]: yield curve (`T10Y2Y`), VIX (centred on 18, scaled by 12), 60-day DXY change (times 10), 60-day HYG/LQD change (times 10, half weight and inverted for bonds). Neutral 0 when no macro data. Data sources: FRED CSV and the Yahoo Finance chart endpoint (`core/macro.py` calls it directly, not the `yfinance` package), cached for `cache_ttl_sec` (21600).
   - Risk-off: when SPY's last close is below its `risk_off_spy_ma_days` (200) average AND VIX is above `risk_off_vix_threshold` (25), the target is an equal split across `risk_off_universe` (TLT, IEF). Both conditions are required.
   - Selection: otherwise equal weight across the top `hold_top_n` (4).
4. Core sleeve triggers and orders (`core/sleeves/core.py`, `live/portfolio_runner.py`). Drift trigger: with N held symbols, each symbol's target is taken as 1/N, and the sleeve evaluates when any held symbol's weight (shares times latest close over sleeve equity) is more than `drift_threshold` (0.08) away from it. Calendar trigger: `CoreSleeve.should_evaluate` compares the weekday of the current UTC time with `rebalance_day_of_week` (4, Friday) and runs at most once per UTC calendar date (`last_rebalance` in the `rebalances` table). Target shares are `floor(sleeve_equity * weight / close)`; the delta to current shares becomes one limit order per symbol: `LMT`, `tif=DAY`, price `close * 1.0005` for BUY and `close * 0.9995` for SELL. No stop-loss or take-profit on core positions. Every evaluation writes one `rebalances` row and one `targets` row per symbol.
5. Sentiment satellite (`core/sleeves/sentiment_satellite.py`, `core/sentiment.py`, `live/sentiment_worker.py`).
   - Data: the worker polls Alpha Vantage NEWS_SENTIMENT once per `poll_interval_sec` (3600) in one batch for the satellite universe (`limit=100` articles). Per ticker: relevance-weighted mean of `ticker_sentiment_score`, times 2, clipped to [-1, 1]. `article_count` is the number of ticker mentions. Rows older than `staleness_sec` (86400) count as missing.
   - Exits first: a position exits when it is at least `hold_max_days` (10) calendar days old (code uses `(now - opened_at).days`, not trading days), or when the latest daily close is at least `stop_loss_pct` (5%) below the entry price. The stop is checked by the bot each cycle; there is no stop order at the broker.
   - Entries: candidates in `sleeves.sentiment.universe`, sorted by sentiment score descending. Skip if no fresh sentiment, if `abs(score) < sentiment_threshold` (0.4, so exactly 0.4 enters), if `article_count < min_article_count` (5), if the score is negative while `longs_only` is true, if already held, or if the position rounds to 0 shares. At most `max_concurrent_positions` (4) open at once, counted after exits. Size: `floor(sleeve_equity * position_pct_of_sleeve / price)`, so 5% of the sleeve (1,000) per position and at most 20% of the sleeve invested.
   - Orders: same limit order shape as core (LMT DAY, plus or minus 0.05%). No bracket.
6. Tactical sleeve (disabled). `enabled: false`, `allocation_pct: 0.0`. `live/portfolio_runner.py` does not run it even when enabled (it only adds its symbols to the price fetch). The single-symbol `live/runner.py` is the only tactical path. Short description of the existing code for reference: XGBoost prediction and confidence, `core/signal_fusion.fuse` (0.7 TA, 0.3 sentiment, sentiment never flips direction, missing sentiment means TA only), `core/sizing.size_portfolio` (confidence floor 0.55, linear signal weight, vol scale `target_vol_per_bar / realized_vol` clipped to [0.1, 5.0], per-symbol cap 40% and gross cap 100% from the YAML). Recompute the worked examples from the code: 0.85 up with +0.40 sentiment gives 0.715; 0.85 up with -0.80 gives 0.355 (the old doc said 0.32); 0.70 up with no sentiment gives 0.70; 0.80 down with +0.90 gives 0.29.
7. Risk controls table: paper lock (`assert_paper_mode`: `PAPER=true` and `IB_PORT=5000`), dry run by default (`--live` needed), risk-off filter, top-N equal weight, drift threshold, sentiment threshold and article minimum, longs only, max concurrent positions, fixed position size, hold timer, bot-side stop, limit orders only.
8. Known gaps (each links to a follow-up issue, see Risks): UTC weekday versus Eastern regime in the core calendar trigger, unused `reentry_cooldown_sec`, calendar days versus "trading days", tactical not wired into the portfolio runner, static equity, no holiday calendar.
9. Where to change behaviour: table of YAML keys per intent, matching the current `sleeves.core`, `sleeves.sentiment`, `sleeves.tactical`, `macro`, `sentiment_provider` blocks. Remove every `long_term` reference.

Mismatches found while planning (document the code behaviour, do not fix):
- `CoreSleeve.should_evaluate` uses `datetime.now(timezone.utc).weekday()`, while the runner gates on Eastern regimes. With hourly cycles the calendar trigger can fire on Thursday evening Eastern time (after 20:00 EDT or 19:00 EST, when UTC is already Friday) or during the Friday session, not "Friday close" as the YAML comment says. `is_core_rebalance_window` only bypasses the WEEKEND/PRE_OPEN skip, so it has no effect in practice.
- `portfolio.reentry_cooldown_sec` (86400) is read into `PortfolioConfig` and shown on the dashboard control page, but no runner uses it.
- YAML comment says "hard exit after N trading days"; code counts calendar days.
- `AlphaVantageProvider.lookback_hours` is stored but never sent to the API.
- `size_portfolio(signals: Iterable[...])` iterates `signals` twice (list comprehension, then `zip` when gross-capping). A generator input would give an empty result after gross capping. `TacticalSleeve.decide` passes a list, so it works today.
- `core/macro.py` uses `datetime.utcnow()`, deprecated since Python 3.12 (warning only, not called by tests).

## 4. Tasks

Mapping to the issue: issue task 1 is tasks 1 to 3 here, issue task 2 is task 4, issue task 3 is task 5, issue task 4 is task 6, issue task 5 is task 7. Commands below are for macOS (zsh) in the repo root; on Windows (PowerShell) replace `.venv/bin/` with `.venv\Scripts\`.

1. **chore: add ruff and pytest config and LF line endings.**
   Files: `pyproject.toml` (new, content from Design A), `.gitattributes` (new, `* text=auto eol=lf`), `requirements-dev.txt` (add `ruff==X.Y.Z`, the exact version `uv pip install ruff` resolves today).
   Steps: `uv pip install -r requirements-dev.txt`.
   Verify: `.venv/bin/python -m pytest -q` still passes (5 existing tests plus the 4 parametrized cases). `git ls-files --eol | grep -v "i/lf"` lists only files with no text (or nothing). `.venv/bin/ruff format --check core/ibkr_auth.py` reports no files checked (warning "No Python files found"), proving `force-exclude` works. `.venv/bin/ruff check --statistics .` output is saved in the commit body or PR notes for task 3. No `.py` file changes in this commit.
   independent

2. **style: format the repo with ruff.**
   Files: every `.py` file except `core/ibkr_auth.py` (output of `.venv/bin/ruff format .`). Nothing else.
   Verify: `.venv/bin/ruff format --check .` passes. `git diff main -- core/ibkr_auth.py` is empty. `git diff --stat` shows only `.py` files. `.venv/bin/python -m pytest -q` passes. `.venv/bin/python -m compileall -q core live dashboard models scripts tests` succeeds. Commit message: `style: apply ruff format` with a body noting it is formatting only.
   depends on: 1

3. **style: fix ruff lint findings.**
   Files: the files reported by `ruff check` (expected: `core/scoring.py`, `core/cpgw_client.py`, `core/macro.py`, `core/sleeves/core.py`, `dashboard/app.py`, `live/portfolio_runner.py`, `live/notifier.py`, `live/cpgw_keeper.py`, `live/storage.py`, plus import-order changes from `I` across most `.py` files). Possibly `pyproject.toml` for a `per-file-ignores` entry.
   Steps: `.venv/bin/ruff check --fix .` (safe fixes only, never `--unsafe-fixes`), then `.venv/bin/ruff format .` again. Review the diff by hand: only import lines may change. Anything that is not autofixable and is not a pure import change gets an explicit `[tool.ruff.lint.per-file-ignores]` entry with a one-line comment why, and a follow-up issue. Do not hand-edit logic. Do not touch `core/sleeves/__init__.py` re-exports (they are in `__all__`, so F401 does not fire; confirm).
   Verify: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` pass. `git diff main -- core/ibkr_auth.py` empty. `.venv/bin/python -m pytest -q` passes. Import smoke test: `.venv/bin/python -c "import core.sleeves, core.sentiment, live.portfolio_runner, live.sentiment_worker, live.cpgw_keeper, dashboard.app"` succeeds. If a paper gateway session is available, `python -m live.portfolio_runner --once` (dry run, never `--live`) still runs a cycle.
   depends on: 2

4. **test: offline tests for scoring, sleeves, fusion, sizing, sentiment and the paper lock.**
   Files: `tests/conftest.py`, `tests/fixtures/alpha_vantage_news_sentiment.json`, `tests/test_scoring.py`, `tests/test_sleeve_core.py`, `tests/test_sleeve_sentiment_satellite.py`, `tests/test_signal_fusion.py`, `tests/test_sizing.py`, `tests/test_sentiment.py`, `tests/test_config.py` (extend). No changes outside `tests/`.
   Test cases are listed in section 5. Write each test file, run it, then the next.
   Commits: one `test:` commit per module, in this order, each with the suite green:
   1. `test: add offline conftest and extend paper-lock tests` (`tests/conftest.py`, `tests/test_config.py`)
   2. `test: cover signal fusion` (`tests/test_signal_fusion.py`)
   3. `test: cover position sizing` (`tests/test_sizing.py`)
   4. `test: cover core scoring` (`tests/test_scoring.py`)
   5. `test: cover the core sleeve` (`tests/test_sleeve_core.py`)
   6. `test: cover the sentiment satellite sleeve` (`tests/test_sleeve_sentiment_satellite.py`)
   7. `test: cover Alpha Vantage sentiment parsing` (`tests/test_sentiment.py`, `tests/fixtures/alpha_vantage_news_sentiment.json`)
   Verify: `.venv/bin/python -m pytest -q` passes with Wi-Fi off (or the conftest guard active, which makes any network call fail). `.venv/bin/ruff format --check .` and `.venv/bin/ruff check .` pass. `.venv/bin/python -m pytest -q -p no:cacheprovider` passes too (no reliance on cache). `.venv/bin/python -c "import sys, pytest; pytest.main(['-q']); print('tensorflow' in sys.modules)"` prints `False`.
   depends on: 3

5. **ci: add GitHub Actions workflow and README badge.**
   Files: `.github/workflows/ci.yml` (new, Design C), `README.md` (badge line under the title only).
   Triggers include the weekly `schedule` and `workflow_dispatch` from Design C.
   Verify: `.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` parses. Run the four CI commands locally in a fresh venv (`uv venv --python 3.12 /private/tmp/ci312` style scratch path, then the same install and checks) to catch 3.12 issues before pushing. After push (lead or ship step, not the implementer unless asked), both matrix jobs are green and the badge renders.
   depends on: 3

6. **docs: rewrite TRADING_LOGIC.md to match the current config and code.**
   Files: `docs/TRADING_LOGIC.md` only.
   Content: Design D, sections 1 to 9.
   Verify: every number in the doc appears in `config/symbols.yaml` or as a default in `core/config.py` (check by grep, one by one). `rg -n "long_term|All-Weather|80%|600s|7200|every minute" docs/TRADING_LOGIC.md` returns nothing except an explicit "legacy, not used" note if kept. `rg -n "[\x{2013}\x{2014}]" docs/TRADING_LOGIC.md` returns nothing (no en or em dashes). Fusion examples recomputed with `fuse(...)` in a Python shell match the table.
   depends on: 3

7. **docs: add portfolio polish entries to the changelog.**
   Files: `CHANGELOG.md` only, inside the existing `[Unreleased]` section.
   Entries: Added: `pyproject.toml` with ruff and pytest config (`core/ibkr_auth.py` excluded), `.gitattributes` (LF), ruff pinned in `requirements-dev.txt`, offline test suite for scoring, core and sentiment sleeves, signal fusion, sizing, Alpha Vantage parsing and the paper lock, GitHub Actions CI on Python 3.11 and 3.12 with a README badge. Changed: the codebase is formatted with ruff and imports are sorted; unused imports removed; `docs/TRADING_LOGIC.md` rewritten for the core (70%), sentiment (20%) and disabled tactical sleeves. Reference `(#4)`. No dashes other than list hyphens.
   Verify: Keep a Changelog headings unchanged (`Added`, `Changed`, `Fixed`), no new version header, `rg -n "[\x{2013}\x{2014}]" CHANGELOG.md` returns nothing new.
   depends on: 4, 5, 6

8. **docs: update CLAUDE.md for ruff, tests and CI.**
   Files: `CLAUDE.md` (repo root) only. The user explicitly authorised this edit for this task.
   Changes: replace the "No pyproject.toml, no lint/format config, no `.github/workflows`" line with the current state (`pyproject.toml` holds ruff and pytest config, ruff pinned in `requirements-dev.txt`, CI in `.github/workflows/ci.yml` on 3.11 and 3.12 plus weekly). Add `ruff format .` and `ruff check .` to the Commands block. Update the Gotchas line that says the suite "only covers config and the paper lock" to list what the tests now cover. Note that `core/ibkr_auth.py` is excluded from ruff. Change nothing else.
   Verify: no en or em dashes added; commands in the file match the ones in the CI workflow.
   depends on: 4, 5

Parallelism: after task 3, tasks 4, 5 and 6 touch disjoint files (`tests/`; `.github/` and `README.md`; `docs/TRADING_LOGIC.md`) and can run in parallel. Tasks 7 and 8 touch different files and run after their dependencies.

## 5. Tests

Write `tests/conftest.py` first (offline guard and `.env` isolation), then `test_config.py` additions, then the pure-math modules (`test_signal_fusion.py`, `test_sizing.py`), then `test_scoring.py`, `test_sleeve_core.py`, `test_sleeve_sentiment_satellite.py`, and `test_sentiment.py` last (needs the fixture).

`tests/test_config.py` (extend, do not duplicate existing cases):
- `PAPER` values `"true"`, `"TRUE"`, `"1"`, `"yes"`, `"on"` with `IB_PORT=5000` pass.
- `PAPER` values `"false"`, `"0"`, `"no"`, `"off"`, `"maybe"` are refused with `match="PAPER=false"` (`_env_bool` treats unknown strings as false).
- `PAPER` unset and `IB_PORT` unset: `load_app_config()` gives `paper=True`, `port=5000`, passes.
- `PAPER=false` and `IB_PORT=4002` together: the PAPER error wins (checked first).
- `IB_PORT=5001` refused.

`tests/test_signal_fusion.py`:
- Bullish with sentiment: (2, 0.85, +0.40) gives 0.715; (2, 0.85, -0.80) gives 0.355.
- Missing sentiment: (2, 0.70, None) gives 0.70 and reason starts with `"ta-only"`.
- Bearish never flips: (0, 0.80, +0.90) gives 0.29 and `ta_prediction == 0`.
- Clamp to 0: (2, 0.20, -1.0) gives 0.0. Clamp to 1 is impossible with normalised weights; assert result is at most 1.0 for (2, 1.0, 1.0).
- Weight renormalisation: `ta_weight=1.4, sentiment_weight=0.6` equals the 0.7/0.3 result.
- Class 1 counts as bullish (binary classifier).

`tests/test_sizing.py`:
- `realized_vol`: series shorter than `lookback + 1` gives NaN; known series gives population std (`ddof=0`) of the last `lookback` returns.
- Not bullish (prediction 0) gives 0 shares and reason `"prediction=0 not bullish"`.
- Confidence below floor gives 0 shares.
- Signal weight ramp: confidence 0.775 with floor 0.55 gives `signal_weight == 0.5`.
- Basic sizing: equity 100,000, vol 0.005, confidence 0.775, close 100, default params: vol_scale 1.0, raw 50,000, capped at 30% to 30,000, 300 shares.
- Vol scale clamp: vol 0.0001 gives 5.0; vol 1.0 gives 0.1.
- NaN vol gives 0 shares and reason `"realized_vol unavailable"`.
- Zero vol is floored to 1e-6 and clamps to the max scale (document current behaviour).
- Rounds to 0: close larger than the capped value gives reason `"rounded to 0 shares"`.
- Gross cap: three symbols each sized to 30,000 with `max_gross_pct=0.5` are scaled by 50,000/90,000, reasons contain `"gross-capped"`, sum of values at most 50,000, zero-share decisions stay unchanged.
- Output order equals input order. Pass a list (not a generator; see section D).

`tests/test_scoring.py`:
- `_rank_to_signed`: three values give -1, 0, +1; ties average; one valid value gives all zeros; `None` entries get 0.
- `score_universe` momentum: three linear series with different slopes and the small config; best slope gets momentum +1; short history gets 0.
- `_trend_score`: last close 10% or more above the MA gives 1.0; below by 10% gives -1.0; history shorter than `ma_days` gives 0.0.
- `_macro_score`: all-None snapshot gives 0.0; VIX 30 only gives -1.0 for `equity_us_large` and +1.0 for `bond_long`; yield curve 2.0 gives +1 for equity and -1 for `bond_long`; `cash` with only yield curve data gives 0 (no matching rule, but the component still counts).
- Composite equals `0.4*mom + 0.3*macro + 0.3*trend` for each asset, and results are sorted descending.
- `_check_risk_off`: SPY below MA and VIX 30 gives `(True, reason)`; SPY below MA with VIX 20 gives False; SPY above MA with VIX 30 gives False; VIX None gives False; SPY history shorter than the MA window gives False.
- `select_holdings`: risk-off gives `{"TLT": 0.5, "IEF": 0.5}`; normal gives equal weights for the top `hold_top_n`; empty scores give `{}`.

`tests/test_sleeve_core.py`:
- `CoreSleeve` raises `ValueError` when weights sum to 0.9.
- `should_evaluate` with `now=datetime(2026, 11, 13, 12, tzinfo=timezone.utc)` (Friday): no prior rebalance gives `(True, "calendar: Fri close")`; `last_rebalance_ts` the same day gives `(False, "already rebalanced today")`.
- `now` on Wednesday 2026-11-11 gives False with `"not rebalance day (today=Wed, want=Fri)"`.
- Drift: holdings `{"SPY": 60, "TLT": 40}`, prices 100, equity 10,000 gives True with reason starting `"drift: "` even on Wednesday; holdings `{"SPY": 50, "TLT": 50}` gives no drift trigger.
- Drift ignores zero-share holdings; empty holdings or equity 0 never drift.
- `evaluate`: equity 10,000, closes at 100, top 2 gives 50 target shares each; a held symbol outside the top set gets `target_shares == 0`, negative delta and reason `"out of top set"`; a symbol with no close series is skipped; risk-off path gives reason `"risk-off bond bucket"` for TLT and IEF (provide IEF closes).

`tests/test_sleeve_sentiment_satellite.py` (use `SentimentSatelliteConfig(enabled=True)` defaults and fixed `now`):
- Entry: score 0.6, 6 articles, price 100, sleeve equity 20,000 gives 10 shares, reason starts `"enter:"`.
- Boundary: score exactly 0.4 enters; 0.39 is skipped; 4 articles skipped; negative score -0.8 skipped as bearish with `longs_only`.
- `sentiment_score=None` skipped with `"no fresh sentiment"`.
- Symbol outside `cfg.universe` is ignored (not in entries or skipped).
- Ranking and cap: six qualifying symbols with two already held gives only two entries, the highest scores first.
- Already holding gives skip `"already holding"`.
- Rounds to 0: price 2,000 with 1,000 target gives skip `"rounds to 0 shares"`.
- Exit by timer: opened 10 days before `now` exits; 9 days stays. Naive `opened_at` is treated as UTC.
- Exit by stop: entry 100, price 95 exits (5% loss); price 95.01 stays.
- Exits free slots: four positions, one exits, one new entry allowed.
- `allow_entries=False` gives no entries; `allow_exits=False` gives no exits.
- Exit with no input for the symbol uses `entry_price` as exit price.

`tests/test_sentiment.py`:
- `StubProvider().fetch_batch(["SPY", "AAPL"])` gives score 0, count 0, source `"stub"`.
- `get_provider("stub")` returns `StubProvider`; `get_provider("nope")` raises `ValueError`; `get_provider("alpha_vantage")` without the env var raises `ValueError` (env cleared by conftest).
- `AlphaVantageProvider()` with `monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key")` picks up the key.
- `fetch_batch([])` returns `[]` without calling `urlopen`.
- Fixture parsing through `fetch_batch` with patched `urlopen`: expected values from Design B table; the recorded URL contains `function=NEWS_SENTIMENT`, `tickers=AAPL%2CMSFT%2CNVDA%2CGOOGL` and `apikey=test-key`.
- Throttle: payload `{"Information": "rate limit"}` and payload `{"Note": "..."}` raise `RuntimeError` with `"throttled"`.
- Transport error: fake `urlopen` raising `URLError("down")` gives `RuntimeError` with `"request failed"`.
- `"feed": null` gives zero readings for every symbol.
- `_normalize`: 0.25 gives 0.5, 0.8 gives 1.0, -0.9 gives -1.0.

Fixtures needed: only `tests/fixtures/alpha_vantage_news_sentiment.json`. Everything else is built in code.

## 6. Risks

- **Touching `core/ibkr_auth.py` by accident.** Mitigation: `extend-exclude` plus `force-exclude = true`; check `git diff main -- core/ibkr_auth.py` after tasks 2 and 3 and in review. Never run `ruff ... --no-force-exclude` or `ruff format core/` with a different config.
- **Large format diff hides behaviour changes.** Mitigation: task 2 is format only and task 3 is import-only autofixes, each reviewed alone. Optional follow-up: `.git-blame-ignore-revs` with the task 2 SHA.
- **Import reordering (rule `I`) changes import side effects.** No module here has import-time side effects that depend on order, but task 3 includes an import smoke test of every entry point and, if available, a dry-run `--once` cycle.
- **Ruff version drift.** A newer ruff can reformat code and fail CI. Mitigation: exact pin in `requirements-dev.txt`; bump it in its own commit with a re-format.
- **Unpinned runtime deps in CI.** `requirements.txt` uses `>=` bounds, so CI resolves the newest pandas, numpy and xgboost. A new major release can break tests without a code change. Accept for now; follow-up issue to add a lock or constraints file.
- **Python 3.12.** `datetime.utcnow()` in `core/macro.py` warns on 3.12 but is not called by tests. Do not set `filterwarnings = error`.
- **Developer `.env` leaking into tests.** Mitigated by the conftest `load_dotenv` patch and env cleanup. Without it, a local `ALPHA_VANTAGE_API_KEY` would break the "missing key" test.
- **`pyproject.toml` without `[project]`.** `uv sync` would fail with no project table, but the repo workflow uses `uv pip install -r ...`, which is unaffected. CI calls `.venv/bin/...` directly. The Dockerfile uses `pip install -r requirements.txt` and copies `pyproject.toml` harmlessly.
- **Security.** The workflow has `contents: read` only, uses no secrets, and does not run on `pull_request_target`. The fixture contains no API key and only `example.com` URLs. The badge URL only names the public repo.
- **Deployment.** None of these tasks change runtime behaviour. The home server needs no action; the next `git pull && docker compose --profile auto up -d --build` picks up formatted code. Rollback: revert the branch merge; each task is a separate commit and can be reverted alone (revert task 3 before task 2 if both).

Follow-up issues to open (findings, not fixed here):
1. Core calendar trigger uses the UTC weekday (`CoreSleeve.should_evaluate`) while the runner gates on Eastern regimes; rebalances can fire Thursday evening ET. `is_core_rebalance_window` is effectively unused.
2. `portfolio.reentry_cooldown_sec` is configured and displayed but not enforced by any runner.
3. Satellite hold timer counts calendar days; YAML says trading days.
4. Tactical sleeve is not wired into `live/portfolio_runner.py` even when `enabled: true`.
5. `size_portfolio` iterates its `Iterable` argument twice; accept `Sequence` or materialise with `list()`.
6. `AlphaVantageProvider.lookback_hours` is unused.
7. `datetime.utcnow()` in `core/macro.py` (deprecated in 3.12).
8. Enable ruff `UP` and `B` rules.
9. README "Run it" still uses `python -m venv` and `pip`; align with uv.
10. (Now in scope as task 8, do not open as an issue.) Update the stale "No pyproject.toml ..." line in `CLAUDE.md`.
11. Pin or lock runtime dependencies for reproducible CI.
12. Holidays and exact DST handling in `core/market_hours.py`.

## 7. Open questions

See the top of this file (questions 1 to 6). Two more for the reviewer:

7. Should the CI workflow also run on a weekly `schedule` to catch dependency drift (Risks, unpinned deps)? Default: no, keep `push` and `pull_request` only.
8. Should `docs/TRADING_LOGIC.md` keep a short "Legacy: All-Weather long_term sleeve" note, since `core/sleeves/long_term.py` and `LongTermSleeveConfig` still exist? Default: one sentence saying the code exists and is not used by the portfolio runner.

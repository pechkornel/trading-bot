# Small bug batch (#9, #10, #11, #13)

Branch `fix/small-bug-batch` (from main `5c22406`, 169 offline tests). One PR that closes #9, #10, #11 and #13.


## Approval

Approved by the lead on 2026-11-24 while the user was away, with all Decisions as written (including the lookback_hours default of 24). The user asked for unattended work within this scope; every choice is listed in the PR.

## Decisions (taken by the lead, all reversible)

1. **#10 lookback default.** Send `time_from` and raise the `AlphaVantageProvider` default `lookback_hours` from 6 to 24. Reason: the satellite acts daily, `staleness_sec` is 86400, and `min_article_count: 5` gates entries. A 6 hour window over 9 tickers would often leave symbols under 5 articles and quietly stop satellite entries. Reverting is a one-line default change. Not wiring it into `config/symbols.yaml` in this PR (follow-up).
2. **#10 clock injection.** Constructor keyword `clock: Callable[[], datetime] | None = None` on `AlphaVantageProvider`, defaulting to `datetime.now(timezone.utc)`. `fetch_batch(symbols)` keeps its signature, so the `SentimentProvider` protocol and `live/sentiment_worker.py` are unchanged. A naive datetime from the clock raises `ValueError` (no guessing at local time). `lookback_hours=None` omits `time_from` (escape hatch); `lookback_hours <= 0` raises `ValueError`.
3. **#11 aware UTC.** `MacroSnapshot.as_of` becomes timezone-aware UTC (`datetime.now(timezone.utc)`). The only other producer, `tests/conftest.py::make_macro`, already builds an aware value, and no code in `core/`, `live/` or `dashboard/` reads `as_of` (checked with `rg as_of`), so nothing compares naive with aware.
4. **CHANGELOG and Known gaps in one last task.** All CHANGELOG lines, the test count and the removal of the three fixed items from `docs/TRADING_LOGIC.md` section 8 go in task 5, so tasks 1 to 4 share no files.

## 1. Goal

Fix four small issues: `size_portfolio` loses decisions for a generator input under the gross cap (#9); `AlphaVantageProvider.lookback_hours` is never sent (#10); `core/macro.py` uses deprecated `datetime.utcnow()` (#11); the README "Run it" section uses `python -m venv` and `pip` (#13). A failing test comes first for each code fix.

Out of scope: a `lookback_hours` key in `SentimentConfig`/`config/symbols.yaml`, any change to the paper lock, `DRY_RUN` default or `--live`, `core/ibkr_auth.py`, the stale "no pyproject.toml" line in the project CLAUDE.md (follow-up).

## 2. Current state

- `core/sizing.py::size_portfolio(signals: Iterable[SymbolSignal], equity, params)`: builds `decisions` from `signals`, then in the gross-cap branch iterates `zip(decisions, signals)` again. A generator is exhausted, so it returns `[]`. Only caller: `core/sleeves/tactical.py` (passes a list). Tests: `tests/test_sizing.py` (`sig()` helper, `test_gross_cap_scales_positions_down`).
- `core/sentiment.py::AlphaVantageProvider.__init__(api_key=None, lookback_hours=6)` stores `lookback_hours`; `fetch_batch` builds `params` (`function`, `tickers`, `limit`, `apikey`) and calls `urlopen(Request(url))` without `time_from`. Built by `get_provider("alpha_vantage")` with no arguments. `tests/test_sentiment.py` patches `sentiment.urlopen` with `FakeUrlopen` and asserts substrings of `fake.requests[0].full_url`; helper `provider()` returns `AlphaVantageProvider(api_key="test-key")`.
- `core/macro.py::MacroFetcher.snapshot` sets `as_of=datetime.utcnow()` (line 122). The only `utcnow` in code (`rg utcnow --glob '!.venv'`); the other hits are docs. No macro tests exist.
- `README.md` lines 75 to 85, "Run it": `python -m venv`, `pip install`, bare `python -m` and `uvicorn`. CI (`.github/workflows`) runs `uv venv`, `uv pip install -r requirements-dev.txt`, `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .`, `.venv/bin/python -m pytest -q`.
- `docs/TRADING_LOGIC.md` section 8 "Smaller code notes" lists all three code bugs.

## 3. Design

- **#9:** first line of `size_portfolio`: `signals = list(signals)`. Keep the `Iterable` hint (the public contract stays permissive). Rejected: changing the hint to `Sequence` (type hints are not enforced, a generator would still break).
- **#10:** new module helper `_time_from(now: datetime, lookback_hours: int) -> str`: raises `ValueError` if `now.tzinfo is None`, else returns `(now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y%m%dT%H%M")` (Alpha Vantage format, UTC). `fetch_batch` adds `params["time_from"]` when `self.lookback_hours is not None`. Update the class docstring endpoint example and the `lookback_hours` meaning. Rejected: a `now=` argument on `fetch_batch` (changes the protocol); monkeypatching a module-level `_utcnow` (hidden global, worse for tests).
- **#11:** `from datetime import datetime, timezone`; `as_of=datetime.now(timezone.utc)`. Update the `as_of` field comment to "aware UTC". Rejected: naive UTC via `datetime.now(timezone.utc).replace(tzinfo=None)` (keeps the old ambiguity, and conftest already uses aware).
- **#13:** replace the README block with:

```sh
# Setup. macOS/Linux shown; on Windows use .venv\Scripts\python and .venv\Scripts\ruff
uv venv --python 3.11
uv pip install -r requirements.txt          # add requirements-train.txt to train models
uv pip install -r requirements-dev.txt      # pytest and ruff
cp .env.example .env                        # fill in your own values

.venv/bin/python -m live.portfolio_runner --once            # one dry-run cycle of every enabled sleeve
.venv/bin/python -m uvicorn dashboard.app:app --port 8765   # dashboard
docker compose --profile auto up -d                         # or everything in Docker

# Lint and tests (what CI runs)
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

  `python -m uvicorn` keeps the dashboard on the venv interpreter (`dashboard/process.py` spawns workers with `sys.executable`). No `uv run` or `uv sync` (they would create `uv.lock`).

## 4. Tasks

Commands (macOS/Linux): `.venv/bin/ruff format . && .venv/bin/ruff check . && .venv/bin/python -m pytest -q`. Never `uv run`/`uv sync`. Each code task: write the test, run it and see it fail on current code, then fix, then the full suite.

1. **fix: size_portfolio handles generator input (#9).** Test in `tests/test_sizing.py`: `test_gross_cap_with_generator_input` passes `(s for s in [sig("A"), sig("B"), sig("C")])` with `PortfolioParams(max_gross_pct=0.5)` and asserts 3 decisions, symbols `["A","B","C"]`, each reason contains `gross-capped`. Fails today (returns `[]`). Fix in `core/sizing.py`. Commit `fix: size_portfolio handles a generator input (#9)`.
   independent

2. **fix: send time_from to Alpha Vantage (#10).** Tests in `tests/test_sentiment.py` (fixed clock, e.g. `lambda: datetime(2026, 11, 24, 12, 30, tzinfo=timezone.utc)`; make the `provider()` helper pass a fixed clock too): (a) default provider URL contains `time_from=20261123T1230` (24 h); (b) parametrized: `lookback_hours=6` at 03:00 UTC gives `20261123T2100` (crosses midnight), and a `UTC+2` aware clock at 14:30 local with 6 h gives `20261124T0630` (converted to UTC); (c) `lookback_hours=None` gives no `time_from` in the URL; (d) `lookback_hours=0` raises `ValueError`; (e) naive clock makes `fetch_batch` raise `ValueError`. All fail today (no `clock` keyword, no `time_from`). Fix in `core/sentiment.py`. Commit `fix: send lookback_hours to Alpha Vantage as time_from (#10)`.
   independent

3. **fix: replace datetime.utcnow in core/macro.py (#11).** New `tests/test_macro.py`: `MacroFetcher(MacroConfig(fred_series={}, yf_tickers={})).snapshot()` makes no network call (all fetchers return `None`); assert `snap.as_of.utcoffset() == timedelta(0)` and `before <= snap.as_of <= after` with aware `datetime.now(timezone.utc)` bounds. Fails today (`utcoffset()` is `None`). Fix in `core/macro.py`. Commit `fix: use aware UTC for MacroSnapshot.as_of (#11)`.
   independent

4. **docs: README "Run it" uses uv (#13).** Replace the block in `README.md` as in Design. Verify by reading, then `rg -n "[\x{2013}\x{2014}]" README.md` returns nothing and `rg -n "uv run|uv sync|pip install -r" README.md` shows only `uv pip install`. Commit `docs: use uv in the README Run it section (#13)`.
   independent

5. **docs: changelog and known gaps.** `CHANGELOG.md` `[Unreleased]` `### Fixed`, one short line each: generator input to `size_portfolio` (#9); Alpha Vantage request sends `time_from`, default lookback 24 h (#10); `MacroSnapshot.as_of` is aware UTC (#11); README setup uses uv (#13). Update the test count in the `### Added` suite line from 169 to the real number from `pytest -q` (expected 177: +1 sizing, +6 sentiment, +1 macro). Remove the three fixed bullets from `docs/TRADING_LOGIC.md` section 8 "Smaller code notes" (drop the heading if empty). Dash check on both files. Commit `docs: changelog for the small bug batch`.
   depends on: 1, 2, 3, 4

## 5. Tests

Write the failing test first in tasks 1 to 3. Edge cases: generator under the gross cap (the only path that iterates twice); `time_from` across midnight, from a non-UTC aware clock, disabled with `None`, invalid lookback, naive clock. Fixtures: none new; reuse `sig()`, `FakeUrlopen`/`patch_urlopen` and the existing Alpha Vantage JSON fixture. `conftest.py` blocks network, so the macro test must use empty `fred_series`/`yf_tickers`. Final check: full suite green, `ruff format --check .` and `ruff check .` clean, and one dry-run `python -m live.portfolio_runner --once` against a paper gateway if available (never `--live`).

## 6. Risks

- **Alpha Vantage behaviour change.** Today the call returns the latest 100 articles with no time bound. With `time_from` (24 h) it returns only articles in the window: on quiet days fewer articles, lower `confidence` (`count / 20`), more `skip: articles < 5` in the satellite, and scores that reflect only the last day. On busy days the result is about the same (the 100 limit still applies). Watch the sentiment rows in the dashboard for a few days after deploy.
- **Aware vs naive.** `as_of` is now aware. No current reader, but any future code that subtracts it from a naive datetime will raise `TypeError`; the field comment documents aware UTC. `_time_from` rejects naive input on purpose.
- **Deployment:** none beyond the normal update (`git pull && docker compose --profile auto up -d --build`). No schema or config change.
- **Rollback:** revert the single commit per issue. For #10 only, a quicker option is `lookback_hours=None` (no `time_from`) or reverting the default to 6.

## 7. Open questions

None blocking. Follow-ups to file: expose `lookback_hours` in `SentimentConfig`/`config/symbols.yaml`; fix the stale "no pyproject.toml, no workflows" line in the project CLAUDE.md.

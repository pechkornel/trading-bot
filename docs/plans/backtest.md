# Plan: core sleeve backtest (`backtest/` package)

Branch: `feat/backtest` (from `main` at 5c22406, 169 offline tests). The open PRs #30 (`feat/home-server-prep`) and #31 (`fix/trading-days-and-holidays`) are not on this branch. See Risks for the files they share with this plan.

Every task is test first: write the test, see it fail, write the code, see it pass. Verification for every code task (macOS, zsh, repo root):

```sh
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

Never `uv run` or `uv sync` (they create `uv.lock`). Install with `uv pip install -r ...`.


## Approval

Approved by the lead on 2026-11-24 while the user was away, with all Decisions and Open-question defaults as written. The user asked for unattended work within this scope; every choice is listed in the PR.

## Decisions (taken by the lead on the user's behalf; all reversible)

D1. **HYG and LQD are downloaded too.** The live macro score uses `credit_ratio_trend_60d` (built from HYG/LQD in `MacroFetcher.snapshot`) for every equity and bond bucket in `core.scoring._macro_score`. Leaving it `None` would change the macro score of 7 of the 10 ETFs (the average divides by `components`, so dropping a component reweights the rest). Adding two tickers is the faithful choice. HYG starts in April 2007, before BIL (May 2007), so it should not move the start date; the start-date rule checks this anyway. Reverse by removing `hyg`/`lqd` from the replay macro config.

D2. **The macro snapshot is built by the bot's own `MacroFetcher.snapshot()`.** A subclass `HistoricalMacroFetcher` overrides only the two data methods (`_fred_series`, `_yf_series`) to return cached series sliced as of the decision date. The credit ratio, the 60-day helpers (`_pct_change_60d` and friends) and the `MacroSnapshot` assembly all run unchanged. The override keeps the live 365-calendar-day Yahoo window (`lookback_days=365`).

D3. **DFF (fed funds) is not downloaded.** `fed_funds` is not read by `_macro_score` or `_check_risk_off`. The replay config keeps only `fred_series={"yield_curve": "T10Y2Y"}`, so `snapshot()` returns `fed_funds=None` without an error, and `complete` is always False. This has no effect on scores: the only use of `complete` is the neutral guard `if not m.complete and m.vix is None and m.yield_curve is None`, which also needs VIX and the curve to be missing. Documented in results.md.

D4. **Signals use Yahoo `Close` (split adjusted, not dividend adjusted); P&L uses `Adj Close`.** This matches the live bot: CPGW daily bars are split adjusted price closes, and the live Yahoo chart endpoint returns the same `close` for VIX, DXY, HYG and LQD. Adjusted closes are never passed to `CoreSleeve`, `score_universe` or the macro fetcher.

D5. **Decision clock.** Each trading day `t` is evaluated once with `now = datetime(t.year, t.month, t.day, 21, 0, tzinfo=timezone.utc)`. That is 16:00 EST or 17:00 EDT, so `core.market_hours.to_eastern(now).date() == t` under both the current approximate DST rule and the exact one in PR #31.

D6. **Holdings passed to `CoreSleeve` are synthetic fractional shares.** With current weights `w`, raw closes `p` at `t` and a constant notional `E = 1e9`, the replay passes `current_holdings = {s: w[s] * E / p[s]}`, `prices = p`, `sleeve_equity = E`. `_max_drift` then sees the true current weights. Target weights are read from `CoreDecision.target_weight` (0 when the cooldown blocks a buy), not from `target_shares`, so the integer truncation in `evaluate` does not matter. Live uses integer shares and DAY limit orders at plus or minus 5 bps; the backtest uses fractional weights filled at the next close. Documented.

D7. **Sleeve equity is marked to market.** Live uses a fixed `portfolio.starting_equity * allocation_pct` (known gap "Static starting equity" in `docs/TRADING_LOGIC.md` section 8), so live drift is measured against a stale budget. The replay measures weights against current equity, which is what the drift rule intends. This deviation is documented in results.md and listed as a follow-up finding, not fixed here.

D8. **Cash earns 0.** Weight not invested (before the first fill, and any cooldown-blocked weight) earns nothing, as in the live paper account logic. The cooldown is called with live semantics (exit time = decision `now` of a sell to 0, `reentry_cooldown_sec` from `load_portfolio()`), but with one decision per day at a fixed 21:00 UTC clock the next decision is exactly 86400 s later, which `cooldown_remaining` treats as free. So it never binds in the replay. Documented as such, not hidden.

D9. **Execution and costs.** Decide at the close of `t` with data up to and including `t`; fill at the close of the next calendar day `t+1`. Turnover on a fill day is `sum(|w_target - w_drifted|)` over assets (cash excluded), so the first buy from cash is 1.0 and a full switch is 2.0. Cost = `cost_bps / 10_000 * turnover`, taken from equity on the fill day. The first entry is charged for every portfolio (strategy and benchmarks).

D10. **The strategy starts like the live sleeve: in cash.** With no holdings and no last rebalance, `should_evaluate` fires only on the rebalance weekday (Friday), so the strategy waits up to four trading days for its first decision. Benchmarks decide at the start date and fill the next day. All curves start at 1.0 on the start date.

D11. **Benchmarks.** `spy_buy_hold`: buy SPY at the first fill, pay one entry cost, never rebalance. `sixty_forty`: 60% SPY, 40% AGG. `equal_weight`: 10% each of the same 10 core ETFs, BIL included. The two rebalanced benchmarks decide at the last trading day of each month and fill at the first trading day of the next month, with the same cost rate as the strategy run they are compared with.

D12. **Ablations are config overrides only** (`dataclasses.replace` on the loaded `CoreSleeveConfig`, plus the cost argument). No bot code changes:
- `momentum_only`: weights 1.0 / 0 / 0. Risk-off switch stays on.
- `no_macro`: macro weight 0, momentum and trend renormalised proportionally from the loaded config (0.40/0.70 and 0.30/0.70, about 0.571 and 0.429). VIX still feeds the risk-off switch.
- `no_risk_off`: `risk_off_vix_threshold=float("inf")`. `_check_risk_off` tests `macro.vix > cfg.risk_off_vix_threshold`, which is never true for infinity, so no flag in `core/scoring.py` is needed. A test pins this.
- `top_3`, `top_5`: `hold_top_n` 3 and 5.
- `cost_0`, `cost_20`: base config at 0 and 20 bps. The benchmarks are recomputed at the same cost and shown next to them.

D13. **Start date** is computed, not hard coded: the first trading day on which every required ticker (core universe, risk-off universe, SPY, AGG, BIL, `^VIX`, `DX-Y.NYB`, HYG, LQD) has at least `min_bars` raw closes up to and including that day. `min_bars = max(momentum_lookback_days + 1, trend_ma_days, risk_off_spy_ma_days)`, which is 253 for the live config. T10Y2Y must have at least one observation before the start date. The start date is reported in results.

D14. **Trading calendar** = dates on which all 10 core ETFs have both `Close` and `Adj Close`. Macro tickers are joined as of (last value on or before `t`), so DXY's different holidays do not drop days. Days dropped from the calendar are counted and reported.

D15. **History passed to scoring** is the trailing 504 raw bars up to `t` (about the live `history_duration: "2 Y"`). `score_universe` only looks at the last 253 (momentum) and 200 (MA) bars, so this gives the same numbers as the full history and keeps run time down.

D16. **Cache** under `data/cache/backtest/` as CSV (one file per ticker or series plus `manifest.json` with download time, yfinance version, rows, first and last date). CSV avoids a pyarrow dependency for about 15 small files. `data/cache/` goes in `.gitignore`, `data/` in `.dockerignore`. `--refresh` re-downloads everything.

D17. **Dependencies** in a new `requirements-backtest.txt` (`-r requirements.txt`, `yfinance`, `matplotlib`). The Docker image and CI do not install it. `backtest.engine`, `backtest.metrics`, `backtest.macro_replay`, `backtest.strategy`, `backtest.ablations`, `backtest.report` (except the plot function) and `backtest.run` import without yfinance or matplotlib; both are imported inside the functions that need them. CI runs the backtest tests offline with `requirements-dev.txt` only; no workflow change is needed because the tests live in `tests/`.

D18. **Risk-off share** = share of trading days on which the portfolio is positioned on a risk-off decision (from the fill day of a risk-off decision to the next fill). It comes from `CoreCycleResult.risk_off`, so no private scoring function is called for it.

D19. **Sharpe and Sortino** use BIL daily adjusted returns as the risk-free rate (as the user asked). Both return `None` when the denominator is 0.

D20. **DXY and VIX as of the close of `t`.** VIX settles at 16:15 ET and DXY at 17:00 ET, slightly after the 16:00 equity close. This is what the brief asks for; the effect on a 60-day trend and a level vs 25 is tiny. It is listed for the data-scientist review and documented.

D21. **An ADR is added** (`docs/adr/0002-own-backtest-replay.md`) for "own replay that calls the bot's code, not vectorbt or backtrader". Number 0002 assumes PR #30's 0001 lands first; renumber at rebase if not.

D22. **No `CLAUDE.md` change in this PR.** The new command goes in the README. Adding it to `CLAUDE.md` is a suggested follow-up for the user.

## 1. Goal

Add a `backtest/` package that replays the live core sleeve day by day from mid 2008 to the latest data, by calling the bot's own `CoreSleeve.should_evaluate`, `CoreSleeve.evaluate` (which calls `score_universe` and `select_holdings`) and `MacroFetcher.snapshot`, never a copy of their logic. It compares the strategy with three benchmarks under the same costs, runs six ablations, and writes `docs/backtest/results.md`, `docs/backtest/equity_curve.png` and `docs/backtest/results.json` with `python -m backtest.run`. The README gets a "Results" section with honest limitations. The engine is tested offline on tiny synthetic data with known answers.

Out of scope:
- The sentiment satellite and tactical sleeves (no historical news data; tactical is off).
- Parameter search or optimisation of any kind. Parameters are the ones in `config/symbols.yaml` today.
- Changes to `core/`, `live/`, `dashboard/`, the paper lock, `DRY_RUN`, `--live` or `core/ibkr_auth.py`. The backtest never opens a broker connection and does not import `live.*` or `core.cpgw_client`.
- Fixing the live static-equity drift gap (D7) or the Friday-morning vs close timing (see Risks). Both become follow-up issues.
- Intraday data, slippage models, taxes, integer share sizing.

## 2. Current state

- `core/sleeves/core.py`: `CoreSleeve(cfg, reentry_cooldown_sec)`. `should_evaluate(*, last_rebalance_ts, current_holdings, prices, sleeve_equity, now)` applies the once-per-Eastern-date rule first, then drift (`_max_drift`: largest `|held_w - 1/N_held|` vs `drift_threshold` 0.08), then the weekday (`rebalance_day_of_week` 4 = Friday). `evaluate(*, closes, macro, current_holdings, sleeve_equity, reason, last_exit_ts, now)` returns `CoreCycleResult(triggered, reason, risk_off, scores, decisions)`; each `CoreDecision` has `target_weight` (0 if the re-entry cooldown blocks a buy back).
- `core/scoring.py`: `score_universe(closes, macro, cfg)` computes cross-sectional momentum rank (`momentum_lookback_days` 252, needs 253 bars, `iloc[-253]`), `_macro_score(bucket, macro)` (yield curve, VIX, `dxy_trend_60d`, `credit_ratio_trend_60d`), `_trend_score` (price vs `trend_ma_days` 200 MA, plus or minus 10% maps to plus or minus 1), and `_check_risk_off` (SPY below its 200-day MA AND `macro.vix > risk_off_vix_threshold` 25). `select_holdings` equal-weights the top `hold_top_n` (4) or `risk_off_universe` (TLT, IEF).
- `core/macro.py`: `MacroSnapshot` dataclass; `MacroFetcher(cfg).snapshot()` calls `_fred_series(id)` for `yield_curve` and `fed_funds`, and `_yf_series(ticker, lookback_days=365)` for `vix`, `dxy`, `hyg`, `lqd`; builds the HYG/LQD ratio and fills fields via `_last`, `_pct_change_60d` (vs `iloc[-60]`), `_pp_change_60d`, `_window_mean`. `_fred_series` downloads the public FRED CSV (no key).
- `core/config.py`: `CoreSleeveConfig`, `MacroConfig`, `PortfolioConfig`, `load_phase8_sleeves()`, `load_macro_config()`, `load_portfolio()` (none of these read `.env`).
- `core/sleeves/cooldown.py`: `cooldown_remaining(exit_ts, now, cooldown_sec)` returns None when `left <= 0`.
- `core/market_hours.py`: `to_eastern(utc)`.
- `config/symbols.yaml`: core universe `[SPY, IWM, EFA, VWO, TLT, IEF, AGG, GLD, DBC, BIL]`, weights 0.40/0.30/0.30, `hold_top_n: 4`, `drift_threshold: 0.08`, `rebalance_day_of_week: 4`; `portfolio.reentry_cooldown_sec: 86400`; `macro.yf_tickers` includes `hyg: HYG`, `lqd: LQD`.
- `live/portfolio_runner.py`: `_cycle_core` passes fixed `sleeve_equity = starting_equity * allocation_pct`, integer holdings from SQLite, the last rebalance time and `last_exit_times("core")`; `_execute_core_leg` sends DAY limit orders at close times 1.0005 (buy) or 0.9995 (sell). The core sleeve is evaluated from the first OPEN cycle of Friday onwards, so live decides on Friday intraday bars, not at the close.
- `tests/conftest.py`: autouse network block, `make_macro`, `series`, `linear_closes`. `tests/test_sleeve_core.py`: `small_cfg()` with short lookbacks (pattern to reuse).
- `pyproject.toml`: ruff (line length 100, isort `known-first-party` list) and pytest (`testpaths = ["tests"]`). `.github/workflows/ci.yml` installs `requirements-dev.txt` only.
- `docs/TECH_STACK.md` line 163 says backtesting libraries are deferred "if we add a real backtester".
- No `backtest/`, `data/` or `docs/backtest/` exists yet.

## 3. Design

### Package layout

```text
backtest/__init__.py      docstring only
backtest/metrics.py       pure pandas/numpy metrics
backtest/engine.py        daily portfolio loop, calendar, start date, benchmark decide functions
backtest/macro_replay.py  HistoricalMacroFetcher(MacroFetcher)
backtest/strategy.py      CoreReplay: wraps CoreSleeve as a decide function
backtest/ablations.py     Variant list and build_cfg()
backtest/data.py          yfinance + FRED download, CSV cache, manifest, validation (lazy yfinance)
backtest/report.py        results dict, JSON, Markdown, PNG (lazy matplotlib)
backtest/run.py           run_all() (pure) and main() (CLI)
```

### Interfaces

`backtest/engine.py`
- `DecideFn = Callable[[pd.Timestamp, dict[str, float]], dict[str, float] | None]`: called at the close of `t` with post-fill weights; returns target weights to fill at the next close, or None.
- `simulate(adj_close: pd.DataFrame, calendar: pd.DatetimeIndex, decide: DecideFn, cost_bps: float) -> SimResult`. `SimResult` fields: `equity: pd.Series`, `weights: pd.DataFrame` (post-fill, per day), `turnover: pd.Series`, `costs: pd.Series`, `fills: list[tuple[pd.Timestamp, dict[str, float]]]`.
- Loop: day 0 equity 1.0, all cash, `pending = decide(t0, {})`. For each later day `t_i`: (1) grow each weight by `adj_close[t_i] / adj_close[t_{i-1}]`, update equity, renormalise weights by the new equity (cash weight follows); (2) if `pending`, turnover and cost per D9, weights become the target, cash = 1 minus the sum; (3) `pending = decide(t_i, weights)`. A decision on the last day is never filled. A NaN adjusted close for a held symbol raises `ValueError` naming symbol and date (no forward fill).
- `trading_calendar(raw_close, adj_close, symbols) -> pd.DatetimeIndex`.
- `min_bars_for(cfg: CoreSleeveConfig) -> int`.
- `first_start_date(raw_close, symbols, min_bars, calendar) -> pd.Timestamp`; raises `ValueError` listing the short tickers if no date qualifies.
- `buy_and_hold(symbol) -> DecideFn`; `monthly_rebalance(weights, calendar) -> DecideFn` (decides on the first call and on every `t` whose next calendar day is in a new month).

`backtest/macro_replay.py`
- `HistoricalMacroFetcher(yahoo_close: pd.DataFrame, fred: dict[str, pd.Series], cfg: MacroConfig)`. `set_asof(t)`. `_fred_series(series_id)` returns observations dated strictly before `t` (one-day lag: FRED posts T10Y2Y for day `t` on the next business day). `_yf_series(ticker, lookback_days=365)` returns non-NaN raw closes with `t - 365 days < date <= t`. Missing ticker or series returns an empty Series (never raises, so `snapshot()` prints nothing).
- `replay_macro_config(live: MacroConfig) -> MacroConfig`: same `yf_tickers`, `fred_series` reduced to `yield_curve`.

`backtest/strategy.py`
- `decision_time(t) -> datetime` (D5).
- `CoreReplay(cfg, raw_close, macro_fetcher, reentry_cooldown_sec, history_bars=504, notional=1e9)`, method `decide(t, weights)`:
  1. `prices` = raw close at `t` for held symbols; `holdings` per D6.
  2. `CoreSleeve.should_evaluate(last_rebalance_ts=self.last_ts, ...)`; return None if false.
  3. `closes` = `{s: raw_close[s].loc[:t].dropna().iloc[-history_bars:]}` for `cfg.universe`, `cfg.risk_off_universe` and SPY.
  4. `macro_fetcher.set_asof(t)`; `macro = macro_fetcher.snapshot()`.
  5. `CoreSleeve.evaluate(..., last_exit_ts=self.exits, now=now)`; targets = positive `target_weight`s.
  6. `self.last_ts = now` on every evaluation (live logs a rebalance row on every evaluation); a held symbol with `target_shares == 0` records `self.exits[sym] = now`.
  7. Append a `DecisionRecord(date, reason, risk_off, targets, top_scores)`.
- `risk_off_days(calendar, fills, decisions) -> pd.Series[bool]` (D18).
- `trigger_counts(decisions) -> dict[str, int]` (calendar vs drift, from the reason prefix).

`backtest/ablations.py`
- `Variant(name, description, overrides: dict, cost_bps: float)`; `VARIANTS` in report order: `base`, `momentum_only`, `no_macro`, `no_risk_off`, `top_3`, `top_5`, `cost_0`, `cost_20`. `DEFAULT_COST_BPS = 5.0`.
- `build_cfg(base: CoreSleeveConfig, variant) -> CoreSleeveConfig`. `no_macro` weights are computed from `base`.

`backtest/metrics.py` (`TRADING_DAYS = 252`)
- `daily_returns(equity)`; `cagr(equity) = (E_end / E_start) ** (252 / n_returns) - 1`; `annual_volatility(r) = std(r, ddof=1) * sqrt(252)`.
- `sharpe(r, rf) = mean(r - rf) / std(r - rf, ddof=1) * sqrt(252)`, None if std is 0.
- `sortino(r, rf) = mean(x) * 252 / (sqrt(mean(min(x, 0) ** 2)) * sqrt(252))` with `x = r - rf`, None if the downside deviation is 0.
- `max_drawdown(equity) -> Drawdown(depth, peak, trough, recovery)`: `dd = E / cummax(E) - 1`; trough = first argmin; peak = last date before the trough where E equals the running max; recovery = first date after the trough with `E >= E[peak]`, else None.
- `calmar(cagr, depth)`: `cagr / abs(depth)`, None if depth is 0.
- `annual_turnover(turnover, n_returns) = turnover.sum() / (n_returns / 252)` (one-way, 1.0 = 100%).
- `calendar_year_returns(equity) -> dict[int, float]` (first and last years are partial and flagged in the report).
- `summarize(equity, rf, turnover, risk_off=None) -> dict` with every field above (dates as ISO strings).

`backtest/data.py`
- `MarketData(raw_close: pd.DataFrame, adj_close: pd.DataFrame, fred: dict[str, pd.Series], manifest: dict)`.
- `required_tickers(core_cfg, macro_cfg) -> list[str]` (D13 list, from config, sorted).
- `load_market_data(tickers, fred_ids, cache_dir, refresh=False, fetch_yahoo=None, fetch_fred=None) -> MarketData`. Default `fetch_yahoo(ticker)` imports yfinance lazily and calls `yf.Ticker(ticker).history(period="max", interval="1d", auto_adjust=False, actions=False)` (pass `auto_adjust=False` explicitly: the yfinance default changed to True), keeps `Close` and `Adj Close`, converts the tz-aware index to naive dates. Default `fetch_fred(id)` calls `MacroFetcher(MacroConfig())._fred_series(id)` (the live parser). Requests are sequential with a 1 s pause and up to 3 attempts per ticker.
- `validate_frame(ticker, df)` raises `DataError` (empty, missing columns, non-positive prices, duplicate dates).
- Cache path `<cache_dir>/yahoo/<safe>.csv` (`^VIX` becomes `_VIX`), `<cache_dir>/fred/T10Y2Y.csv`, `<cache_dir>/manifest.json`.

`backtest/report.py`
- `build_results(...) -> dict`, `write_json(results, path)` (sorted keys, floats rounded to 6 places, non-finite values as null, `allow_nan=False`), `write_markdown(results, path)` (no Python code blocks, so ruff's Markdown formatting has nothing to change), `plot_equity(curves, drawdowns, path)` (imports matplotlib inside, `Agg` backend; top panel log equity of strategy and three benchmarks, bottom panel drawdowns).

`backtest/run.py`
- `run_all(data: MarketData, core_cfg, reentry_cooldown_sec, end=None) -> dict`: calendar, start date, every variant, benchmarks at 0, 5 and 20 bps, metrics. Pure, no network.
- `main(argv=None) -> int`: flags `--refresh`, `--end YYYY-MM-DD`, `--cache-dir` (default `data/cache/backtest`), `--out-dir` (default `docs/backtest`). Loads config with `load_phase8_sleeves()[0]`, `load_macro_config()`, `load_portfolio()`. On any `DataError` or download failure it prints the ticker and reason, writes nothing and returns 1. There is no synthetic or fallback data path.

### Data flow

```text
yfinance / FRED -> data/cache/backtest (CSV) -> MarketData
MarketData.raw_close -> CoreReplay (closes sliced to t) -> CoreSleeve.should_evaluate / evaluate
MarketData.raw_close[^VIX, DX-Y.NYB, HYG, LQD] + fred[T10Y2Y] -> HistoricalMacroFetcher (as of t, FRED < t) -> MacroFetcher.snapshot()
CoreReplay.decide / benchmark decide -> simulate(adj_close) -> SimResult -> metrics.summarize -> report
```

### How look-ahead is prevented

1. Price signals: `CoreReplay` slices `raw_close[s].loc[:t]` before calling the sleeve. Rolling windows (`rolling(200)`, `iloc[-253]`) inside `score_universe` then only see rows up to `t`.
2. Drift: uses raw closes at `t` and weights that include the return up to `t` only.
3. FRED T10Y2Y: observations dated strictly before `t`.
4. VIX, DXY, HYG, LQD: raw closes dated on or before `t`, last 365 calendar days (D20 caveat).
5. Adjusted closes are only read by `simulate`, for returns between consecutive calendar days. A test replaces all adjusted closes with noise and checks that the decision list is unchanged.
6. Execution: a decision at `t` fills at `t+1`; weights from `t` to `t+1` are the old ones. A test checks that a signal on day `t` changes holdings from `t+1`, not `t`.
7. No-look-ahead test: perturb every input after day `k` (raw, adjusted, macro, FRED) and assert that decisions and fills on or before `k` are identical.
8. The start date only uses bar counts, not prices.

### Rejected alternatives

- vectorbt, backtrader or zipline: they would need the strategy rewritten in their API, which is the reimplementation the user ruled out.
- Setting credit to None: changes 7 of 10 macro scores, so the test would not be of the live sleeve (D1).
- Rebuilding `MacroSnapshot` by hand from `core.macro` helpers: duplicates `snapshot()` logic; the subclass runs the real method (D2).
- A `risk_off_enabled` flag in `core/scoring.py`: not needed, an infinite VIX threshold disables the switch with current code (D12).
- Parquet cache: needs pyarrow for about 15 small files (D16).
- Filling at the next open: no reliable adjusted opens for 2008 ETFs, and the user asked for the next close.
- Integer shares with a dollar notional: adds rounding noise and no insight at this level.
- Using `Adj Close` for signals: would differ from live (D4) and is a classic total-return look-ahead trap with back-adjusted series.
- `DTB3` from FRED as risk-free: the user asked for BIL.

## 4. Tasks

### Task 1. Scaffolding and metrics

`chore:` plus `feat:` (two commits are fine, or one `feat: add backtest metrics`).
- Add `requirements-backtest.txt` (`-r requirements.txt`, `yfinance>=0.2.40`, `matplotlib>=3.8`, a header comment saying it is not installed in Docker or CI).
- `.gitignore`: add `data/cache/`. `.dockerignore`: add `data/`.
- `pyproject.toml`: add `"backtest"` to `known-first-party`.
- `backtest/__init__.py` (docstring), `backtest/metrics.py` per Design.
- Tests first, `tests/test_backtest_metrics.py`: CAGR of an equity that doubles over 504 returns is `sqrt(2) - 1`; volatility of a known two-value series; Sharpe is None on a constant series equal to rf, and matches a hand computation on an alternating series; Sortino by hand; max drawdown on `[1, 1.2, 0.9, 1.0, 1.3]` gives depth -0.25, peak index 1, trough index 2, recovery index 4; recovery None when never regained; Calmar None at zero drawdown; annual turnover; calendar-year returns across a year boundary; `import backtest.metrics` works with `sys.modules["yfinance"]` and `sys.modules["matplotlib"]` set to None.
- Verify: the standard command.

independent

### Task 2. Portfolio loop, calendar, start date and benchmarks

`feat: add the backtest portfolio loop and benchmarks`
- `backtest/engine.py` per Design.
- `tests/backtest_synth.py`: small helpers that build business-day `raw_close` and `adj_close` frames from per-symbol return lists, with optional first dates per symbol.
- Tests first, `tests/test_backtest_engine.py`:
  - next-close execution: a decide function that returns `{"A": 1.0}` on day 0 leaves day 0 and day 1 returns at 0 (cash), charges 5 bps on day 1, and earns A's return from day 2;
  - cost accounting: switch A to B gives turnover 2.0 and a 10 bps cost at 5 bps; 0 bps costs nothing;
  - drift: 50/50 A/B with A up 10% and B flat gives weights 0.5238 and 0.4762 the next day;
  - buy and hold equals `(1 - c) * adj[t] / adj[t1]` and has exactly one fill;
  - monthly rebalance fills exactly on the first trading day of each month, including when the 1st is a weekend;
  - NaN adjusted close for a held symbol raises `ValueError`;
  - `trading_calendar` drops a day missing for one symbol; `first_start_date` picks the day the latest-starting symbol reaches `min_bars`, and raises with the short ticker named when none qualifies;
  - `min_bars_for(CoreSleeveConfig()) == 253`.
- Verify: the standard command.

depends on: 1

### Task 3. Historical macro replay

`feat: replay the live macro snapshot as of a date`
- `backtest/macro_replay.py` per Design.
- Tests first, `tests/test_backtest_macro_replay.py` (own small series built inline, not `tests/backtest_synth.py`):
  - FRED lag: a T10Y2Y spike dated `t` is not in the snapshot at `t` and is at `t+1`;
  - VIX on `t` is the close dated `t`; rows after `t` never change the snapshot;
  - the 365-day window: a value older than 365 days is ignored by `_yf_series`;
  - `credit_ratio_trend_60d` equals the hand-computed HYG/LQD 60-row change (proves the live `snapshot()` path runs);
  - `fed_funds is None` and `complete is False` with the replay config, and `snapshot()` prints nothing (`capsys`);
  - a missing ticker gives `None` fields without raising.
- Verify: the standard command.

depends on: 1

### Task 4. Data layer with cache

`feat: download and cache backtest data`
- `backtest/data.py` per Design. yfinance is imported only inside the default Yahoo fetcher.
- Tests first, `tests/test_backtest_data.py` (all offline, `tmp_path`, fake fetchers):
  - first load calls the fake fetchers and writes CSV plus `manifest.json`; second load reads the cache and calls nothing; `refresh=True` calls them again;
  - `^VIX` is cached as `_VIX.csv` and read back under `^VIX`;
  - `validate_frame` rejects empty frames, missing `Adj Close`, non-positive prices and duplicate dates;
  - a fetcher that raises surfaces as `DataError` naming the ticker after 3 attempts (patch the sleep);
  - `required_tickers` on the live config returns the 10 ETFs plus `^VIX`, `DX-Y.NYB`, `HYG`, `LQD`;
  - `import backtest.data` works with `sys.modules["yfinance"] = None`.
- Verify: the standard command.

depends on: 1

### Task 5. Core sleeve replay and ablations

`feat: replay the core sleeve with its own decision code`
- `backtest/strategy.py` and `backtest/ablations.py` per Design.
- Tests first, `tests/test_backtest_strategy.py` (using `tests/backtest_synth.py` and a short-lookback config like `small_cfg()` in `tests/test_sleeve_core.py`, `drift_threshold` high unless the test is about drift):
  - with no holdings, the first decision is on the first Friday and its fill is on the next trading day;
  - a Friday that is missing from the calendar (holiday) gives no decision that week unless drift fires;
  - drift: a large move in one holding triggers a decision on a non-Friday, at most once per day;
  - `decision_time(t)` maps to Eastern date `t` for a January and a July date;
  - the targets equal `select_holdings(score_universe(...))` called directly on the same sliced inputs (proves the bot code decides);
  - risk-off: SPY below its MA with VIX above the threshold makes the targets `risk_off_universe` and `risk_off_days` true from the fill day;
  - no look-ahead: perturbing raw, adjusted, macro and FRED data after day `k` leaves decisions and fills up to `k` unchanged;
  - adjusted closes replaced by noise leave the decision list unchanged;
  - `trigger_counts` splits calendar and drift.
- Tests first, `tests/test_backtest_ablations.py`: every `Variant` builds a config that `CoreSleeve` accepts (weights sum check); `no_macro` weights are 4/7 and 3/7 of the live config; `no_risk_off` gives `score_universe(...).risk_off is False` with SPY far below its MA and VIX at 80, while the base config gives True on the same inputs; `top_3` and `top_5` set `hold_top_n`; cost variants keep the base config.
- Verify: the standard command.

depends on: 2, 3

### Task 6. Report and CLI

`feat: add python -m backtest.run`
- `backtest/report.py` and `backtest/run.py` per Design. results.md contains: generation time, data download time, start and end dates, trading days and dropped days, data sources per ticker (first date, rows), assumptions and deviations from live (D3 to D11, D20, Friday-close timing), headline table (strategy and three benchmarks: CAGR, volatility, Sharpe, Sortino, max drawdown with peak, trough and recovery dates, Calmar, annual turnover, risk-off share), rebalance counts by trigger, calendar-year returns, ablation table, cost table with benchmarks at the same cost.
- Tests first, `tests/test_backtest_report.py` and `tests/test_backtest_run.py`:
  - `run_all` on a synthetic `MarketData` with a short-lookback config runs every variant and the benchmarks, offline;
  - results.json is valid with `allow_nan=False`, has the start date and every variant, and is byte-identical across two runs on the same input;
  - results.md contains the start date and every table heading;
  - `main()` with a fake loader that raises `DataError` returns 1 and writes no files in `tmp_path`;
  - `plot_equity` writes a PNG (`pytest.importorskip("matplotlib")`, so it is skipped in CI);
  - `import backtest.run` works with yfinance and matplotlib blocked.
- Verify: the standard command, then `.venv/bin/python -m backtest.run --help`.

depends on: 4, 5

### Task 7. Docs

`docs: document the backtest`
- `README.md`: a "Results" section after "Safety": what is tested (core sleeve only, `python -m backtest.run`, install `requirements-backtest.txt`), links to `docs/backtest/results.md` and the PNG, no numbers yet (task 8 adds the headline table from results.md), and the limitations: core sleeve only; universe chosen with hindsight in 2026; parameters fixed before the test, not optimised; survivorship (only ETFs that exist today) and ETF start dates set the start; costs simplified to a flat bps rate, no slippage beyond it; fractional weights, fills at the next close (live decides on Friday intraday bars and uses limit orders); signals on price closes, not total return, as live; FRED and Yahoo revise data; live drift uses a fixed budget, the backtest a marked-to-market one; the cooldown never binds at a daily step; cash earns 0. Also add the command to "Run it".
- `docs/TECH_STACK.md`: update the backtesting row (own replay, reason) and add yfinance and matplotlib as backtest-only dependencies.
- `docs/adr/0002-own-backtest-replay.md` (Status, Context, Decision, Consequences, Alternatives).
- `CHANGELOG.md` `[Unreleased]` `Added`: one short line for the `backtest/` package and `requirements-backtest.txt`.
- Verify: `.venv/bin/ruff format --check .` (Markdown) and the standard command.

depends on: 6

### Task 8. Generate and commit results (lead, network needed)

`docs: add backtest results`
- macOS, zsh, repo root:

```sh
uv pip install -r requirements-backtest.txt
.venv/bin/python -m backtest.run
git status   # only docs/backtest/* should be new; data/cache/ must not appear
```

- Commit `docs/backtest/results.md`, `docs/backtest/equity_curve.png`, `docs/backtest/results.json`. Fill the README headline table from results.md (results.md is the source of truth).
- Check: the start date is in mid 2008 and the per-ticker table explains it; no ticker has a suspicious first date (DX-Y.NYB especially).
- If any download fails or validation raises: stop, commit nothing under `docs/backtest/`, and write the error in the PR description. Do not fake, hand-edit or synthesise results.

depends on: 7

### Task 9. Data-scientist review gate

- Ask `kit:data-scientist` to review the `backtest/` code, tests and generated results for: look-ahead (signal slicing, FRED lag, DXY/VIX timing, adjusted closes, rolling windows), survivorship and hindsight, unfair benchmarks (same start, calendar, costs, entry cost, BIL in equal weight), cost and turnover handling, metric formulas, and the deviations list. It only edits `research/` and notebooks, so it reports in its reply.
- The lead puts the findings in the PR description. Each finding is fixed by an implementer (one commit per fix, with a regression test) or documented in results.md and the README limitations. If a fix changes numbers, rerun task 8's commands and recommit the outputs.
- Verify: the standard command; results regenerated if code changed.

depends on: 8

## 5. Tests

- Write first: the metrics known answers (task 1), then the loop's next-close and cost tests (task 2), because every later number depends on them.
- Edge cases: weekend and holiday gaps in the calendar; a symbol whose history starts late (start date); NaN adjusted close for a held symbol (raise); zero-variance return series (Sharpe and Sortino None); never-recovered drawdown; decision on the last day (never filled); all-cash strategy before the first Friday; cooldown with exactly 86400 s; infinity in config serialised as null.
- Fixtures: `tests/backtest_synth.py` builds tiny frames in code. No data files are committed under `tests/`. Short-lookback `CoreSleeveConfig` (momentum 10, MA 5, risk-off MA 5) keeps synthetic series to a few dozen rows.
- All tests run offline under the existing `conftest.py` network block, with `requirements-dev.txt` only. The plot test is skipped without matplotlib.

## 6. Risks

- **Look-ahead.** Mitigated by the slicing rules, the perturbation test, the adjusted-close noise test and the review gate. DXY and VIX settle up to an hour after the equity close (D20).
- **Timing differs from live.** Live decides on the first Friday OPEN cycle on intraday bars and fills the same day with limit orders; the backtest decides at Friday's close and fills Monday's close. Documented; a follow-up could test a "decide Thursday close, fill Friday close" variant.
- **yfinance rate limits and schema changes.** Sequential requests, a pause, 3 attempts, strict column validation, explicit `auto_adjust=False`. The cache means one successful download is enough. On failure the run stops with exit code 1.
- **Data gaps and holidays.** The calendar is the intersection of the 10 ETFs; macro series are joined as of. Dropped days are reported. Yahoo has occasional bad prints; validation catches non-positive values only, so the review should glance at the largest daily returns.
- **DX-Y.NYB history start.** If Yahoo's history is short or patchy, the start-date rule moves the start date and results.md shows why. The lead does not swap tickers silently; that would be an open question for the user.
- **BIL as risk-free.** BIL's adjusted returns include its expense ratio (slightly lower than T-bills, flatters Sharpe a little), were near 0 or negative in 2009 to 2015, and BIL's inception (May 2007) is what sets the start date. BIL's raw price momentum is near 0 because it pays out its yield; live has the same effect.
- **Run time.** About 4,600 days, 8 strategy runs, about 1,000 evaluations each; expected under 2 minutes on a laptop. Download of about 15 tickers takes under a minute.
- **Merge conflicts.** PR #30 touches `.gitignore`, `.dockerignore`, `README.md`, `CHANGELOG.md`, `tests/conftest.py` and adds `docs/adr/0001`. PR #31 touches `core/market_hours.py`, `requirements.txt`, `docs/TECH_STACK.md` and `CHANGELOG.md`. This plan does not edit `tests/conftest.py` or `core/`. Rebase after they merge; D5 is safe under PR #31's DST change. If PR #31 changes `to_eastern`'s signature, task 5's `decision_time` test catches it.
- **`MacroFetcher` private methods.** The subclass depends on `_fred_series` and `_yf_series` names and signatures. Task 3's tests fail loudly if a later change (for example a fix for `datetime.utcnow()`) renames them.
- **No migrations, no deployment.** Nothing runs in Docker or systemd. Rollback is reverting the PR; the outputs under `docs/backtest/` go with it.
- **Security.** No keys are needed (FRED public CSV, Yahoo). Raw data stays in the git-ignored cache.

## 7. Open questions (defaults applied, see Decisions)

1. Add HYG and LQD although the user listed only the universe, `^VIX`, `DX-Y.NYB` and T10Y2Y? Default: yes (D1).
2. Charge an entry cost to SPY buy and hold? Default: yes, one 5 bps entry, same as every portfolio (D9, D11).
3. Include BIL in the equal-weight benchmark? Default: yes, it is one of "the same ETFs" (D11).
4. Should the strategy also start on the start date instead of waiting for the first Friday? Default: no, wait like live (D10).
5. Recompute the benchmarks at 0 and 20 bps for the cost ablations? Default: yes (D12).
6. "Share of days in risk-off": positioned days or signal days? Default: positioned days (D18).
7. Marked-to-market sleeve equity instead of live's fixed budget? Default: marked to market, deviation documented, follow-up issue (D7).
8. Add an ADR and leave `CLAUDE.md` alone? Default: ADR 0002 yes, `CLAUDE.md` unchanged (D21, D22).
9. Suggested follow-up issues (not in this PR): live drift against a fixed budget (D7); a "Thursday close" timing variant; adding `python -m backtest.run` to `CLAUDE.md`.

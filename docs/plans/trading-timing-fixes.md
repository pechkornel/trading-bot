# Plan: trading timing and re-entry fixes (#5, #6, #16, #17, #18)

Branch: `fix/trading-timing-fixes`. The PR closes #5, #6, #16, #17 and #18 (`Closes #5`, `Closes #6`, `Closes #16`, `Closes #17`, `Closes #18` in the PR body). Every fix is test-first: write the test, run it and see it fail on current code, then fix, then see it pass.

## Decisions (approved 2026-11-24)

Approved with the defaults below, except:
- Q7: rename the calendar reason string from `calendar: Fri close` to `calendar: Fri` (the trigger no longer waits for the close). Task 2 pins the new string; task 10 notes the rename in the CHANGELOG (`Changed`).
- New task 11: change the `PortfolioConfig.reentry_cooldown_sec` dataclass default in `core/config.py` from 600 to 86400 to match `config/symbols.yaml`, because this PR starts enforcing it. Test first.
- At ship: open the `control.html` wording and the `core/sleeves/__init__.py` docstring findings as follow-up issues. The `PortfolioConfig` default finding is fixed here, not opened.
- If a hook or guard blocks a command, stop and report to the user.

## Open questions (answered, kept for the record)

1. Should the core calendar trigger wait for the Friday close (16:00 ET)? Default: no. Fire on the first eligible cycle of the rebalance day in Eastern time.
2. Should a partial sell (a trim that keeps shares above 0) start the cooldown? Default: no. Only a sell to 0 shares is an exit.
3. Is the cooldown per sleeve and symbol, or per symbol across sleeves? Default: per sleeve and symbol.
4. Should a symbol exited in this cycle be blocked from re-entry in the same cycle even when the cooldown is 0? Default: yes, always.
5. When core cannot buy a top-set symbol because of the cooldown, is its weight left in cash or spread over the other targets? Default: left in cash.
6. Remove `is_core_rebalance_window`, or keep it? Default: remove it (it has no effect today).
7. Keep the calendar reason string `calendar: Fri close`? Default: keep it unchanged.

## 1. Goal

Make the portfolio runner act on US trading days and stop churning symbols:

- The core sleeve rebalances at most once per US Eastern calendar date, whatever the trigger (calendar or drift). The rebalance weekday is read in Eastern time, not UTC (#5, #18).
- `portfolio.reentry_cooldown_sec` is enforced for both sleeves that the runner runs (core and sentiment). A symbol that a sleeve sold down to 0 shares cannot be bought back by that sleeve before the cooldown ends (#6).
- A sentiment satellite position that exits in a cycle is never re-entered in the same cycle (#17).
- Satellite skip reasons have no leading space (#16).

"US trading day" means the Eastern calendar date from the existing `core.market_hours.to_eastern` helper. No holiday calendar, no `zoneinfo`.

Out of scope:

- #7 (`datetime.utcnow()` in `core/macro.py`).
- #15 (holiday calendar and exact DST in `core/market_hours.py`). `_us_eastern_offset` and `to_eastern` are reused unchanged.
- #8 (tactical sleeve wiring). The tactical sleeve is not run by `live/portfolio_runner.py`, so it gets no cooldown here.
- Any schema change. No new table or column (see Design B).
- `core/ibkr_auth.py`, `assert_paper_mode` and the `DRY_RUN` default are not touched.
- Stale dashboard text found while planning (section 6, follow-ups).

## 2. Current state

- `core/sleeves/core.py`, `CoreSleeve.should_evaluate(*, last_rebalance_ts, current_holdings, prices, sleeve_equity, now=None)`. Defaults `now` to `datetime.now(timezone.utc)`. Order of checks: drift first (returns early, so #18), then `now.weekday()` against `cfg.rebalance_day_of_week` (UTC weekday, #5), then `last_rebalance_ts.date() == now.date()` (UTC date). Reason strings: `drift: ...`, `not rebalance day (today=Thu, want=Fri)`, `already rebalanced today`, `calendar: Fri close`.
- `core/sleeves/core.py`, `CoreSleeve.evaluate(*, closes, macro, current_holdings, sleeve_equity, reason)`. One `CoreDecision` per symbol in holdings plus targets. No `now`, no cooldown.
- `core/sleeves/sentiment_satellite.py`, `SentimentSatelliteSleeve.evaluate(*, inputs, positions, sleeve_equity, now=None, allow_entries=True, allow_exits=True)`. Exits first; `remaining_positions` drops exited symbols, so an exited symbol is a fresh candidate in the entry pass (#17). Line 143: `skipped.append((sym, reason_or_skip[5:]))` leaves `" no fresh sentiment"` (#16). `_entry_check` returns `"skip: <why>"` or `"enter: ..."`.
- `core/market_hours.py`: `_us_eastern_offset(utc_now)` (DST by date only), `to_eastern(utc) -> naive Eastern datetime` (naive input is treated as UTC), `market_state(now_utc=None)`, `should_run_entries`, `should_run_exits`, `is_core_rebalance_window(state, rebalance_day_of_week=4)`. The module docstring claims core evaluates only after close on Friday, which is not what the runner does.
- `live/portfolio_runner.py`:
  - `PortfolioRunner.__init__` builds `CoreSleeve(self.core_cfg)` and `SentimentSatelliteSleeve(self.sent_cfg)`, loads `self.portfolio_cfg = load_portfolio()`.
  - `_cycle()` calls `market_state()` with the wall clock, then `_cycle_satellite(cycle_id, total_equity, ms)` and `_cycle_core(cycle_id, total_equity, ms)`.
  - `_cycle_core` skips in `WEEKEND` and `PRE_OPEN` unless `is_core_rebalance_window(...)` is true. That bypass never matters: the window requires `AFTER_CLOSE`, which is never `WEEKEND` or `PRE_OPEN`. It then calls `should_evaluate` without `now` and `evaluate` without `now`.
  - `_execute_core_leg`: in dry run and in live (on success) it calls `storage.log_order(..., status="logged" | "placed")` and `storage.upsert_holding(shares=target_shares)`. A core symbol sold to 0 keeps a `holdings` row with 0 shares. On failure it logs `status="failed"` and leaves holdings alone.
  - `_exec_satellite_leg`: same pattern. A SELL calls `storage.delete_holding`, a BUY calls `upsert_holding`. Satellite SELLs always sell the whole position (`SatelliteExit.shares = pos.shares`).
  - Dry run confirmed: both sleeves write `orders` rows (`dry_run=1`, `status='logged'`) and update `holdings` in dry run exactly as in live. So a cooldown derived from `orders` plus `holdings` behaves the same in dry run.
- `live/storage.py`, `Storage(db_path=None)`: tables `orders` (`ts`, `sleeve`, `symbol`, `action`, `quantity`, `dry_run`, `status`, ...), `holdings` (`UNIQUE(sleeve, symbol)`, `opened_at`, `last_update`), `rebalances`. Methods used here: `log_order`, `get_holdings`, `upsert_holding`, `delete_holding`, `log_rebalance`, `last_rebalance`. Timestamps are `datetime.now(timezone.utc).isoformat()`. Schema is `CREATE TABLE IF NOT EXISTS` only, no migrations.
- `core/config.py`, `PortfolioConfig.reentry_cooldown_sec: int = 600` (dataclass default); `config/symbols.yaml` sets 86400. `dashboard/templates/control.html` line 20 shows `cooldown {{ portfolio.reentry_cooldown_sec }}s`, which stays correct.
- Tests: `tests/test_sleeve_core.py` (`FRIDAY = 2026-11-13 12:00 UTC`, which is Fri 07:00 EST; `WEDNESDAY`; helpers `small_cfg`, `check`, `evaluate`, `ranked_closes`, `by_symbol`), `tests/test_sleeve_sentiment_satellite.py` (`NOW`, `make_sleeve`, `inp`, `pos`, `run`), `tests/conftest.py` (network guard, `.env` isolation, `make_macro`, `series`, `linear_closes`). No tests for `live/` yet. `import live.portfolio_runner` works offline.
- Docs: `docs/TRADING_LOGIC.md` sections 4 (core triggers), 5 (satellite entries), 7 (risk controls), 8 (Known gaps lists all five bugs), 9 (where to change behaviour). `config/symbols.yaml` comments on `cycle_interval_sec`, `reentry_cooldown_sec` and `rebalance_day_of_week`. `CHANGELOG.md` has an `[Unreleased]` section with `Fixed`.

Checked Eastern conversions with the existing helper:

| UTC | `to_eastern` |
|---|---|
| 2026-11-13 01:00 | Thu 2026-11-12 20:00 |
| 2026-11-14 02:00 | Fri 2026-11-13 21:00 |
| 2026-06-12 00:30 | Thu 2026-06-11 20:30 (EDT) |
| 2026-11-13 12:00 | Fri 2026-11-13 07:00 |

## 3. Design

### A. Core trigger in Eastern time (#5, #18)

`should_evaluate` keeps its signature. It converts once at the top:

```python
now = now or datetime.now(timezone.utc)
now_et = to_eastern(now)
last_et = to_eastern(last_rebalance_ts) if last_rebalance_ts is not None else None
```

`to_eastern` is imported from `core.market_hours` (stdlib only imports, no cycle). New order of checks:

1. Once per Eastern date: if `last_et is not None and last_et.date() == now_et.date()`, return `(False, "already rebalanced today")`. This now runs before drift (#18).
2. Drift: unchanged.
3. Calendar: `now_et.weekday()` against `rebalance_day_of_week`; the "today=" label uses `now_et.strftime('%a')`.
4. Return `(True, f"calendar: {_day_name(rebalance_day_of_week)}")`, which is `calendar: Fri` with the default config (renamed from `calendar: Fri close`, see Decisions).

Tests stay deterministic because they pass explicit UTC `now=` and `last_rebalance_ts=` values. Naive datetimes keep being treated as UTC (that is what `to_eastern` does).

Calendar trigger timing (Open question 1, default): the trigger fires on the first cycle of the Eastern rebalance day that the runner lets through. The runner skips `PRE_OPEN`, so with hourly cycles that is the first `OPEN` cycle on Friday (around 09:30 to 10:30 ET). It can also fire in Friday `AFTER_CLOSE` if no earlier Friday cycle ran. It no longer fires on Thursday evening ET.

`is_core_rebalance_window` (Open question 6, default): remove it and its no-op use in `_cycle_core`. The gate becomes `if ms.regime in (MarketRegime.WEEKEND, MarketRegime.PRE_OPEN): skip`, which is the same behaviour as today. Fix the comments and the `core/market_hours.py` docstring that describe a Friday after-close window.

Rejected:
- Rewrite `market_hours` with `zoneinfo`: that is #15, out of scope.
- Pass `MarketState` into `should_evaluate`: couples the pure sleeve to the regime object; a UTC `now` plus `to_eastern` is enough.
- Fix `is_core_rebalance_window` and make the calendar trigger depend on it: that is the wait-for-close behaviour change (Open question 1).

### B. Re-entry cooldown (#6)

Definitions:
- Exit: a sleeve's SELL that takes a symbol to 0 shares in that sleeve. Satellite exits always do. Core exits happen when a symbol leaves the top set or the risk-off switch drops it (`target_shares == 0`).
- Buy back: a BUY that takes a symbol from 0 shares (no row, or a 0-share row) to more than 0 in the same sleeve. Top-ups of a held position are never blocked. Sells are never blocked.
- Blocked when `now - last_exit_ts < timedelta(seconds=reentry_cooldown_sec)`. At exactly the cooldown, the buy is allowed. A cooldown of 0 or less disables it.
- Scope: per sleeve and symbol (Open question 3). Core and sentiment can both hold SPY; a satellite stop on SPY does not block a core rebalance into SPY.

Where the exit time comes from (no schema change):

New method `Storage.last_exit_times(sleeve: str) -> dict[str, datetime]`:

```sql
SELECT symbol, MAX(ts) AS ts FROM orders
WHERE sleeve = ? AND action = 'SELL' AND status IN ('logged', 'placed')
GROUP BY symbol
```

It returns tz-aware UTC datetimes (`datetime.fromisoformat`, naive values get `tzinfo=utc`). The sleeves only apply the cooldown to symbols they currently hold at 0 shares. For such a symbol the latest successful SELL is the one that took it to 0: any later successful order would be a BUY that left it above 0, and failed orders do not change holdings. So `MAX(ts)` of successful SELLs is exactly the last exit time, for both sleeves, without knowing whether a core SELL was a trim. Dry-run (`logged`) and live (`placed`) rows both count, matching how `holdings` already mixes both.

Existing `logs/bot.sqlite` files work unchanged: the `orders` table and its columns already exist. Past exits in the order history start counting on the first cycle after deploy.

Shared helper (keeps both sleeves consistent): new module `core/sleeves/cooldown.py`:

```python
def cooldown_remaining(exit_ts: datetime | None, now: datetime, cooldown_sec: int) -> timedelta | None:
    """Time left before a symbol exited at exit_ts may be bought again, or None if free."""
```

Naive `exit_ts` or `now` is treated as UTC. Returns `None` when `exit_ts is None`, `cooldown_sec <= 0`, or the time left is 0 or less. A second helper `format_cooldown(left: timedelta) -> str` returns `"reentry cooldown: 23.0h left"` (hours, one decimal), used for skip reasons in both sleeves.

Sleeve interfaces (pure, no SQLite):
- `CoreSleeve.__init__(self, cfg, *, reentry_cooldown_sec: int = 0)`.
- `CoreSleeve.evaluate(..., reason, last_exit_ts: Mapping[str, datetime] | None = None, now: datetime | None = None)`. For a symbol with `current == 0` and `target_shares > 0` and time left: emit the decision with `target_weight=0.0`, `target_shares=0`, `target_value=0.0`, `delta_shares=0`, `reason=format_cooldown(left)`. The freed weight stays in cash (Open question 5). No order is sent because `delta_shares == 0`.
- `SentimentSatelliteSleeve.__init__(self, cfg, *, reentry_cooldown_sec: int = 0)`.
- `SentimentSatelliteSleeve.evaluate(..., last_exit_ts: Mapping[str, datetime] | None = None)`. In the entry loop, after the "already holding" check and before `_entry_check`: skip with `"exited this cycle"` if the symbol is in this cycle's exits (#17, regardless of cooldown), else skip with `format_cooldown(left)` if the cooldown blocks it. Skipped symbols do not use a slot.

Defaults of 0 and `None` keep every existing test and caller unchanged.

Runner wiring (`live/portfolio_runner.py`):
- Pass `reentry_cooldown_sec=self.portfolio_cfg.reentry_cooldown_sec` to both sleeve constructors.
- `_cycle()` takes one `now = datetime.now(timezone.utc)` per cycle, calls `market_state(now)`, and passes `now` to `_cycle_satellite(cycle_id, total_equity, ms, now)` and `_cycle_core(cycle_id, total_equity, ms, now)`. Those pass `now=now` to `should_evaluate` and both `evaluate` calls, and `last_exit_ts=self.storage.last_exit_times(<sleeve>)`. One clock per cycle makes the runner testable and keeps the regime and the sleeve decisions on the same instant.

Rejected:
- New `exits` table or `exited_at` column on `holdings`: a schema change for data that `orders` already has. Old DBs would have no history, and it needs an ADR.
- Use `holdings.last_update` of 0-share rows for core: works for core only (satellite deletes the row), so two sources.
- Mark exits in the `orders.note` text: fragile parsing and old rows lack the marker.
- Sleeves query SQLite: breaks the pure, I/O-free sleeves.
- Per symbol across sleeves: couples independent books (Open question 3).
- Redistribute blocked core weight: oversizes the others, which the next rebalance trims again (Open question 5).

### C. Same-cycle re-entry (#17)

Covered by the "exited this cycle" check in Design B. With the cooldown at 86400 the cooldown alone would also block it (exit time is `now`), but the explicit rule keeps #17 fixed when the cooldown is 0 or turned off.

### D. Skip reason (#16)

Replace `reason_or_skip[5:]` with `reason_or_skip.removeprefix("skip:").strip()`. The runner event then reads `AAPL skip: no fresh sentiment` (one space, not two).

## 4. Tasks

Verification for every task (macOS zsh, repo root, or Linux with the same paths; Windows: `.venv\Scripts\...`):

```sh
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Do not use `uv run` or `uv sync`. Each task is one `fix:` commit (test and fix together, test written and seen failing first) unless noted. Reference the issue in the subject, for example `fix: strip the space from satellite skip reasons (#16)`.

### Task 1. Skip reasons without a leading space (closes #16)

Files: `core/sleeves/sentiment_satellite.py`, `tests/test_sleeve_sentiment_satellite.py`.

Failing tests first:
- Change `test_missing_sentiment_is_skipped` to assert `result.skipped == [("AAPL", "no fresh sentiment")]` and drop the "Current behaviour" comment. Fails today (value is `" no fresh sentiment"`).
- Add `test_skip_reasons_have_no_surrounding_whitespace`, parametrized over `inp(score=None)`, `inp(score=0.39)`, `inp(count=4)`, `inp(score=-0.8)`: assert every `why` in `result.skipped` equals `why.strip()`. Fails today.

Fix: Design D. Run `.venv/bin/python -m pytest -q tests/test_sleeve_sentiment_satellite.py`, then the full suite.

independent

### Task 2. Core trigger uses the Eastern date, once per day for every trigger (closes #5 and #18)

Files: `core/sleeves/core.py`, `tests/test_sleeve_core.py`.

Failing tests first (all pass explicit tz-aware UTC datetimes):
- `test_thursday_evening_eastern_is_not_a_rebalance_day`: `now = 2026-11-13 01:00 UTC` (Thu 20:00 EST). Expect `(False, "not rebalance day (today=Thu, want=Fri)")`. Today returns `(True, "calendar: Fri close")`.
- `test_thursday_evening_eastern_in_summer_is_not_a_rebalance_day`: `now = 2026-06-12 00:30 UTC` (Thu 20:30 EDT). Same expectation. Fails today.
- `test_friday_evening_eastern_is_a_rebalance_day`: `now = 2026-11-14 02:00 UTC` (Fri 21:00 EST). Expect `(True, "calendar: Fri close")`. Today returns not rebalance day (UTC Saturday).
- `test_same_eastern_date_blocks_a_repeat_across_utc_midnight`: `last = 2026-11-13 15:00 UTC` (Fri 10:00 EST), `now = 2026-11-14 02:00 UTC` (Fri 21:00 EST). Expect `(False, "already rebalanced today")`. Today the reason is "not rebalance day".
- `test_rebalance_on_previous_eastern_date_does_not_block`: `last = 2026-11-13 01:00 UTC` (Thu 20:00 EST), `now = 2026-11-13 15:00 UTC` (Fri 10:00 EST). Expect `(True, "calendar: Fri close")`. Today it is blocked (same UTC date).
- `test_drift_does_not_rebalance_twice_on_the_same_eastern_date` (#18): `WEDNESDAY` with drifted holdings `{"SPY": 60, "TLT": 40}`, prices 100, `last = WEDNESDAY - 2h`. Expect `(False, "already rebalanced today")`. Today returns a drift trigger.
- `test_drift_triggers_after_a_rebalance_on_the_previous_eastern_date`: same holdings, `last = WEDNESDAY - 1 day`. Expect a `drift: ` reason. Passes today; it guards the new ordering.

Reason string (Decisions, Q7): every expectation above that says `(True, "calendar: Fri close")` uses `(True, "calendar: Fri")` instead, and the existing tests that pin `"calendar: Fri close"` (for example `test_friday_without_prior_rebalance_triggers` and `test_rebalance_on_the_previous_utc_date_does_not_block`) are updated in the same commit. Check `rg -n "Fri close" core live dashboard tests docs` after the fix; only CHANGELOG history and this plan may still mention it.

Then fix (Design A). After the fix, `test_earlier_rebalance_on_the_same_utc_date_blocks_a_repeat` fails because its `earlier` (01:00 UTC) is Thursday evening ET. Replace it in the same commit with `test_earlier_rebalance_on_the_same_eastern_date_blocks_a_repeat` (`last = 2026-11-13 14:30 UTC`, `now = 2026-11-13 20:00 UTC`, both Friday ET). Rename `test_rebalance_on_the_previous_utc_date_does_not_block` to say "Eastern". Update the module docstring and the `should_evaluate` docstring: Eastern date, once per day for every trigger, first eligible cycle of the rebalance day.

independent

### Task 3. Remove the no-op core rebalance window (part of #5)

Files: `core/market_hours.py`, `live/portfolio_runner.py`. Commit type `refactor:` (no behaviour change).

There is no failing test: the removed branch can never run (`AFTER_CLOSE` is never `WEEKEND` or `PRE_OPEN`). Proof: the full suite stays green, `rg -n is_core_rebalance_window` finds nothing outside `docs/plans/`, and `.venv/bin/python -c "import live.portfolio_runner"` succeeds.

Steps: delete `is_core_rebalance_window` from `core/market_hours.py` and its import and use in `_cycle_core`. The gate becomes `if ms.regime in (MarketRegime.WEEKEND, MarketRegime.PRE_OPEN):`. Fix the `core/market_hours.py` module docstring (core is evaluated in `OPEN` and `AFTER_CLOSE`, not only Friday after close) and the runner comments at the core block in `_cycle` and at the top of `_cycle_core`, plus the module docstring line "core only acts on Fri close or on drift". Do not touch `_us_eastern_offset`, `to_eastern` or `market_state`.

If Open question 1 or 6 is answered differently, this task changes (see section 7).

independent

### Task 4. `Storage.last_exit_times` (part of #6)

Files: `live/storage.py`, new `tests/test_storage.py`.

Failing tests first (use `Storage(tmp_path / "bot.sqlite")`; it is local SQLite, the network guard is not involved):
- `test_last_exit_times_returns_latest_successful_sell_per_symbol`: log two SELLs for `("sentiment", "AAPL")` and one for `MSFT`; result keys are `{"AAPL", "MSFT"}` and AAPL's value equals the later row's `ts`.
- `test_last_exit_times_ignores_buys_failed_orders_and_other_sleeves`: a BUY, a `status="failed"` SELL, and a `sleeve="core"` SELL do not appear in `last_exit_times("sentiment")`.
- `test_last_exit_times_counts_dry_run_and_placed`: `dry_run=True, status="logged"` and `dry_run=False, status="placed"` both count.
- `test_last_exit_times_values_are_tz_aware_utc`.
- `test_last_exit_times_empty_db`: returns `{}`.

All fail today with `AttributeError`. To control timestamps, the tests can update `orders.ts` directly with `sqlite3` on the temp file, or compare against the row's stored `ts`.

Fix: add the method (Design B). No change to `SCHEMA`.

independent

### Task 5. Cooldown helper (part of #6)

Files: new `core/sleeves/cooldown.py`, new `tests/test_cooldown.py`.

Failing tests first (fail today with `ModuleNotFoundError`):
- `None` exit time, cooldown 0, and negative cooldown all return `None`.
- Exit 1 hour ago with 86400: returns 23 hours.
- Exit exactly 86400 seconds ago: returns `None` (boundary allows the buy).
- Exit 2 days ago: `None`.
- Naive `exit_ts` is treated as UTC (same result as the aware value).
- `format_cooldown(timedelta(hours=23)) == "reentry cooldown: 23.0h left"`.

Fix: implement Design B helpers. Do not export from `core/sleeves/__init__.py` (internal helper).

independent

### Task 6. No same-cycle re-entry in the satellite (closes #17)

Files: `core/sleeves/sentiment_satellite.py`, `tests/test_sleeve_sentiment_satellite.py`.

Failing tests first:
- `test_timer_exit_is_not_reentered_in_the_same_cycle`: positions `{"AAPL": pos(age_days=10)}`, inputs `[inp("AAPL", score=0.9)]`. Expect exits `["AAPL"]`, entries `[]`, and `("AAPL", "exited this cycle")` in `skipped`. Today AAPL is also entered.
- `test_stop_exit_is_not_reentered_in_the_same_cycle`: `pos()` with entry 100, `inp("AAPL", score=0.9, price=95.0)`. Same expectation. Fails today.
- `test_freed_slot_goes_to_another_candidate`: four held positions with `SPY` aged 10 days, inputs `inp("SPY", score=0.95)` and `inp("NVDA", score=0.9)`. Expect exits `["SPY"]`, entries `["NVDA"]`. Today entries is `["SPY"]`.

Fix: build `exited = {e.symbol for e in exits}` and skip those symbols in the entry loop with `"exited this cycle"`, after the "already holding" check. Update the module docstring entry rules.

depends on: 1

### Task 7. Satellite cooldown (part of #6)

Files: `core/sleeves/sentiment_satellite.py`, `tests/test_sleeve_sentiment_satellite.py`.

Failing tests first (extend `make_sleeve` with a `reentry_cooldown_sec=0` keyword, and let `run` accept a `sleeve=` keyword or build the sleeve inline):
- `test_recent_exit_blocks_reentry`: cooldown 86400, `last_exit_ts={"AAPL": NOW - 1h}`, `inp("AAPL", score=0.9)`. Expect no entries and `("AAPL", "reentry cooldown: 23.0h left")` in `skipped`.
- `test_reentry_allowed_at_exactly_the_cooldown`: exit at `NOW - 86400s`. One entry.
- `test_zero_cooldown_allows_reentry_after_an_earlier_exit`: cooldown 0, exit 1 hour ago. One entry.
- `test_naive_exit_time_is_treated_as_utc`.
- `test_cooldown_block_does_not_use_a_slot`: `max_concurrent_positions=1`, AAPL (0.9, blocked) and MSFT (0.8). Entries `["MSFT"]`.

All fail today (`TypeError` on the new keywords). Fix: Design B, using `cooldown_remaining` and `format_cooldown`.

depends on: 5, 6

### Task 8. Core cooldown (part of #6)

Files: `core/sleeves/core.py`, `tests/test_sleeve_core.py`.

Failing tests first (build `CoreSleeve(small_cfg(), reentry_cooldown_sec=86400)` and pass `now=FRIDAY`; `ranked_closes()` puts SPY and TLT in the top 2, 50 shares each at 100 on 10,000):
- `test_recent_exit_blocks_core_buy_back`: holdings `{"SPY": 0, "TLT": 50}`, `last_exit_ts={"SPY": FRIDAY - 1h}`. SPY decision: `target_shares == 0`, `delta_shares == 0`, `target_weight == 0.0`, reason starts with `"reentry cooldown"`. TLT stays at 50 shares (weight not spread).
- `test_cooldown_applies_without_a_holdings_row`: holdings `{"TLT": 50}` (no SPY key), same exit. SPY blocked.
- `test_held_symbol_top_up_is_not_blocked`: holdings `{"SPY": 30, "TLT": 50}`, SPY exit (a trim) 1 hour ago. SPY `delta_shares == 20`.
- `test_core_buy_back_allowed_after_cooldown`: exit 2 days ago. SPY `delta_shares == 50`.
- `test_sells_are_never_blocked`: holdings `{"GLD": 30}`, `last_exit_ts={"GLD": FRIDAY - 1h}`. GLD `delta_shares == -30`.

All fail today (`TypeError`). Fix: Design B. Update the module docstring decision flow (step 3 mentions the cooldown).

depends on: 2, 5

### Task 9. Wire the cooldown and one clock into the runner (closes #6)

Files: `live/portfolio_runner.py`, new `tests/test_portfolio_runner.py`.

Test setup: build the runner with `PortfolioRunner.__new__(PortfolioRunner)` and set only the attributes the cycle methods read: `storage = Storage(tmp_path / "bot.sqlite")`, `dry_run = True`, `client` (a small fake whose `get_historical_data(symbol, bar, period)` returns `pd.DataFrame({"close": [...]})`), `symbols` (`SymbolConfig(symbol=s, bar_size="1 day", history_duration="2 Y")`), `portfolio_cfg`, `sent_cfg` and `satellite` (cooldown 86400), `sentiment_cfg = SentimentConfig()`, `core_cfg` and `core` (`small_cfg`-like config, cooldown 86400), `macro_fetcher` (fake with `snapshot()` returning `make_macro()`). No network is used; the conftest guard would fail the test if it were. `ms = market_state(<a UTC time that is OPEN>)`.

Failing tests first:
- `test_satellite_cycle_respects_cooldown`: seed `log_sentiment(symbol="AAPL", source="test", score=0.9, article_count=10)` and a `log_order(sleeve="sentiment", symbol="AAPL", action="SELL", status="logged", dry_run=True, ...)`. Call `_cycle_satellite("c1", 100_000.0, ms, datetime.now(timezone.utc))`. Assert no `BUY` row for AAPL in `storage.recent_orders()` and no AAPL holding. Today it fails (wrong arity, and without the wiring a BUY is logged). Use the wall clock for `now` here because `latest_sentiment` checks freshness against the wall clock.
- `test_core_cycle_respects_cooldown`: the fake client returns `ranked_closes()`-shaped closes (SPY and TLT rank top, last close 100). Seed `upsert_holding(sleeve="core", symbol="SPY", shares=0.0, avg_cost=100.0)` and a core SELL order for SPY. Use a fixed Friday `now` (for example `2026-11-13 15:00 UTC`, Fri 10:00 EST, `OPEN`) and `ms = market_state(now)`, because the calendar trigger needs a Friday. Set the SELL row `ts` to `(now - 1h).isoformat()` with `sqlite3` on the temp file. Call `_cycle_core("c1", total_equity, ms, now)` with `total_equity` chosen so the core sleeve equity is 10,000. Assert no core BUY for SPY in `orders` and a `targets` row for SPY whose `reason` contains `reentry cooldown`. Fails today (wrong arity, and without the wiring SPY is bought).
- `test_runner_passes_cooldown_to_sleeves`: with `load_portfolio` and `load_phase8_sleeves` monkeypatched to known configs and `load_app_config` returning a paper config (`PAPER=true`, `IB_PORT=5000` via `monkeypatch.setenv`), `PortfolioRunner(dry_run=True, once=True, interval_override=None)` has `core.reentry_cooldown_sec == satellite.reentry_cooldown_sec == portfolio_cfg.reentry_cooldown_sec`. If constructing the real runner needs more patching than this, drop this test and rely on the two cycle tests.

Fix (Design B, runner wiring): constructor keywords, one `now` per `_cycle`, `market_state(now)`, new `now` parameter on `_cycle_core` and `_cycle_satellite`, `now=` and `last_exit_ts=self.storage.last_exit_times(...)` on the sleeve calls. `assert_paper_mode` and the dry-run default stay as they are.

Manual check after merge into the branch (macOS or the Ubuntu home server, with a logged-in paper gateway): `.venv/bin/python -m live.portfolio_runner --once` (dry run, never `--live`). Look for `core ... skip (already rebalanced today)` or a calendar or drift line, and satellite `skip: reentry cooldown` events after a recent exit.

depends on: 3, 4, 7, 8

### Task 10. Docs and changelog

Files: `docs/TRADING_LOGIC.md`, `config/symbols.yaml` (comments only), `CHANGELOG.md`. Commit type `docs:`.

- `docs/TRADING_LOGIC.md`:
  - Section 4: checks are once-per-day first (Eastern date, any trigger), then drift, then calendar. Calendar trigger: Eastern weekday equals `rebalance_day_of_week`, fires on the first eligible cycle that day (`OPEN` or `AFTER_CLOSE`). Drop the "does not fire at Friday close, see section 8" paragraph. Add a short "Re-entry cooldown" paragraph: a symbol sold to 0 is not bought back by core for `reentry_cooldown_sec`; its weight stays in cash; top-ups and sells are not blocked.
  - Section 5, Entries: add "it exited in this cycle" and "it was sold less than `portfolio.reentry_cooldown_sec` (86400) ago by this sleeve" to the skip list.
  - Section 7: add a row "Re-entry cooldown | Both active sleeves | No buy back of a symbol within 24 hours of selling it to 0, per sleeve. Derived from the `orders` table."
  - Section 8: remove the four fixed items (UTC weekday, cooldown not enforced, same-cycle re-entry, drift bypass).
  - Section 9: add "Change the re-entry cooldown | `portfolio.reentry_cooldown_sec`".
- `config/symbols.yaml`: the `cycle_interval_sec` comment ("core only acts on Friday close") becomes "core acts at most once per US Eastern day: on Friday or on drift". `reentry_cooldown_sec`: "min seconds between selling a symbol to 0 and buying it back, per sleeve". `rebalance_day_of_week`: "0=Mon, 4=Fri (US Eastern weekday)". No value changes.
- `CHANGELOG.md` `[Unreleased]`:
  - Fixed: core weekday and once-per-day check use the US Eastern date (#5); `portfolio.reentry_cooldown_sec` is enforced for the core and sentiment sleeves (#6); satellite skip reasons have no leading space (#16); the satellite no longer re-enters a symbol in the cycle it exits it (#17); the drift trigger respects the once-per-day rule (#18).
  - Changed: the core calendar trigger reason is now `calendar: Fri` instead of `calendar: Fri close`, because it fires on the first Friday cycle in Eastern time, not at the close (#5). Anything that matches the old string in `targets.reason` or the events log must be updated. The `PortfolioConfig.reentry_cooldown_sec` default is now 86400 (was 600), matching `config/symbols.yaml` (#6).
  - Removed: `core.market_hours.is_core_rebalance_window` (it had no effect) (#5).
  - Update the test count in the existing "Offline test suite (123 tests)" line to the real number from `pytest -q`.
- Dashboard: no change. `control.html` still shows the cooldown value correctly.

Proof: `rg -n "UTC weekday|not enforced|same cycle|bypasses" docs/TRADING_LOGIC.md` shows no stale Known gaps lines. Full verification commands pass.

depends on: 9, 11

### Task 11. Match the PortfolioConfig cooldown default to the YAML (part of #6)

Files: `core/config.py`, `tests/test_config.py`.

Failing test first:
- `test_portfolio_config_default_cooldown_matches_yaml`: `PortfolioConfig().reentry_cooldown_sec == 86400`. Fails today (600).
- `test_loaded_portfolio_cooldown_is_one_day`: `load_portfolio().reentry_cooldown_sec == 86400` (reads `config/symbols.yaml`, no network). Passes today; it guards the YAML value.

Fix: change the dataclass default from 600 to 86400. No change to `config/symbols.yaml`. Commit: `fix: default the re-entry cooldown to one day to match the config (#6)`.

independent

### Issue map

| Issue | Tasks |
|---|---|
| #5 | 2, 3, 10 |
| #6 | 4, 5, 7, 8, 9, 10, 11 |
| #16 | 1, 10 |
| #17 | 6, 10 |
| #18 | 2, 10 |

Parallel groups: tasks 1, 2, 3, 4, 5 and 11 can run at the same time. Then 6 and 8. Then 7. Then 9, then 10.

## 5. Tests

Write first, in this order: the #16 pin change (smallest), the Eastern-date and once-per-day tests (largest behaviour change), then storage and helper, then sleeve cooldown, then runner.

Edge cases to cover (all listed in the tasks):
- UTC and Eastern dates that differ: Thursday evening ET (UTC Friday), Friday evening ET (UTC Saturday), in both EST and EDT.
- Once per day across UTC midnight, and drift on a day that already had a rebalance.
- Cooldown boundary (exactly `reentry_cooldown_sec` is allowed), cooldown 0, naive timestamps, missing holdings row versus 0-share row, top-ups and sells never blocked, a blocked candidate does not take a slot.
- Same-cycle re-entry blocked for both exit kinds (timer and stop).
- Failed orders and other sleeves do not start a cooldown; dry-run and placed orders do.

Fixtures: no new files under `tests/fixtures`. `tmp_path` for SQLite, a fake CPGW client and a fake macro fetcher defined in `tests/test_portfolio_runner.py`, existing `make_macro`, `series`, `linear_closes` from `tests/conftest.py`. The network guard stays active.

## 6. Risks

- Schema: none. `Storage.last_exit_times` reads existing columns of `orders`. Existing `logs/bot.sqlite` files keep working; no ADR needed.
- Behaviour change on the running home server after `git pull && docker compose --profile auto up -d --build`:
  - Core calendar rebalances move from Thursday evening ET or Friday session to the first Friday ET cycle (normally the first `OPEN` cycle). In the deploy week, a rebalance logged on Thursday evening ET does not block a Friday rebalance, so there can be one extra rebalance that week.
  - Drift rebalances happen at most once per Eastern day.
  - The cooldown applies at once, using past SELL rows. A satellite symbol sold in the last 24 hours is not bought again until 24 hours have passed.
  - The core cooldown can leave one top-set slot in cash for up to a day. With `hold_top_n: 4` and `drift_threshold: 0.08`, three holdings at about 25% against a 1/3 drift target give 8.3% drift, so a drift rebalance runs once per day until the cooldown ends (at most one no-op style rebalance row per day). This is acceptable and now bounded by the once-per-day rule.
- `placed` live orders that never fill still count as exits, because the bot already updates `holdings` on placement (no fill sync). This is the existing bookkeeping model, not new.
- Dry-run and live rows share `orders` and `holdings`, so switching modes carries cooldowns over. Same as holdings today.
- DST approximation (#15): on the two DST change dates the Eastern hour can be off by one, so a cycle near Eastern midnight could land on the wrong date. Effect is at most one early or late trigger on those days.
- Performance: one grouped query on `orders` per sleeve per cycle; the table is small. Index `idx_orders_symbol` exists; no new index.
- Safety: no change to `assert_paper_mode`, `DRY_RUN` or `--live`. Runner tests use dry run and a fake client.
- Rollback: `git revert` the merge commit (or check out the previous tag) and rebuild. No data to migrate back.
- Follow-up issues to open at ship (not fixed here): `dashboard/templates/control.html` describes the runner as "long-term + tactical"; `core/sleeves/__init__.py` docstring says the satellite is "not built yet". (The `PortfolioConfig` default mismatch is fixed by task 11.)
- Reason string rename: anything outside this repo that matches `calendar: Fri close` in `targets.reason` or the events log stops matching. Nothing in this repo reads it (task 2 checks with `rg`).

## 7. Open questions

1. Wait for the Friday close? The issue asks to decide. Waiting would require Eastern time at or after 16:00 on the rebalance day for the calendar trigger (one extra check in `should_evaluate`, using `to_eastern(now).time()`). That changes when the core trades, beyond the user's decision on date basis and once per day. Recommended default: no, fire on the first eligible cycle of the Eastern rebalance day. If the answer is yes, add tests for Friday 15:00 ET (no trigger) and Friday 16:30 ET (trigger) to task 2, and drop task 3's function removal in favour of using the check inside the sleeve.
2. Partial sells and the cooldown. Recommended default: only a sell to 0 shares starts the cooldown, and top-ups of held positions are never blocked. The alternative (any SELL blocks any BUY) would stop core from rebalancing weights for a day after every trim.
3. Cooldown scope. Recommended default: per sleeve and symbol. Alternative: per symbol across sleeves (a satellite stop on SPY would block core buying SPY).
4. Same-cycle re-entry with cooldown 0. Recommended default: always blocked, so #17 stays fixed whatever the cooldown value.
5. Blocked core weight. Recommended default: stays in cash until the next rebalance after the cooldown. Alternative: spread over the other targets.
6. `is_core_rebalance_window`. Recommended default: remove it and its no-op branch (task 3). Alternative: keep it unused with a corrected docstring, or use it if question 1 is answered yes.
7. Calendar reason string. Recommended default: keep `calendar: Fri close` so dashboards and history stay comparable, even though it now fires at the first Friday cycle. Alternative: change it to `calendar: Fri (ET)` and update the pinned tests in task 2.

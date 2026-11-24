# Plan: trading-day hold timer and NYSE holiday calendar (#7, #15)

Branch: `fix/trading-days-and-holidays` (from main `5c22406`). The PR closes both issues (`Closes #7`, `Closes #15` in the PR body). Every fix task is test first: write the test, run it, see it fail on current code, then fix, see it pass, one `fix:` commit.


## Approval

Approved by the lead on 2026-11-24 while the user was away, with all Decisions and Open-question defaults as written. The user asked for unattended work within this scope; every choice is listed in the PR.

## Decisions (taken by the lead, all reversible)

1. Library: `exchange_calendars` (calendar `XNYS`), pinned `exchange_calendars>=4.13,<5` in `requirements.txt`. Add `tzdata>=2025.2` explicitly as well (it is already a transitive dependency, but Windows has no system tz database and `zoneinfo` needs it there).
2. `to_eastern` keeps its contract: naive input is UTC, aware input is converted, the result is a naive US Eastern wall-clock time. It now uses `zoneinfo.ZoneInfo("America/New_York")`. `_us_eastern_offset` and `_nth_weekday` are deleted (private, no other callers).
3. New regime `MarketRegime.HOLIDAY = "holiday"` for a weekday that is not an NYSE session. Saturday and Sunday stay `WEEKEND`. A holiday is `HOLIDAY` for the whole Eastern day (no `PRE_OPEN` or `AFTER_CLOSE` on it).
4. `HOLIDAY` is treated like `WEEKEND`: no satellite entries (`should_run_entries` already returns False for it), satellite exits still run (`should_run_exits` unchanged), and the core sleeve is skipped in `PortfolioRunner._cycle_core`.
5. Early closes come from the calendar: `OPEN` ends at the session close (13:00 ET on early-close days) and `AFTER_CLOSE` starts there. `minutes_to_close` uses the real close.
6. `minutes_to_open` is real elapsed minutes from now to the next session open (computed in UTC), so it is correct across DST weekends and holidays.
7. The calendar is built once per process and calendar year: `start="2020-01-01"`, `end` = 31 December of the current UTC year + 2. Cached with `functools.lru_cache` keyed on that year, so a long-running process rebuilds it after New Year. A date outside the window raises the library's error; the runner already logs it as `cycle error` and skips the cycle (fail closed).
8. Hold timer rule (#7): the age of a position is the number of NYSE sessions whose close is after `opened_at` and at or before `now`. The entry day counts once its close passes (also for an intraday entry), an early-close day counts as a full day. Exit when age >= `hold_max_days`. The config key stays `hold_max_days`.
9. The counter lives in `core/market_hours.py` (`trading_days_between`) and the sleeve imports it. No injection parameter: tests use the real calendar with fixed dates, which is deterministic and offline.
10. The exit reason becomes `hold timer: 10 trading days >= 10 max` (was `hold timer: 10d >= 10d max`). Noted under Changed in the CHANGELOG.
11. `CoreSleeve.should_evaluate` is not changed. When Friday is a holiday the runner skips that day, so the calendar rebalance waits for the next Friday (drift can still fire on other days).

## Open questions (default applied)

1. Move the weekly rebalance to Thursday when Friday is a holiday (Good Friday 2026-04-03, 2026-07-03, 2026-12-25)? Default: no change (decision 11).
2. Should an intraday entry day count as day 1? Default: yes, once its close passes (decision 8).
3. Should an early-close day count as a full trading day? Default: yes.
4. Exits on `WEEKEND` and `HOLIDAY` are planned and, with `--live`, sent as `DAY` limit orders, as today on weekends. Default: unchanged, out of scope. Follow-up issue if wanted.
5. Ad hoc closures (for example a national day of mourning) only reach us through a library release. Default: add a pip ecosystem to `.github/dependabot.yml` as a follow-up issue, not in this PR.
6. If `exchange_calendars` 4.13 does not work with the venv's pandas 3.1.1 (task 1 checks this): default is to stop and report to the lead, not to cap pandas.

## 1. Goal

Make the runner follow the real NYSE schedule. `market_state` uses an IANA time zone for US Eastern (correct at DST changes), knows full-day holidays and 13:00 early closes, and the runner does not evaluate core or open satellite positions on a holiday (#15). The satellite hold timer counts NYSE trading days, not calendar days (#7).

Out of scope: the rebalance weekday rule, order types and when exit orders are sent, intraday bars, the tactical sleeve, the dashboard, `core/ibkr_auth.py`, the paper lock, `DRY_RUN` and `--live`.

## 2. Current state

- `core/market_hours.py`: `_us_eastern_offset` (DST by UTC date only), `_nth_weekday`, `to_eastern` (naive Eastern out), `MarketRegime` (`OPEN`, `PRE_OPEN`, `AFTER_CLOSE`, `WEEKEND`), `MarketState`, `market_state` (weekday plus fixed 09:30 and 16:00, naive wall-clock minute math), `should_run_entries` (OPEN, AFTER_CLOSE), `should_run_exits` (always True). Module docstring says holidays are not handled.
- `core/sleeves/sentiment_satellite.py`: `SentimentSatelliteSleeve._maybe_exit_reason` uses `(now - opened).days`; naive `opened_at` is treated as UTC.
- `core/sleeves/core.py`: `CoreSleeve.should_evaluate` uses `to_eastern(...).date()`, `.weekday()` and `strftime`.
- `live/portfolio_runner.py`: `_cycle` builds `ms = market_state(now)` once and a `mkt_note` string (its `else` branch would label a new regime as AFTER-CLOSE); `_cycle_core` skips when `ms.regime in (MarketRegime.WEEKEND, MarketRegime.PRE_OPEN)`; `_cycle_satellite` calls `should_run_entries(ms)` and `should_run_exits(ms)`.
- Tests: `tests/test_sleeve_sentiment_satellite.py` pins calendar days through `pos(age_days=...)` (`test_exit_by_timer_at_ten_days`, `test_nine_day_old_position_stays`, `test_naive_opened_at_is_treated_as_utc`, `test_exit_without_input_uses_entry_price`, `test_exit_frees_a_slot_for_one_new_entry`, `test_allow_exits_false_blocks_exits`, `test_timer_exit_is_not_reentered_in_the_same_cycle`, `test_freed_slot_goes_to_another_candidate`). `tests/test_portfolio_runner.py` compares `ms.eastern_now == to_eastern(FRIDAY_OPEN)`. `tests/conftest.py` blocks all network access (autouse), so every test proves the calendar works offline. There is no `tests/test_market_hours.py` yet.
- Deps: `requirements.txt` has `pandas>=2.0`; the local venv has pandas 3.1.1, numpy 2.4.6, Python 3.11.5. CI (`.github/workflows/ci.yml`) installs `requirements-dev.txt` (which includes `requirements.txt`) on 3.11 and 3.12. `docker/Dockerfile` is `python:3.11-slim` with `TZ=UTC` and installs `requirements.txt`.
- Docs: `docs/TRADING_LOGIC.md` section 2 (regime table, DST paragraph), section 4 calendar trigger, satellite exits (`The code counts calendar days`), the risk table row `Hold timer`, Known gaps (two items). `config/symbols.yaml` line 143 comment. `docs/TECH_STACK.md` Data + numerics table.

### Library comparison (checked on PyPI, 2026-11-24)

| | exchange_calendars 4.13.3 | pandas_market_calendars 5.6.1 |
|---|---|---|
| Requires | numpy>=1.26.4, pandas>=1.5.0, pyluach, toolz, tzdata>=2025.2, korean_lunar_calendar | exchange-calendars>=3.3, pandas>=2.3 |
| Wheels | pure Python `py3-none-any` (216 KB), Python >=3.10,<4, classifiers 3.10 to 3.14 | pure Python, wraps exchange_calendars |
| Network | none: holidays are rules in the package (`exchange_calendar_xnys.py`, `us_holidays.py`) | none |
| Range | default end is today + 1 year (`GLOBAL_DEFAULT_END`), but `end=` can be set; rules extend forward. Past the end, `is_session` and friends raise `DateOutOfBounds` | same data |

Choice: `exchange_calendars`. It is the data source of the other library anyway, has fewer layers and an explicit `end`. Rejected `pandas_market_calendars`: one more dependency for the same XNYS data. Rejected a hand-written holiday list: it needs yearly maintenance, which the issue asks to avoid.

Verified from the XNYS rules in the 4.13.3 wheel (not by running it): regular open 09:30, close 16:00, `regular_early_close = time(13)`, `tz = ZoneInfo("America/New_York")`. 2026 dates that follow from those rules: Good Friday 2026-04-03 (pandas `GoodFriday.dates` confirmed it), Independence Day observed Friday 2026-07-03 (`nearest_workday`; Thursday 2026-07-02 is a full day because the July 3 early-close rules do not cover Friday), Thanksgiving 2026-11-26, early close Friday 2026-11-27 (`USBlackFridayInOrAfter1993`), early close Thursday 2026-12-24 (`ChristmasEveInOrAfter1993`), Christmas Friday 2026-12-25. Task 1 confirms these by running the library.

## 3. Design

### `core/market_hours.py`

```python
_ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=2)
def _nyse(end_year: int) -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS", start="2020-01-01", end=f"{end_year}-12-31")


def nyse_calendar() -> xcals.ExchangeCalendar:
    """XNYS calendar from 2020 to 31 December of the current year + 2."""
    return _nyse(datetime.now(timezone.utc).year + 2)


def to_eastern(utc: datetime) -> datetime:
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    return utc.astimezone(_ET).replace(tzinfo=None)


def trading_days_between(start: datetime, end: datetime) -> int:
    """NYSE sessions whose close is in (start, end]. Naive inputs are UTC."""
```

`market_state(now_utc)`:

1. Normalize `now_utc` to aware UTC, `et = to_eastern(now_utc)`, `day = pd.Timestamp(et.date())`.
2. If `cal.is_session(day)`: `open_utc = cal.session_open(day)`, `close_utc = cal.session_close(day)` (both UTC-aware). Before open: `PRE_OPEN`, minutes to `open_utc`. Before close: `OPEN`, minutes to `close_utc`. Otherwise `AFTER_CLOSE`, minutes to `cal.session_open(cal.next_session(day))`.
3. Else: `WEEKEND` if `et.weekday() >= 5`, else `HOLIDAY`. Minutes to `cal.session_open(cal.date_to_session(day, direction="next"))`.
4. Minutes stay `int(seconds // 60)` as today. `MarketState` fields are unchanged (`is_weekend` stays Saturday or Sunday only; `is_open` is True only in `OPEN`).

`trading_days_between`: return 0 if `end <= start`; otherwise count `cal.closes` values with `start < close <= end` (vectorized over about 2,000 sessions, cheap per position per cycle).

### Callers

- `SentimentSatelliteSleeve._maybe_exit_reason`: `age = trading_days_between(opened, now)`; reason `f"hold timer: {age} trading days >= {max} max"`. Module docstring and comments say trading days.
- `PortfolioRunner._cycle_core`: skip tuple becomes `(MarketRegime.WEEKEND, MarketRegime.HOLIDAY, MarketRegime.PRE_OPEN)`. `_cycle` gets a `HOLIDAY` branch for `mkt_note` (`HOLIDAY (next open in Nh, ET Thu 10:00)`).
- `CoreSleeve.should_evaluate`: unchanged; `to_eastern` keeps its return type.

Rejected alternatives:
- Treat a holiday as `WEEKEND`: hides why the runner skips on a Thursday, and `is_weekend` would lie.
- Return aware datetimes from `to_eastern`: breaks the naive comparisons in tests and in `should_evaluate` for no gain.
- Inject a trading-day counter into the sleeve: extra parameter with one real implementation; fixed-date tests are already deterministic.
- Count sessions strictly after the entry session: an intraday entry would hold one extra day, and the rule is harder to explain.
- Fall back to the weekday rule outside the calendar window: silent wrong answers; the dynamic window makes it unreachable for `now`.

## 4. Tasks

Verification for every task (macOS zsh, repo root): `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .`, `.venv/bin/python -m pytest -q`. Never `uv run` or `uv sync`.

1. **Add exchange_calendars and pin the calendar data** (`chore:`).
   Files: `requirements.txt` (add `exchange_calendars>=4.13,<5` and `tzdata>=2025.2` with a one-line comment), `docs/TECH_STACK.md` (row in Data + numerics), `core/market_hours.py` (add `_nyse` and `nyse_calendar` only, nothing else changes), new `tests/test_market_hours.py`.
   Steps: `uv pip install -r requirements-dev.txt`. Write tests through `nyse_calendar()`: 2026-04-03, 2026-07-03, 2026-11-26 and 2026-12-25 are not sessions; 2026-07-02 is a session that closes at 20:00 UTC; 2026-11-27 and 2026-12-24 close at 18:00 UTC (13:00 EST); 2026-11-13 opens 14:30 and closes 21:00 UTC; the calendar's last session is in or after December of the current year + 2. If any library date disagrees with this plan, trust the library, fix the test and the plan, and tell the lead. If import or these tests fail under pandas 3.1.1, stop (open question 6).
   Tests: `tests/test_market_hours.py` (all pass offline under the conftest network guard).
   `independent`

2. **Use zoneinfo for US Eastern** (`fix:`, part of #15).
   Files: `core/market_hours.py` (`to_eastern`, delete `_us_eastern_offset` and `_nth_weekday`, update the comment block), `tests/test_market_hours.py`.
   Failing tests first, at the 2026 DST changes (old values in brackets):
   - `to_eastern(2026-03-08 04:30 UTC)` is Sat 2026-03-07 23:30 (old: Sun 00:30).
   - `to_eastern(2026-03-08 06:30 UTC)` is 01:30 (old: 02:30); `07:30 UTC` is 03:30.
   - `to_eastern(2026-11-01 04:30 UTC)` is Sun 00:30 (old: Sat 23:30); `05:30 UTC` is 01:30 (old: 00:30); `06:30 UTC` is 01:30.
   - Naive input equals the same instant with `tzinfo=timezone.utc`; an aware non-UTC input converts correctly; the result is naive.
   `depends on: 1`

3. **NYSE holidays and early closes in market_state** (`fix:`, closes #15).
   Files: `core/market_hours.py` (`MarketRegime.HOLIDAY`, `market_state`, module docstring), `live/portfolio_runner.py` (`_cycle` `mkt_note` branch, `_cycle_core` skip tuple and its comment, the `_cycle` comment above the core call), `tests/test_market_hours.py`, `tests/test_portfolio_runner.py`.
   Failing tests first (UTC inputs, expected Eastern in brackets):
   - 2026-11-26 15:00 (Thu 10:00, Thanksgiving): `HOLIDAY`, `is_open` False, `is_weekend` False, `minutes_to_open == 1410`. 12:00 (07:00) is `HOLIDAY`, not `PRE_OPEN`.
   - 2026-04-03 15:00, 2026-07-03 14:00, 2026-12-25 15:00: `HOLIDAY`.
   - 2026-11-27 17:30 (12:30, early close): `OPEN`, `minutes_to_close == 30`. 18:30 (13:30): `AFTER_CLOSE`, `minutes_to_open == 4080`.
   - 2026-12-24 17:59: `OPEN`, `minutes_to_close == 1`; 18:00: `AFTER_CLOSE`.
   - 2026-11-25 22:00 (Wed 17:00, eve of Thanksgiving): `AFTER_CLOSE`, `minutes_to_open == 2430`.
   - DST weekends: 2026-03-07 17:00 (Sat 12:00 EST) `minutes_to_open == 2670` (old 2730); 2026-10-31 16:00 (Sat 12:00 EDT) `minutes_to_open == 2790` (old 2730).
   - Regression: 2026-11-13 15:00 (Fri 10:00) `OPEN`, `minutes_to_close == 360`; 2026-11-13 14:00 `PRE_OPEN`, `minutes_to_open == 30`.
   - `should_run_entries` is False and `should_run_exits` is True for a `HOLIDAY` state.
   - Runner: `_cycle_core` with `market_state(2026-11-26 15:00 UTC)` logs a `skip (holiday, ...)` event and writes no rebalance or order (reuse `make_runner` and `ranked_closes`). `_cycle_satellite` on the same state with a bullish AAPL sentiment row writes no BUY.
   `depends on: 2`

4. **Hold timer counts NYSE trading days** (`fix:`, closes #7).
   Files: `core/market_hours.py` (`trading_days_between`), `core/sleeves/sentiment_satellite.py` (`_maybe_exit_reason`, docstrings), `tests/test_market_hours.py`, `tests/test_sleeve_sentiment_satellite.py`.
   Failing tests first. `trading_days_between` (UTC):
   - 2026-10-30 14:00 to 2026-11-13 12:00 is 10; 2026-11-02 15:30 to 2026-11-13 12:00 is 9.
   - 2026-11-25 15:00 to 2026-11-30 15:00 is 2 (Thanksgiving and the weekend skipped, early close counted).
   - 2026-12-23 15:00 to 2026-12-28 15:00 is 2.
   - Same-day: 2026-11-13 15:00 to 20:59 is 0, to 21:00 is 1 (close is inclusive).
   - `end <= start` is 0; naive inputs equal UTC-aware inputs.
   Sleeve tests: replace `pos(age_days=...)` with `pos(opened_at=...)` and module constants `TEN_SESSIONS = datetime(2026, 10, 30, 14, tzinfo=timezone.utc)` and `NINE_SESSIONS = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)`; the default stays `NOW - timedelta(days=1)` (1 session). Let `run()` take `now=NOW`. Update the eight tests listed in Current state to use these constants. Add the issue's case: opened Fri 2026-11-20 15:00 UTC, no exit at 2026-11-30 15:00 (10 calendar days, 5 sessions) or at 2026-12-04 20:59 (9 sessions), exit at 2026-12-04 21:00 with reason `hold timer: 10 trading days >= 10 max`.
   `depends on: 3`

5. **Docs and changelog** (`docs:`).
   Files: `docs/TRADING_LOGIC.md` (section 2: add `HOLIDAY` row, early closes, replace the DST paragraph with "US Eastern via `zoneinfo`, schedule from the `exchange_calendars` XNYS calendar"; regime list: core skipped in `WEEKEND`, `HOLIDAY` and `PRE_OPEN`; section 4 calendar trigger: a holiday Friday is skipped; satellite exits: trading-day rule from decision 8; risk table row `Hold timer`; Known gaps: remove the hold timer and holiday/DST items), `config/symbols.yaml` (comment: `# hard exit after N NYSE trading days (session closes after entry)`), `CHANGELOG.md` (`[Unreleased]`: Fixed lines for #7 and #15, Added line for `exchange_calendars` and `tzdata`, Changed line for the new exit reason and the `HOLIDAY` regime, and the real test count in the offline test suite line).
   Check: `rg -n -i "calendar days|approximat|holidays are not" docs config core` returns nothing stale.
   `depends on: 4`

## 5. Tests

- Write tests in task order; each fix task starts with tests that fail on the code before it.
- All fixtures are fixed UTC datetimes in 2026, so results do not depend on the real clock. The only clock-dependent code is the calendar window in `nyse_calendar()`, and 2026 dates stay inside it until the start bound (2020) matters.
- Edge cases: exact open and close minutes, early close boundary, holiday morning (not `PRE_OPEN`), eve of a holiday, DST Sundays and the weekends around them, naive vs aware inputs, `end <= start`.
- No new fixture files. The conftest network guard stays; the calendar must load with it on.
- Expected count: 169 plus the new tests. Report the real number.
- Optional on the server, not at night: one dry-run cycle `python -m live.portfolio_runner --once` against the paper gateway (never `--live`).

## 6. Risks

- Behaviour change on the server: no core evaluation and no satellite entries on NYSE holidays; on early-close days, after-close logic starts at 13:00 ET; open satellite positions exit later than before (sessions instead of calendar days), including positions opened before the deploy.
- Calendar data end: the window is rebuilt each year, so it does not run out. Ad hoc closures need a library update (open question 5). Out-of-window dates fail closed as a logged cycle error.
- pandas 3 compatibility of `exchange_calendars` is not proven until task 1 runs. It uses a private pandas import (`pandas._libs.tslibs.conversion.localize_pydatetime`), which exists in the venv's pandas 3.1.1.
- Startup cost: building the calendar takes a moment once per process. Docker image grows by well under 2 MB (pure Python wheels plus tzdata).
- Deployment: Docker `git pull && docker compose --profile auto up -d --build` (rebuild installs the new deps). systemd (Ubuntu): `uv pip install -r requirements.txt` in `/opt/trading-bot/.venv`, then restart the units. No schema or config migration.
- Rollback: revert the PR commits and rebuild or reinstall; nothing is stored in the database, so rollback is clean.

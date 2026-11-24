# Plan: sleeve budgets from the live account value (#20)

Branch: `feat/live-account-equity` (from `main` at 5c22406, 169 offline tests). Approved by the lead on behalf of the user; judgement calls are in "Decisions" with the safe, reversible choice taken.


## Approval

Approved by the lead on 2026-11-24 while the user was away, with all Decisions and Open-question defaults as written (sanity range 0.25x to 4x of starting_equity). The user asked for unattended work within this scope; every choice is listed in the PR.

## 1. Goal

Each portfolio cycle, read the paper account's net liquidation value from the Client Portal Gateway and use it as total equity for the sleeve budgets (core 70%, sentiment 20%). When the gateway does not answer, or the value fails sanity checks, fall back to `portfolio.starting_equity`. Log one event per cycle that says which value was used and, for a fallback, why. Tests are offline with a fake client.

Out of scope:

- Any change to the paper lock: `assert_paper_mode`, `PAPER_PORTS`, the `DRY_RUN` default, `--live` handling. `core/ibkr_auth.py` is not touched.
- Changes to `core/cpgw_client.py` (the needed method `account_summary()` already exists).
- Tracking equity from the `holdings` table and latest closes (the issue's second option).
- The `/sleeves` dashboard page (it still shows the legacy long_term and tactical sleeves; separate follow-up).
- `live/runner.py` (single-symbol tactical runner, fixed quantity).
- Scrubbing account ids from the existing order-error events (follow-up, see Risks).

## 2. Current state

- `live/portfolio_runner.py`
  - `PortfolioRunner.start()` (line 134): the banner computes per-sleeve dollars from `self.portfolio_cfg.starting_equity`. It builds `self.client = CPGWClient(...)` from `read_session()` and sets `client.account_id` from the session file when present.
  - `PortfolioRunner._cycle()` (line 214): `total_equity = self.portfolio_cfg.starting_equity`, then passes it to `_cycle_satellite(cycle_id, total_equity, ms, now)` and `_cycle_core(...)`.
  - `_cycle_core()` line 251: `sleeve_equity = total_equity * self.core_cfg.allocation_pct`, also written as `equity_before` in `storage.log_rebalance`.
  - `_cycle_satellite()` line 464: `sleeve_equity = total_equity * self.sent_cfg.allocation_pct`, passed to `self.satellite.evaluate(...)`.
  - `loop()` catches `NotAuthenticatedError` and other exceptions per cycle and logs them.
- `core/cpgw_client.py`
  - `CPGWClient.account_summary()` calls `_ensure_account()` (which runs `bootstrap()` if `account_id` is None) then `GET /v1/api/portfolio/{account_id}/summary`.
  - `_request()` raises `NotAuthenticatedError` on 401, `CPGWError` on other 4xx/5xx, transport errors and non-JSON. Error messages include the request path, so a summary error contains the full account id.
  - httpx client timeout is 30 s (constructor default).
- `core/config.py`: `PortfolioConfig` (line 85) has `starting_equity: float = 100_000.0`, `cycle_interval_sec`, `reentry_cooldown_sec`. `load_portfolio()` builds it with `PortfolioConfig(**raw)` from the `portfolio:` YAML block, so new fields with defaults are backward compatible.
- `live/storage.py`: `log_event(level, source, message)`; an `equity` table with `log_equity(equity, cash, positions)` and `equity_history(limit)` exists but nothing writes to it. `dashboard/app.py` already serves it at `/api/equity`.
- `dashboard/app.py` `control_page()` passes `portfolio` to `templates/control.html`, which prints `Equity ${{ portfolio.starting_equity }}`.
- `config/symbols.yaml` line 101: `starting_equity: 100000.0` with no comment.
- `docs/TRADING_LOGIC.md`: section 1 says total equity is the static `starting_equity`; section 8 Known gaps lists "Static starting equity"; section 9 table says "Change the total budget: `portfolio.starting_equity`".
- `tests/test_portfolio_runner.py`: `FakeClient` (only `get_historical_data`), `make_runner()` (builds a runner with `__new__`, tmp_path `Storage`, dry run), `freeze_runner_clock()`. `tests/conftest.py` blocks all sockets.

## 3. Design

### Data flow

```
_cycle()
  reading = resolve_total_equity(self.client, self.portfolio_cfg)
  storage.log_event(level, "portfolio", f"cycle={cycle_id} {format_equity_event(reading)}")
  if reading.source == "gateway": storage.log_equity(reading.equity)
  total_equity = reading.equity  -> _cycle_satellite / _cycle_core (unchanged)
```

### New module `live/account_equity.py`

```python
@dataclass(frozen=True)
class EquityReading:
    equity: float
    source: str  # "gateway" or "starting_equity"
    reason: str  # "" for gateway, otherwise why the fallback was used
    currency: str | None = None


def parse_net_liquidation(summary: object) -> tuple[float, str | None]:
    """Return (amount, currency) from a /portfolio/{id}/summary response. Raises ValueError."""


def resolve_total_equity(client: object, cfg: PortfolioConfig) -> EquityReading: ...


def format_equity_event(reading: EquityReading) -> str: ...
```

`parse_net_liquidation`: the summary is a dict; take `summary["netliquidation"]` (a dict with `amount`, `currency`, `isNull`, ...). Raise `ValueError` with a short reason when: the summary is not a dict, the key is missing or not a dict, `isNull` is true, or `amount` is missing or not a number (bool rejected). Currency is returned as given (may be None).

`resolve_total_equity` order of checks, first failure wins and returns a fallback reading `EquityReading(cfg.starting_equity, "starting_equity", reason)`:

1. `cfg.use_account_equity` is false: reason `"disabled (portfolio.use_account_equity: false)"`.
2. `client is None`: reason `"no gateway client"`.
3. `client.account_summary()` raises anything (`NotAuthenticatedError`, `CPGWError`, `AttributeError`, timeouts wrapped as `CPGWError`): reason `"gateway error: <ExcClass>: <message>"`. The message is passed through `_mask_account(text, client.account_id)`, which replaces the account id with `***` plus its last 2 characters, and is cut to 160 characters.
4. `parse_net_liquidation` raises `ValueError`: reason `"bad summary: <why>"`.
5. Not finite (NaN or inf): `"net liquidation not finite"`.
6. `<= 0`: `"net liquidation not positive (<value>)"`.
7. Currency present and not equal to `cfg.equity_currency` (case-insensitive): `"currency EUR, expected USD"`. Missing currency is accepted.
8. Outside `[cfg.equity_min_ratio * starting_equity, cfg.equity_max_ratio * starting_equity]` (bounds inclusive): `"net liquidation 1,000,000.00 outside 25,000.00 to 400,000.00 (0.25x to 4x starting_equity)"`.

Otherwise return `EquityReading(amount, "gateway", "", currency)`. The function never raises (one outer `except Exception` guard around everything as well) and does not log; the runner logs.

`format_equity_event`:

- gateway: `equity: net liquidation 123,456.78 USD (gateway)`
- fallback: `equity: starting_equity 100,000.00 (fallback: <reason>)`

No account id appears in either form (only the masked one inside a gateway error reason).

### Config (`core/config.py`, `PortfolioConfig`)

New fields, all with defaults so existing YAML still loads:

| Field | Default | Meaning |
|---|---|---|
| `use_account_equity` | `True` | Off switch. False means always `starting_equity` (old behaviour). |
| `equity_min_ratio` | `0.25` | Reject net liquidation below this multiple of `starting_equity`. |
| `equity_max_ratio` | `4.0` | Reject net liquidation above this multiple of `starting_equity`. |
| `equity_currency` | `"USD"` | Expected currency of the summary value. |

A `__post_init__` raises `ValueError` unless `starting_equity > 0` and `0 < equity_min_ratio <= 1 <= equity_max_ratio`. (`starting_equity` must stay positive because it is the fallback and the range anchor.)

### Runner (`live/portfolio_runner.py`)

- `_cycle()`: replace line 214 with the data flow above. Event level is `info` for gateway, `warn` for any fallback except the deliberate `use_account_equity: false` (which is `info`). Also `print()` the event line so `--once` runs show it.
- `start()`: banner wording only. Keep computing dollars from `starting_equity` (no gateway call before the client exists), but label it, for example `budget base starting_equity $100,000 (net liquidation per cycle when valid)` when `use_account_equity` is true.
- `_cycle_core` and `_cycle_satellite` are unchanged; they already derive sleeve equity from the `total_equity` argument.

### Dashboard

`control_page()` also passes `last_equity` (first row of `Storage().equity_history(1)`, or None). `control.html` replaces `Equity $X` with `Starting equity $X (fallback)` and, when `last_equity` exists, `last net liquidation $Y at <ts>`. Fallback reasons are visible in the existing events timeline. No schema change.

### Decisions (taken, reversible)

1. **Endpoint**: `GET /v1/api/portfolio/{accountId}/summary` via the existing `CPGWClient.account_summary()`, field `netliquidation.amount`. No new client method. The account id comes from the keeper's session file or `bootstrap()`, exactly as for orders, so it is the account of the logged-in paper session. No account id is hard-coded or logged in full.
2. **Dry run and live**: read the value in both modes. Dry run exists to rehearse live sizing, and reading is side-effect free. When the gateway is not logged in, a dry run falls back to `starting_equity`, which matches today's behaviour.
3. **Sanity range 0.25x to 4x** (not the 0.1x to 10x suggested): a fresh IBKR paper account often holds 1,000,000, exactly 10x the 100,000 default, and would pass a 10x bound and multiply every order size by 10. With 4x it is rejected, the bot keeps sizing from 100,000, and a `warn` event says why. Users who want to size from such an account set `starting_equity` to the paper balance. The range still allows a 75% drawdown or a 300% gain. Both bounds are config, so this is a YAML change to undo.
4. **Fallback, never abort**: any failure falls back and the cycle continues. The later bar fetches will hit the same session problem and are handled as today.
5. **Missing currency accepted, mismatched currency rejected**: some summary responses omit or null the currency; rejecting those would make the feature never apply. A present, different currency is a real mismatch.
6. **Timeout**: rely on the client's existing 30 s timeout. One call per hourly cycle; a hung gateway would also stall the bar fetches that follow, so a shorter per-call timeout buys little and would mean editing `core/cpgw_client.py`.
7. **One read per cycle, no caching across cycles**: the cycle is hourly; a stale cache adds a failure mode for no gain.
8. **Gateway readings go to the existing `equity` table** (via `log_equity`), fallbacks do not, so `/api/equity` only ever contains real account values.
9. **Off switch** `portfolio.use_account_equity` (default true) gives a config-only rollback.

### Rejected alternatives

- Equity from `holdings` plus latest closes: duplicates what the broker already computes and ignores cash, fills and fees.
- New `CPGWClient.net_liquidation()` method: one more client method to test for a two-line parse; keeping parsing in `live/account_equity.py` keeps the client unchanged.
- Read once in `start()`: budgets would not follow gains and losses during a long run, which is the point of #20.
- Gateway value only in `--live`: dry runs would size differently from live runs, which defeats rehearsal.
- New `equity_source` column or table for the dashboard: a schema change for information the events log already carries.
- Abort the cycle on a bad value: core rebalances are rare (Friday or drift); skipping one is worse than sizing from the configured budget.

## 4. Tasks

Run before each commit (macOS): `.venv/bin/ruff format . && .venv/bin/ruff check . && .venv/bin/python -m pytest -q`. Write the failing test first.

1. **Config fields.** `test: ` then `feat: add account equity settings to PortfolioConfig (#20)`. Add `use_account_equity`, `equity_min_ratio`, `equity_max_ratio`, `equity_currency` and the `__post_init__` check to `PortfolioConfig` in `core/config.py`. Do not touch `AppConfig`, `PAPER_PORTS` or `assert_paper_mode`. Tests in `tests/test_config.py`: defaults; `load_portfolio()` accepts the new keys from YAML (monkeypatch `core.config._load_yaml`) and still loads a block without them; `ValueError` for `equity_min_ratio=0`, `equity_min_ratio=1.5`, `equity_max_ratio=0.5`, `starting_equity=0`. Existing paper-lock tests still pass unchanged.
   independent

2. **Equity helper.** `feat: resolve total equity from gateway net liquidation (#20)`. New `live/account_equity.py` (`EquityReading`, `parse_net_liquidation`, `resolve_total_equity`, `format_equity_event`, `_mask_account`) and new `tests/test_account_equity.py` with a small `FakeAccountClient(summary=None, exc=None, account_id="DU0000000")` (a made-up id in the documented paper format, not a real one). Tests: valid value used with source `gateway` and currency; fallback with the exact reason for each of: `use_account_equity=False`, client None, `NotAuthenticatedError`, `CPGWError`, generic `RuntimeError`, client without `account_summary` (AttributeError), summary not a dict, missing `netliquidation`, `isNull: true`, missing `amount`, string amount, NaN, inf, 0, negative, `EUR` currency, below 0.25x, above 4x (and 1,000,000 against 100,000 rejected); bounds inclusive (exactly 0.25x and 4x accepted); missing currency accepted; error reason does not contain the full account id but contains the masked tail; reason length capped; `format_equity_event` strings for both sources.
   depends on: 1

3. **Runner wiring.** `feat: size sleeves from the live account value (#20)`. In `live/portfolio_runner.py`: `_cycle()` calls `resolve_total_equity`, logs the event, writes `log_equity` for gateway readings, passes `reading.equity` on; banner wording in `start()`. Tests in `tests/test_portfolio_runner.py`: give `FakeClient` an optional `summary` / `exc` and an `account_summary()` method (default behaviour raises, so existing tests keep using `starting_equity`). New tests, all dry run with frozen clock: (a) `_cycle()` passes the gateway value to both sleeves (capture `eq` like the one-clock test) and logs an `info` event `equity: net liquidation ... (gateway)` and one `equity` row; (b) gateway raises `NotAuthenticatedError`: sleeves get `starting_equity`, a `warn` event with `fallback: gateway error`, no `equity` row, and the cycle still runs; (c) out-of-range value: `starting_equity` used, `warn` event; (d) real budgets: calendar-trigger setup from `test_cycle_runs_the_core_calendar_trigger_and_cooldown_on_its_own_clock` with `starting_equity=20_000` and net liquidation `10_000 / 0.7`, assert `rebalances.equity_before == pytest.approx(10_000)`; wrap `r.satellite.evaluate` to capture `sleeve_equity` and assert it equals `0.2 * net liquidation`; (e) exactly one equity event per `_cycle()`; (f) `dry_run` stays True and no order has `dry_run=0`.
   depends on: 2

4. **Dashboard text.** `feat: show the last net liquidation on the control page (#20)`. `dashboard/app.py` `control_page()` adds `last_equity`; `dashboard/templates/control.html` wording as in Design. Test in new `tests/test_dashboard_control.py` with `fastapi.testclient.TestClient`: monkeypatch `dashboard.app._storage` to a tmp_path `Storage`, `dashboard.app.list_models` to `lambda: []`, `dashboard.app.procmgr.list_processes` to `lambda: []`; assert the page shows `Starting equity $100000` and, after `log_equity(123456.78)`, the net liquidation line. If `import dashboard.app` cannot run offline (heavy imports), drop the TestClient test, keep the change, and verify manually with `uvicorn dashboard.app:app --port 8765`; say so in the PR.
   depends on: 3

5. **Docs and changelog.** `docs: document live account equity sizing (#20)`. `docs/TRADING_LOGIC.md`: section 1 (total equity is net liquidation when valid, else `starting_equity`; the range check; the event), the diagram label, remove "Static starting equity" from section 8, add rows to section 9 (`portfolio.use_account_equity`, `equity_min_ratio`, `equity_max_ratio`, `equity_currency`, and "size from a 1,000,000 paper account: set `starting_equity: 1000000`"). `config/symbols.yaml`: comments on `starting_equity` (fallback and range anchor) and the four new keys written out with their defaults. `CHANGELOG.md` `[Unreleased]`: Added (account equity sizing, config keys, test count), Changed (sleeve budgets follow the paper account value; dry runs read it too). No other files.
   depends on: 4

## 5. Tests

- Start with `tests/test_account_equity.py` (task 2): it pins the parse and every fallback reason without the runner.
- Fixtures: plain dicts shaped like the CPGW summary, for example `{"netliquidation": {"amount": 123456.78, "currency": "USD", "isNull": False}}`. No recorded gateway responses, no real account ids (use `DU0000000`).
- Edge cases: bool amount (`True`) rejected; amount as int accepted; `isNull` true; currency lower case `usd` accepted; bounds inclusive; `starting_equity` changed in config moves the bounds.
- The conftest network guard stays active; the fake client never opens a socket. Every runner test is dry run.
- Paper lock: existing `tests/test_config.py` lock tests must pass unmodified, and `git diff main -- core/config.py` must show only `PortfolioConfig` changes.
- Manual check (macOS or the Ubuntu server, paper gateway logged in): `.venv/bin/python -m live.portfolio_runner --once` (never `--live`) and confirm one `equity:` line in stdout and in the dashboard events.

## 6. Risks

- **Sizing change**: budgets now follow the paper account. If it is within 0.25x to 4x of 100,000 but not equal, every order size changes proportionally on the next cycle, and core drift checks may fire a rebalance right away because `sleeve_equity` moved. Expected and logged; mention it in the PR.
- **Paper balance of 1,000,000**: rejected by default with a `warn` event every hour. Loud but safe. Fix by setting `starting_equity` to the balance, or `use_account_equity: false`.
- **Wrong account**: the value comes from the account bound to the session (session file `account_id` or `bootstrap()`'s first account), the same account orders go to. If a login ever exposed a live account first, orders would already be at risk; this feature adds no new path. The paper lock is unchanged.
- **Account id leakage**: summary errors contain the path with the account id; `_mask_account` handles this for the new event. Existing order-error events in the runner have the same leak: suggested follow-up issue, not fixed here.
- **Latency**: up to 30 s per cycle when the gateway hangs, inside an hourly cycle. Acceptable.
- **Mid-cycle consistency**: one reading per cycle, shared by both sleeves.
- **No migration**: the `equity` table already exists in the schema (`CREATE TABLE IF NOT EXISTS`). New YAML keys are optional.
- **Deployment**: `git pull && docker compose --profile auto up -d --build` on the Ubuntu server. `config/` is mounted read only, so the new keys can be set in YAML without a rebuild, followed by a restart of the `portfolio` service.
- **Rollback**: set `portfolio.use_account_equity: false` in `config/symbols.yaml` and restart `portfolio` (no code change), or revert the merge commit. Rows written to `equity` are harmless and can stay.

## 7. Open questions (defaults applied)

1. Should a 1,000,000 paper balance be used as is? Default: no, the 4x bound rejects it; the user can set `starting_equity` to the paper balance.
2. Should the `/sleeves` page (legacy long_term and tactical) be updated to the core and sentiment sleeves with live equity? Default: no, follow-up issue.
3. Should the start banner query the gateway for its numbers? Default: no, it labels `starting_equity` as the base and the per-cycle event shows the real value.
4. Should repeated identical fallback warnings be throttled (one per hour today)? Default: no throttling; hourly is low volume.
5. Should a missing currency be rejected? Default: accepted, mismatch rejected.

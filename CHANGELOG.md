# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Loader for a private, git-ignored `core/ibkr_auth_private.py` login module. The public stub is used only when that file is missing.
- `SENTIMENT_PROVIDER` in `.env` overrides the sentiment provider in `config/symbols.yaml`.
- `DASHBOARD_HOST` in `.env` sets the dashboard bind address.
- `ibkr_gateway` container (compose profile `gateway`), published on 127.0.0.1:5000 only, with the v1.0.0 release pinned by SHA-256.
- "Home server (Docker)" guide in `deploy/README.md`.
- ADR 0001: run the IBKR gateway as a native executable in a container.
- `exchange_calendars` (NYSE `XNYS` calendar) and `tzdata` in `requirements.txt`. `core/market_hours.py` uses them for sessions, holidays and early closes (#15).
- `tests/test_config.py`: regression tests for the paper-mode lock and its error message.
- `requirements-dev.txt` with pytest.
- `pyproject.toml` with ruff and pytest settings. `core/ibkr_auth.py` is excluded from ruff (#4).
- `.gitattributes` that forces LF line endings.
- ruff 0.17.4, pinned in `requirements-dev.txt`.
- Offline test suite (178 tests) for scoring, the core and sentiment satellite sleeves, signal fusion, position sizing, Alpha Vantage sentiment parsing (hand-built fixture) and the paper lock. A `conftest.py` blocks network access and isolates `.env` (#4).
- GitHub Actions CI on Python 3.11 and 3.12. It runs on push to main, on pull requests, weekly and on manual dispatch. The README has a status badge. Actions are pinned to commit SHAs (#4).
- `.github/dependabot.yml`: weekly updates for the GitHub Actions used in CI (#4).
- `backtest/` package and `requirements-backtest.txt`: replays the live core sleeve from 2008 against SPY, 60/40 and equal weight, with ablations. `python -m backtest.run` writes `docs/backtest/` (results, chart, JSON); the README has a Results section and `docs/adr/0002-own-backtest-replay.md` records the design.

### Changed

- The Docker guide in `deploy/README.md` requires Docker Engine 28 or later.
- `DASHBOARD_HOST` defaults to `127.0.0.1`, so the dashboard is no longer reachable on the tailnet IP directly.
  **Upgrade note:** run `sudo tailscale serve --bg 8765` and open the `https://<machine>.<tailnet>.ts.net` URL instead.
- The runner no longer evaluates core or opens satellite positions on NYSE holidays. A new `HOLIDAY` market regime is treated like `WEEKEND`: satellite exits still run, entries and the core sleeve do not (#15).
- On early-close days (13:00 ET) `OPEN` ends at the real close and after-close logic starts there (#15).
- The satellite hold timer exit reason is now `hold timer: 10 trading days >= 10 max` (was `hold timer: 10d >= 10d max`). Anything that matches the old string in `targets.reason` or the events log must be updated. Open positions exit later than before when a weekend or holiday falls in their hold period, including positions opened before the upgrade (#7).
- The core calendar trigger reason is now `calendar: Fri` instead of `calendar: Fri close`, because it fires on the first Friday cycle in Eastern time, not at the close (#5). Anything that matches the old string in `targets.reason` or the events log must be updated.
- The `PortfolioConfig.reentry_cooldown_sec` default is now 86400 (was 600), matching `config/symbols.yaml` (#6).
- The codebase is formatted with ruff. Imports are sorted and unused imports are removed (#4).
- `docs/TRADING_LOGIC.md` is rewritten to match the code: core sleeve (70%), sentiment satellite (20%) and the disabled tactical sleeve. It has a Known gaps section (#4).
- The documented install root is now `/opt/trading-bot` (the repo root, no `bot/` subfolder) in the deploy docs, all four systemd units and `deploy/backup.sh` (#2).
  **Upgrade note:** existing systemd installs at `/home/USER/trading-bot/bot` should either move the clone to `/opt/trading-bot` or edit the paths in each unit after copying it.
- systemd units use `ProtectSystem=strict`, so only `logs/` and `models/artifacts/` are writable. Run `mkdir -p logs` before the first start.
- systemd ordering (`After=`/`Wants=`) now names the templated units (`tradingbot-*@%i.service`).
- Alpha Vantage requests now send `time_from`, so sentiment covers the last `lookback_hours`, now 24 by default (was 6, never sent). Fewer articles on quiet days (#10).
- The README "Run it" section uses uv (#13).

### Fixed

- The satellite hold timer counts NYSE trading days instead of calendar days, so `hold_max_days` matches its meaning (#7).
- US Eastern time uses the `America/New_York` zone, so the market state is correct on the DST change days. Holidays and early closes are known (#15).
- `size_portfolio` no longer returns no decisions for a generator input when the gross cap applies (#9).
- `AlphaVantageProvider.lookback_hours` is sent to the API instead of being ignored (#10).
- `MacroSnapshot.as_of` is timezone-aware UTC instead of the deprecated `datetime.utcnow()` (#11).
- The core weekday and once-per-day checks use the US Eastern date instead of UTC, so the weekly rebalance runs on the first Friday cycle in Eastern time and no longer on Thursday evening (#5).
- `portfolio.reentry_cooldown_sec` is enforced for the core and sentiment sleeves: a symbol a sleeve sold to 0 shares is not bought back by that sleeve for 24 hours. Top-ups and sells are never blocked (#6).
- Satellite skip reasons have no leading space (#16).
- The satellite no longer re-enters a symbol in the cycle it exits it (#17).
- The core drift trigger respects the once-per-day rule, so the core sleeve rebalances at most once per US Eastern day (#18).
- Docs and code now agree on one dashboard port (8765) and one broker port (5000, Client Portal Gateway). The README and the `dashboard/app.py` docstring no longer say 8080 or 8000, and the `assert_paper_mode` error no longer mentions the old 4002/7497 TWS ports (#2).
- Remaining `bot/.env` references in code comments, error messages and docs now point at `.env` in the repo root.

### Removed

- `core.market_hours.is_core_rebalance_window` (it had no effect) (#5).

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Headless login to the gateway's own login page with Playwright (`core/gateway_login.py`), wired through the git-ignored `core/ibkr_auth_private.py` (template: `deploy/ibkr_auth_private.py.example`). Headless Chromium only, no X server or Xvfb. Certificate errors are never ignored: the browser accepts only the gateway's own key. Playwright is pinned to 1.64.1 (released 2026-10-22). The Chromium sandbox stays off, now set explicitly and documented with its mitigations (ADR 0003, `SECURITY.md`).
- Credentials from a chmod 600 secret file or Docker secret (`secrets/ibkr_login`). The keeper refuses a group or world readable file, a file owned by another uid, and a set `IBKR_PASSWORD`.
- Keeper session policy: a check every 60 s, a new login after the nightly gap, Saturday maintenance and the 24 h reset, backoff capped at 30 minutes, and a persisted lockout after any wrong password, second-factor prompt or unknown page state, or after 3 submitted attempts in 24 h. New flag `python -m live.cpgw_keeper --reset-lockout`.
- Paper account guard: the keeper logs out and locks itself when an account id does not start with `DU`.
- Telegram notifier (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`): daily summary, every order (tagged `[DRY-RUN]` or `[PAPER]`), errors, session lost, lockout, sentiment data missing. Deduplicated and capped at 20 messages per hour. `python -m live.telegram_notifier --test` sends a test message.
- `python -m live.sentiment_worker --check` exits 2 when the provider returns no data.
- CI step that fails when the private login module or files under `secrets/` or `vendor/` are tracked.
- `scripts/fetch_gateway.sh` downloads the pinned gateway release into the git-ignored `vendor/` folder and checks its SHA-256.
- Memory limits (`mem_limit` equal to `memswap_limit`) for every compose service.
- "Phase 1 upgrade (paper account only)" server steps in `deploy/README.md`, with a check and a safe fallback for each thing only the server can confirm.
- ADR 0003: headless gateway login and file secrets.
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
- Offline test suite (964 tests) for scoring, the core and sentiment satellite sleeves, signal fusion, position sizing, Alpha Vantage sentiment parsing (hand-built fixture) and the paper lock. A `conftest.py` blocks network access and isolates `.env` (#4).
- Account equity sizing: each cycle sizes the sleeves from the paper account's net liquidation and logs one `equity:` event. New `portfolio` keys `use_account_equity`, `equity_min_ratio`, `equity_max_ratio`, `equity_max_step`, `equity_last_max_age_sec` and `equity_currency` (#20).
- The control page shows the last net liquidation next to the starting equity (#20).
- GitHub Actions CI on Python 3.11 and 3.12. It runs on push to main, on pull requests, weekly and on manual dispatch. The README has a status badge. Actions are pinned to commit SHAs (#4).
- `.github/dependabot.yml`: weekly updates for the GitHub Actions used in CI (#4).
- `backtest/` package and `requirements-backtest.txt`: replays the live core sleeve from 2008 against SPY, 60/40 and equal weight, with ablations. `python -m backtest.run` writes `docs/backtest/` (results, chart, JSON); the README has a Results section and `docs/adr/0002-own-backtest-replay.md` records the design.

### Changed

- New keeper key `cookieless_401_means_logged_out` (default true). Set it to false when the gateway answers 401 to a cookie-less request while logged in: a 401 then counts as unsure and the keeper never submits on it. `deploy/README.md` section 0B step 13 has the server check.
- The sentiment satellite is in `status: research` with `allocation_pct: 0`, because Alpha Vantage `NEWS_SENTIMENT` is now premium. 30% of equity stays unallocated. A research sleeve must have 0% allocation.
- The `sentiment` compose service moved to profile `research`, so `--profile auto` no longer polls it.
- The gateway uses its own TLS key (generated on first start into the `ibkr_gateway_tls` volume), takes `sslPwd` from a Docker secret and allows only `127.0.0.1` and `172.31.250.1`. IBKR's shared default keystore is no longer used.
- The gateway zip is fetched with `scripts/fetch_gateway.sh` and checked twice, instead of being downloaded during the image build.
- The keeper runs from its own image target (`keeper`, headless Chromium) with the login secret. The other services use the `app` target.
- `CPGWClient` verifies the gateway certificate when `CPGW_CA_FILE` is set.
- Compose no longer sets `CPGW_CA_FILE`. Set it in `.env` (`/app/gateway_tls/cert.pem` for the Docker gateway), so the keeper and the runner no longer crash-loop with the host Java gateway. `deploy/README.md` shows how to export the Java gateway certificate.
- `IbkrCredentials` hides the password in `repr`. The keeper banner shows only the first two characters of the username.
- `.env.example` no longer has `IBKR_USERNAME` or `IBKR_PASSWORD`. It adds `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IBKR_LOGIN_SECRET_FILE` and `CPGW_CA_FILE`.
- **Upgrade note:** move `IBKR_USERNAME` and `IBKR_PASSWORD` from `.env` into `secrets/ibkr_login` (chmod 600, owner uid 1000), create `secrets/gateway_ssl_password`, run `scripts/fetch_gateway.sh`, and replace `core/ibkr_auth_private.py` with `deploy/ibkr_auth_private.py.example`. Follow `deploy/README.md` section 0B.
- Known limit: workers started from the dashboard run inside the dashboard container, which reads `CPGW_CA_FILE` from `.env` but does not mount the gateway certificate volume, and has no login secret and no Chromium. A runner started there fails closed with `CPGWError` at start. Run the keeper and the runners as compose services.
- The Docker guide in `deploy/README.md` requires Docker Engine 28 or later.
- `DASHBOARD_HOST` defaults to `127.0.0.1`, so the dashboard is no longer reachable on the tailnet IP directly.
  **Upgrade note:** run `sudo tailscale serve --bg 8765` and open the `https://<machine>.<tailnet>.ts.net` URL instead.
- The runner no longer evaluates core or opens satellite positions on NYSE holidays. A new `HOLIDAY` market regime is treated like `WEEKEND`: satellite exits still run, entries and the core sleeve do not (#15).
- On early-close days (13:00 ET) `OPEN` ends at the real close and after-close logic starts there (#15).
- The satellite hold timer exit reason is now `hold timer: 10 trading days >= 10 max` (was `hold timer: 10d >= 10d max`). Anything that matches the old string in `targets.reason` or the events log must be updated. Open positions exit later than before when a weekend or holiday falls in their hold period, including positions opened before the upgrade (#7).
- Sleeve budgets follow the paper account value within 0.25x to 2x of `starting_equity`. Steps over 25% and failed reads reuse the last reading, and `use_account_equity: false` restores the old behaviour (#20).
- The core calendar trigger reason is now `calendar: Fri` instead of `calendar: Fri close`, because it fires on the first Friday cycle in Eastern time, not at the close (#5). Anything that matches the old string in `targets.reason` or the events log must be updated.
- The `PortfolioConfig.reentry_cooldown_sec` default is now 86400 (was 600), matching `config/symbols.yaml` (#6).
- The codebase is formatted with ruff. Imports are sorted and unused imports are removed (#4).
- `docs/TRADING_LOGIC.md` is rewritten to match the code: core sleeve (70%), sentiment satellite (20%) and the disabled tactical sleeve. It has a Known gaps section (#4).
- The documented install root is now `/opt/trading-bot` (the repo root, no `bot/` subfolder) in the deploy docs, all four systemd units and `deploy/backup.sh` (#2).
  **Upgrade note:** existing systemd installs at `/home/USER/trading-bot/bot` should either move the clone to `/opt/trading-bot` or edit the paths in each unit after copying it.
- systemd units use `ProtectSystem=strict`, so only `logs/` and `models/artifacts/` are writable. Run `mkdir -p logs` before the first start.
- systemd ordering (`After=`/`Wants=`) now names the templated units (`tradingbot-*@%i.service`).
- The `AlphaVantageProvider.lookback_hours` default is now 24 (was 6). It is now sent as `time_from`, so fewer articles arrive on quiet days and the satellite may skip more symbols on `min_article_count` (#10).

### Fixed

- The keeper counts a login attempt itself when the private `login()` never calls `on_submit`, so the 3 failure lockout and the 6 login cap still hold. It warns once per process.
- `live.runner` and `scripts.train_symbol` accept an empty cookie in the session file, like the portfolio runner.
- `--live` in `live.runner` and `live.portfolio_runner` is refused unless the session account id starts with the keeper paper prefix (`DU` by default). Dry runs are unchanged.
- The keeper logs in after an explicit 401 from its pre-login check, waits on other unclear answers, and writes the session file for a session it adopts.
- A login setup error locks out once with a setup alert, an empty account list is retried instead of locking out, and `--reset-lockout` clears the alert cooldowns.
- The portfolio runner follows the session file to an empty cookie after a keeper re-login, instead of keeping the stale one.
- `CPGWError` no longer chains the unmasked httpx or JSON error, so account ids and URLs stay masked.
- The satellite hold timer counts NYSE trading days instead of calendar days, so `hold_max_days` matches its meaning (#7).
- US Eastern time uses the `America/New_York` zone, so the market state is correct on the DST change days. Holidays and early closes are known (#15).
- `size_portfolio` no longer returns no decisions for a generator input when the gross cap applies (#9).
- `AlphaVantageProvider.lookback_hours` is sent to the API as `time_from` instead of being ignored (#10).
- `MacroSnapshot.as_of` is timezone-aware UTC instead of the deprecated `datetime.utcnow()` (#11).
- The README "Run it" section uses uv instead of `python -m venv` and pip (#13).
- The core weekday and once-per-day checks use the US Eastern date instead of UTC, so the weekly rebalance runs on the first Friday cycle in Eastern time and no longer on Thursday evening (#5).
- `portfolio.reentry_cooldown_sec` is enforced for the core and sentiment sleeves: a symbol a sleeve sold to 0 shares is not bought back by that sleeve for 24 hours. Top-ups and sells are never blocked (#6).
- Satellite skip reasons have no leading space (#16).
- The satellite no longer re-enters a symbol in the cycle it exits it (#17).
- The core drift trigger respects the once-per-day rule, so the core sleeve rebalances at most once per US Eastern day (#18).
- Docs and code now agree on one dashboard port (8765) and one broker port (5000, Client Portal Gateway). The README and the `dashboard/app.py` docstring no longer say 8080 or 8000, and the `assert_paper_mode` error no longer mentions the old 4002/7497 TWS ports (#2).
- Remaining `bot/.env` references in code comments, error messages and docs now point at `.env` in the repo root.

### Removed

- `core.market_hours.is_core_rebalance_window` (it had no effect) (#5).

### Security

- The keeper logs in at most 6 times per 24 h (`keeper.max_logins_per_window`) even when each login works, and checks the gateway without a cookie right before every submit.
- No secret in logs, events, exceptions or Telegram messages: a redactor masks the password, username, Telegram token and cookies, and login errors are fixed strings.

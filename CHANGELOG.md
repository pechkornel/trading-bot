# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `tests/test_config.py`: regression tests for the paper-mode lock and its error message.
- `requirements-dev.txt` with pytest.
- `pyproject.toml` with ruff and pytest settings. `core/ibkr_auth.py` is excluded from ruff (#4).
- `.gitattributes` that forces LF line endings.
- ruff 0.17.4, pinned in `requirements-dev.txt`.
- Offline test suite (123 tests) for scoring, the core and sentiment satellite sleeves, signal fusion, position sizing, Alpha Vantage sentiment parsing (hand-built fixture) and the paper lock. A `conftest.py` blocks network access and isolates `.env` (#4).
- GitHub Actions CI on Python 3.11 and 3.12. It runs on push to main, on pull requests, weekly and on manual dispatch. The README has a status badge. Actions are pinned to commit SHAs (#4).
- `.github/dependabot.yml`: weekly updates for the GitHub Actions used in CI (#4).

### Changed

- The codebase is formatted with ruff. Imports are sorted and unused imports are removed (#4).
- `docs/TRADING_LOGIC.md` is rewritten to match the code: core sleeve (70%), sentiment satellite (20%) and the disabled tactical sleeve. It has a Known gaps section (#4).
- The documented install root is now `/opt/trading-bot` (the repo root, no `bot/` subfolder) in the deploy docs, all four systemd units and `deploy/backup.sh` (#2).
  **Upgrade note:** existing systemd installs at `/home/USER/trading-bot/bot` should either move the clone to `/opt/trading-bot` or edit the paths in each unit after copying it.
- systemd units use `ProtectSystem=strict`, so only `logs/` and `models/artifacts/` are writable. Run `mkdir -p logs` before the first start.
- systemd ordering (`After=`/`Wants=`) now names the templated units (`tradingbot-*@%i.service`).

### Fixed

- Docs and code now agree on one dashboard port (8765) and one broker port (5000, Client Portal Gateway). The README and the `dashboard/app.py` docstring no longer say 8080 or 8000, and the `assert_paper_mode` error no longer mentions the old 4002/7497 TWS ports (#2).
- Remaining `bot/.env` references in code comments, error messages and docs now point at `.env` in the repo root.

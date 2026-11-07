# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `tests/test_config.py`: regression tests for the paper-mode lock and its error message.
- `requirements-dev.txt` with pytest.

### Changed

- The documented install root is now `/opt/trading-bot` (the repo root, no `bot/` subfolder) in the deploy docs, all four systemd units and `deploy/backup.sh` (#2).
  **Upgrade note:** existing systemd installs at `/home/USER/trading-bot/bot` should either move the clone to `/opt/trading-bot` or edit the paths in each unit after copying it.
- systemd units use `ProtectSystem=strict`, so only `logs/` and `models/artifacts/` are writable. Run `mkdir -p logs` before the first start.
- systemd ordering (`After=`/`Wants=`) now names the templated units (`tradingbot-*@%i.service`).

### Fixed

- Docs and code now agree on one dashboard port (8765) and one broker port (5000, Client Portal Gateway). The README and the `dashboard/app.py` docstring no longer say 8080 or 8000, and the `assert_paper_mode` error no longer mentions the old 4002/7497 TWS ports (#2).
- Remaining `bot/.env` references in code comments, error messages and docs now point at `.env` in the repo root.

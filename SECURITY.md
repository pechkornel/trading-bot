# Security notes

- **Paper trading only.** The bot refuses to start unless `PAPER=true` and the gateway port is a
  paper port. The check runs in `core.config.assert_paper_mode()` before any broker connection,
  in both the trainer and the runners.
- **Dry run by default.** The runners log their decisions without sending orders unless
  `--live` is passed, and even then only to the paper account.
- **Secrets stay in `.env`.** Copy `.env.example` to `.env`, keep it out of version control and
  readable only by the bot user (`chmod 600 .env`).
- **Broker login.** The automated login module is not included in this public version, see
  `core/ibkr_auth.py`.

# Security notes

- **Paper trading only.** The bot refuses to start unless `PAPER=true` and the gateway port is a
  paper port. The check runs in `core.config.assert_paper_mode()` before any broker connection,
  in both the trainer and the runners.
- **Dry run by default.** The runners log their decisions without sending orders unless
  `--live` is passed, and even then only to the paper account.
- **Secrets stay in `.env`.** Copy `.env.example` to `.env`, keep it out of version control and
  readable only by the bot user (`chmod 600 .env`).
- **Broker login.** The automated login module is not included in this public version, see
  `core/ibkr_auth.py`. If you have your own, put it at `core/ibkr_auth_private.py`. That path
  is git-ignored. Never commit it and never `git add -f` it. Check with
  `git check-ignore -v core/ibkr_auth_private.py` (it must print the rule) and keep
  `git status --short` clean. The loader prints and logs nothing, and the keeper never prints
  the password.
- **Private module in the Docker image.** The local image `tradingbot:local` contains the
  private module (it is copied in with the rest of the source). Build the image on the
  machine that runs it. Never push it to a registry and never `docker save` it.
- **Loopback binds.** The dashboard has no authentication, so it binds to `127.0.0.1`
  (`DASHBOARD_HOST`, default `127.0.0.1`). The gateway port 5000 is published on `127.0.0.1`
  only. Docker bypasses host firewalls for published ports, so the bind address is the
  control, not a firewall rule. Do not change either to `0.0.0.0` or `5000:5000`.
- **Remote access.** Use `sudo tailscale serve --bg 8765` for tailnet-only HTTPS. Never run
  `tailscale funnel`, which publishes the dashboard to the internet. `tailscale funnel status`
  must show nothing.
- **Third-party gateway binary.** The optional `ibkr_gateway` container runs a native build of
  the IBKR Client Portal Gateway from a personal GitHub repository. That repository has no
  license, no checksum file and no build attestations. The release tag points at a commit
  that is no longer on its `main` branch. The binary handles the IBKR login session. The
  Dockerfile pins release `v1.0.0` and its SHA-256, so a changed asset fails the build, but
  the digest is the one GitHub computed at upload, not one the author published. The
  container runs as a non-root user with `cap_drop: [ALL]` and `no-new-privileges` on its own
  bridge network. It can still reach the internet and it sees the session. Only the paper
  account should ever be used with it. If you do not accept this risk, use IBKR's Java
  gateway instead.
- **Gateway config.** The container reads `docker/gateway/conf.yaml` through `--conf`. Whether
  the native binary honours `--conf` is unverified. If it ignores it, the baked default does
  not allow the bridge address and requests are denied, so nothing is exposed. A result of
  "Access Denied" from `curl -sk https://127.0.0.1:5000/` means exactly that.
- **CPU requirement.** The `v1.0.0` binary may need a CPU with AVX2. On an older CPU it exits
  with an illegal instruction error.
- **Session cookie in backups.** `logs/cpgw_session.json` holds the session cookie and is
  included in the nightly backup. Protect the backup directory.

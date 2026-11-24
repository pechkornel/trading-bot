# Security notes

- **Paper trading only.** The bot refuses to start unless `PAPER=true` and the gateway port is a
  paper port. The check runs in `core.config.assert_paper_mode()` before any broker connection,
  in both the trainer and the runners.
- **Dry run by default.** The runners log their decisions without sending orders unless
  `--live` is passed, and even then only to the paper account.
- **Where secrets live.** `.env` (copy of `.env.example`, `chmod 600`, git-ignored) holds
  settings and the Telegram token. It is loaded into and mounted in every container, so it
  holds no broker secret. The IBKR login is the file `secrets/ibkr_login` (`IBKR_USERNAME` and
  `IBKR_PASSWORD` lines), and the gateway key password is `secrets/gateway_ssl_password`. Both
  are git-ignored and docker-ignored. They reach containers as Compose file secrets, only
  `cpgw_keeper` gets the login and only `ibkr_gateway` gets the key password.
- **Secret file checks.** `core/secret_store.py` refuses the login file when it is missing, a
  symlink, not a regular file, larger than 4 KiB, readable by group or other, or owned by
  another uid than the reader. The keeper container runs as uid 1000, so the host file must
  be `1000:1000` and mode 600. Error messages name the file, line and key, never a value.
  The keeper refuses to start while `IBKR_PASSWORD` is set in its environment or in `.env`.
  It checks after `.env` is read.
- **Broker login.** The keeper logs in headlessly through the gateway's own login page
  (`core/gateway_login.py`, tracked and tested). The git-ignored `core/ibkr_auth_private.py`
  is only wiring (template: `deploy/ibkr_auth_private.py.example`) and holds no secret.
  Never commit it and never `git add -f` it. Check with `git check-ignore -v` (it must print
  the rule) and keep `git status --short` clean. CI fails when that file, anything under
  `secrets/` (except the README and `*.example`) or anything under `vendor/` is tracked.
- **Browser TLS.** The login uses headless Chromium (Playwright's headless shell, no X server).
  HTTPS checks are never turned off. The browser is started with
  `--ignore-certificate-errors-spki-list` for exactly one key, the gateway's own certificate
  named by `CPGW_CA_FILE`. Without that file the login refuses to run. `CPGWClient` verifies
  the same certificate, and refuses a gateway host that is not loopback when no
  `CPGW_CA_FILE` is set (loopback keeps the unverified fallback). It only requests the
  local gateway address and aborts every other request. It records no video, trace or
  screenshot. The browser closes after each login. Whether this headless build honours the
  pin flag is confirmed on the server (`deploy/README.md` section 0B step 12). If it does
  not, navigation fails and nothing is sent.
- **Chromium sandbox.** Off on purpose, and set explicitly (`chromium_sandbox=False`, pinned
  by a test). Playwright's Python default is also off, which adds `--no-sandbox`. The sandbox
  needs unprivileged user namespaces. Docker's default seccomp profile blocks them, and on
  Ubuntu 24.04 an AppArmor setting can block them on the host too. Enabling them would need a
  custom seccomp profile and host changes that nothing here can test (no Docker or real
  browser runs in the test suite), and a wrong guess would break the login. So this is a known
  gap, not a fixed one: a browser exploit would run without the Chromium sandbox, inside the
  container. Remaining mitigations: the browser only requests the local gateway origin (all
  other requests are aborted, service workers and downloads are blocked), it trusts exactly
  one certificate key (SPKI pin), it is short lived (closed after each login), the keeper runs
  as non-root uid 1000 with `cap_drop: ALL`, `no-new-privileges` and a memory limit, and it
  holds only a paper login secret. Weaker points: the keeper uses the host network namespace
  and can read the secret, `.env` and the session file. Follow-up: try a keeper-only seccomp
  profile on the server and enable the sandbox once it is tested there.
- **Redaction.** Every keeper event, every exception text that leaves the login code and every
  Telegram message goes through a redactor. It masks the password, the username, the Telegram
  token and `bot<id>:<token>` URL patterns, and cookie headers and pairs. Login error texts
  are fixed strings and page errors are never chained, so a filled value cannot travel in an
  exception. The keeper banner shows only the first two characters of the username.
  `IbkrCredentials` hides the password in `repr`.
- **Lockout.** The keeper never logs in while the gateway reports an authenticated session or
  is down, and not inside the nightly gap or the Saturday maintenance window. Unexpected drops
  retry with backoff (60 s up to 30 minutes). A wrong password, a second-factor prompt, an
  unknown login page state, a non-paper account or a login setup error locks it out at once.
  A setup error (a missing or invalid CA file, a gateway URL that is not local, or Playwright
  not installed) is refused before any form is submitted. Its lockout reason starts with
  `setup:`. Three submitted logins in 24 hours with no good login in between lock it out too,
  and so do six submitted logins in 24 hours even when each one worked
  (`keeper.max_logins_per_window`), which catches a session that drops right after every login.
  A session shorter than 10 minutes (`keeper.min_session_sec`) makes the next drop back off.
  Right before any submit the keeper checks the gateway a second time with no cookie, so a
  lost session file cannot hide a live session. Only an explicit 401 or `authenticated: false`
  allows the login. Any other answer (a network error, a 5xx or an unexpected body) means no
  login. The keeper waits, and after 5 such checks in a row it logs one error and sends one
  alert. The lockout is saved in `logs/keeper_state.json` (mode 600, no secret), so a restart
  cannot bypass it, and a corrupt state file counts as locked out. It clears only with
  `python -m live.cpgw_keeper --reset-lockout`.
- **Paper account guard.** After every login, and when it adopts a running session, the keeper
  lists the accounts. It asks up to 3 times, 2 s apart. Every account id must start with `DU`
  (`keeper.paper_account_prefix`). An account id without that prefix means the keeper logs
  out, locks itself and alerts. An empty or failed account list is not a lockout: the keeper
  keeps the session, sends one warning alert at most once an hour, and checks again on later
  ticks. This is a second check next to `core.config.assert_paper_mode()`, which stays the
  first one.
- **Telegram.** Alerts go through `api.telegram.org` with `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` from `.env`. Messages are plain text, redacted, truncated, deduplicated
  and capped at 20 per hour per process. Each lockout alert has its own key (`lockout:<time>`),
  so a later lockout is always sent. `--reset-lockout` clears the saved cooldowns of the
  `lockout`, `not_paper` and `state_corrupt` keys, so the next alert of each kind goes out at
  once. Only short summaries leave the server. The token is
  visible to every container because `.env` is. A token file variant is a follow-up. Treat the
  chat as a private channel and revoke the token with BotFather if it leaks.
- **Private module in the Docker image.** The local images (`tradingbot:local`,
  `tradingbot-keeper:local`) contain the private wiring module. It holds no secret any more,
  and `secrets/` is not copied into any image. Still, build on the machine that runs them and
  never push them to a registry.
- **Loopback binds.** The dashboard has no authentication, so it binds to `127.0.0.1`
  (`DASHBOARD_HOST`, default `127.0.0.1`). The gateway port 5000 is published on `127.0.0.1`
  only. Docker bypasses host firewalls for published ports, so the bind address is the
  control, not a firewall rule. Do not change either to `0.0.0.0` or `5000:5000`.
  With the systemd units (not Docker), never leave `DASHBOARD_HOST` empty in `.env`: systemd
  then passes an empty host and uvicorn listens on all interfaces. Set it to `127.0.0.1` or
  remove the line. Docker needs Engine 28 or later, because older engines let hosts on the
  same LAN reach ports published on `127.0.0.1`.
- **Remote access.** Use `sudo tailscale serve --bg 8765` for tailnet-only HTTPS. Never run
  `tailscale funnel`, which publishes the dashboard to the internet. `tailscale funnel status`
  must show nothing.
- **Third-party gateway binary.** The optional `ibkr_gateway` container runs a native build of
  the IBKR Client Portal Gateway from a personal GitHub repository. That repository has no
  license, no checksum file and no build attestations. The release tag points at a commit
  that is no longer on its `main` branch. The binary handles the IBKR login session.
  `scripts/fetch_gateway.sh` downloads release `v1.0.0` into the git-ignored `vendor/`
  folder and checks it against `docker/gateway/gateway.sha256`. The image build checks the
  same hash again before it unpacks the zip. The digest is the one GitHub computed at upload,
  not one the author published, so this detects a changed asset, not a bad original. You can
  inspect the file before building. The container runs as a non-root user with
  `cap_drop: [ALL]` and `no-new-privileges` on its own bridge network. It can still reach the
  internet and it sees the session. Only the paper account should ever be used with it. If you
  do not accept this risk, use IBKR's Java gateway instead.
- **Gateway keystore.** IBKR's default `vertx.jks` is public and is deleted from the image.
  The entrypoint generates the gateway's own RSA 3072 key and a PKCS12 keystore on first
  start and stores it, with `cert.pem`, in the `ibkr_gateway_tls` volume. The key password is
  the `gateway_ssl_password` secret (letters and digits, 32 to 128 characters). It is passed
  to openssl as files, never on a command line, and rendered into a tmpfs config file (mode
  600) that is gone when the container stops. The keystore is a PKCS12 file named `vertx.jks`.
  Whether the native gateway accepts that is confirmed on the server. If not, TLS does not
  start and nothing is exposed. A real JKS from `keytool` is the follow-up. The entrypoint
  never regenerates the key when the password does not match.
- **Gateway config and allow list.** The entrypoint renders `docker/gateway/conf.yaml` and
  starts the gateway with `--conf ../run/conf.yaml`. The allow list is exactly `127.0.0.1`
  (the container healthcheck) and `172.31.250.1` (Docker's proxy address on the bridge).
  Whether the native binary honours `--conf` and the relative keystore path is confirmed on
  the server. If it ignores them, requests are denied, so nothing is exposed. "Access Denied"
  from `curl -sk https://127.0.0.1:5000/` means exactly that. CORS stays IBKR's default
  (`origin.allowed: "*"`).
- **Gateway certificate checks.** When `CPGW_CA_FILE` is set (set in `.env` for the
  Docker gateway, never hard-coded in compose), `CPGWClient` verifies the gateway certificate against that file
  with host name checking. A set but missing file is an error. If unset (host Java gateway),
  verification stays off for the loopback address.
- **CPU requirement.** The `v1.0.0` binary may need a CPU with AVX2. On an older CPU it exits
  with an illegal instruction error.
- **Session cookie in backups.** `logs/cpgw_session.json` can hold the session cookie (it may
  be empty, because the gateway holds the session itself) and is included in the nightly
  backup. Protect the backup directory.
- **Cookie-less 401 and the login gate.** Right before a login the keeper asks the gateway for
  the auth status with no cookie. A 401 is read as "nobody is logged in" only when
  `keeper.cookieless_401_means_logged_out` is true (the default). That is safe only if the
  gateway session is gateway-wide. Check it once on the server before the first automatic
  login (`deploy/README.md` section 0B step 13). If the gateway answers 401 while you are
  logged in, set the key to false: a 401 is then "unsure" and the keeper never submits on it.
- **Dashboard-spawned workers.** The dashboard can start workers inside its own container.
  That container gets `CPGW_CA_FILE` from `.env` but does not mount the `ibkr_gateway_tls`
  volume, and it has no login secret and no Chromium. A keeper started there cannot log in,
  and a runner started there raises `CPGWError` at start because the certificate file is
  missing (it fails closed, it never skips verification). Run
  the workers as compose services. This is a known limit, not changed in Phase 1.
- **Sentiment satellite.** It is off (`status: research`, 0% allocation) and the `sentiment`
  compose service is in profile `research`. No simulated positions are managed.

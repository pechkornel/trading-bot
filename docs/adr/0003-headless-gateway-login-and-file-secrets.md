# 0003. Headless gateway login and file secrets

## Status

Accepted.

## Context

The bot runs unattended on a headless home server and trades a paper account only. The
gateway drops its session at the nightly reset, on Saturday maintenance and after 24 hours,
so someone has to log in again, and nobody is there. The public repo had no working login.

Four problems stood in the way:

- The login needs a real browser flow on the gateway's own login page, and it must not run
  without a display.
- The gateway shipped with IBKR's default keystore. The key is public, so the password that
  protects it protects nothing. Any local process on port 5000 could pretend to be the
  gateway and read the password the browser types.
- The IBKR username and password sat in `.env`, which is loaded into and mounted in every
  container.
- A login that goes wrong can lock the IBKR account. Retrying blindly makes it worse. And
  CI cannot see a git-ignored private file, so none of the secret handling would be tested.

## Decision

- **Generic flow is tracked, wiring is private.** The login flow is `core/gateway_login.py`,
  behind a small `LoginPage` protocol, with offline tests. The git-ignored
  `core/ibkr_auth_private.py` is a few lines of wiring (template:
  `deploy/ibkr_auth_private.py.example`) and can override the page selectors and markers.
  The auth error types moved to `core/auth_types.py` to avoid a circular import. CI fails
  when the private file, `secrets/` files or `vendor/` are tracked.
- **Headless Chromium, TLS kept on.** The keeper image has Playwright 1.64.1 and only
  Chromium's headless shell, so no X server is needed. The gateway gets its own key. The
  browser accepts exactly that key through an SPKI pin computed from the certificate in
  `CPGW_CA_FILE`. HTTPS errors are never ignored. Without the certificate file the login
  refuses to run. The browser only reaches the gateway origin. Success is decided by the
  gateway API (`/iserver/auth/status`), not by page text, and an empty cookie is accepted.
- **File secrets.** The login is `secrets/ibkr_login` (mode 600, owner must be the reading
  uid, checked by `core/secret_store.py`) and the gateway key password is
  `secrets/gateway_ssl_password`. Both are Compose file secrets, each mounted into one
  service. The keeper refuses to start while `IBKR_PASSWORD` is set in its environment or
  in `.env` (checked after `.env` is loaded).
  A redactor masks secrets and tokens in every event, exception text and Telegram message.
- **Gateway container.** The pinned release zip is fetched by `scripts/fetch_gateway.sh`
  into `vendor/` and checked against one hash file, again in the image build. IBKR's default
  keystore is deleted. The entrypoint generates the gateway's own key and a PKCS12 keystore
  into the `ibkr_gateway_tls` volume, renders the config into a tmpfs with the password from
  the secret, and starts the gateway. The allow list is exactly `127.0.0.1` and
  `172.31.250.1`.
- **Session policy and lockout.** `live/keeper_policy.py` is pure. The keeper never logs in
  while authenticated or while the gateway is down, and not inside the nightly gap or the
  Saturday window. Right before a submit it checks the gateway again without the stored
  cookie: only a 401 or `authenticated: false` allows the login; any other answer waits, with
  one alert after 5 unclear checks. Backoff is 60 s up to 30 minutes. A wrong password, a
  second factor, an unknown page state, a non-paper account or a login setup error (CA file
  missing or invalid, Playwright missing, non-local URL; reason `setup:`, no form submitted)
  locks out at once. Three failed or six total submitted logins in 24 hours lock out too. The
  lockout is persisted and cleared only by `--reset-lockout`. After every login, the accounts
  are checked up to 3 times: an account id without the `DU` prefix makes the keeper log out
  and lock itself; errors or an empty list do not log out, they alert and retry later.
- **Telegram** carries the alerts. Gmail stays for the single-symbol runner only.
- **Sentiment sleeve off.** Alpha Vantage `NEWS_SENTIMENT` is premium, so the satellite is in
  `status: research` with 0% allocation, the service is in profile `research`, and a
  `--check` flag fails loudly.

## Consequences

- The keeper image is a few hundred MB bigger (browser and system libraries). The other
  services use the unchanged `app` target.
- A manual vendor step (`scripts/fetch_gateway.sh`) before building the gateway.
- Five things are only confirmed on the server, each fails closed and has a fallback in
  `deploy/README.md` section 0B: the login page selectors and markers, that Chromium honours
  the SPKI pin flag, that the gateway accepts a PKCS12 keystore named `vertx.jks`, that
  `--conf ../run/conf.yaml` and the relative keystore path resolve, and that Docker's proxy
  address is `172.31.250.1`.
- A lockout needs a person: fix the cause, then `--reset-lockout`. That is intended.
- Supply chain: Chromium is downloaded at image build time, pinned only through the
  `playwright` version, not by file hash. The gateway binary risk from ADR 0001 is
  unchanged. Vendoring adds no new trust, it only lets you inspect the file.
- The Chromium sandbox is off on purpose and set explicitly (`chromium_sandbox=False`, pinned
  by a test), not left to Playwright's default (also off, adding `--no-sandbox`). The sandbox
  needs unprivileged user namespaces. Docker's default seccomp profile blocks `clone` and
  `unshare` with `CLONE_NEWUSER`, and Ubuntu 24.04 can also restrict them on the host
  (`kernel.apparmor_restrict_unprivileged_userns`). The setuid helper is not an option with
  `cap_drop: ALL` and `no-new-privileges`. Enabling it needs a keeper-only seccomp profile and
  host sysctl changes that cannot be tested here (nothing in the suite runs Docker or a real
  browser), and failure would break the login. Remaining mitigations: only the local gateway
  origin is requested (route blocking, no service workers, no downloads), the SPKI pin,
  `cap_drop: ALL`, `no-new-privileges`, non-root uid 1000, the memory limit and a browser that
  closes after every login. The keeper still shares the host network namespace and can read the
  secret, `.env` and the session file. Follow-up: test a keeper-only seccomp profile on the
  server, then enable the sandbox with a fail-closed launch and an explicit opt-out flag.
  Playwright is pinned to 1.64.1; the earlier 1.55.0 was about 14 months old.
- The Telegram token is in `.env` and visible to every container. The cookie file in `logs/`
  is in the nightly backup.
- The dashboard can spawn workers inside its own container, which has no certificate file,
  no login secret and no Chromium. Such a keeper cannot log in. Documented as a known limit.
- Each service has a memory limit equal to its swap limit. The numbers are checked against
  `docker stats` after the first day.

## Alternatives

- **All login code only in the private file, tests skipped when it is absent.** CI would
  never test the secret handling, the second-factor detection or the backoff.
- **`ignore_https_errors` in Playwright.** It accepts any certificate, so any local process
  on port 5000 could collect the password.
- **Keep IBKR's `vertx.jks` and only change `sslPwd`.** The key is public, so the password
  protects nothing.
- **Generate the keystore with `keytool` from a JRE image.** It adds a second unpinned image
  and a manual step. It stays the fallback if the gateway rejects the PKCS12 file.
- **The official `mcr.microsoft.com/playwright/python` image.** About 1.5 GB or more with all
  browsers, too big.
- **Xvfb with headed Chromium.** Ruled out. The headless shell needs no display.
- **Credentials in `.env`.** They would be loaded into and mounted in every container.
- **A third-party Telegram library.** `httpx` already does it in a few lines and is easy to
  mock.
- **One Telegram state file shared by all processes.** Concurrent writes would race.
- **Proactive logout and login before 24 hours.** It adds a login a day and risks a lockout
  for no gain.
- **Run the login inside the gateway container.** It would need Python and Chromium inside
  the hardened gateway image.

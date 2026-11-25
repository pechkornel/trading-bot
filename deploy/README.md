# Ubuntu home-server deployment

End-to-end walkthrough for running the bot 24/7 on a Ubuntu box.

Two supported paths:
- **Docker compose** (recommended - isolated, reproducible)
- **systemd** (lighter, no Docker layer)

Pick one. Both end with a dashboard you reach via Tailscale.

---

## 0. Prerequisites

- Ubuntu 22.04 LTS or newer
- IB account in **paper mode** (live trading is blocked at the application layer - see `core.config.assert_paper_mode`)
- **Client Portal Gateway (CPGW)** running on the server - this is IBKR's Java
  daemon at `https://localhost:5000`. Download from
  https://www.interactivebrokers.com/en/trading/ib-api.php#clientportal (filename
  `clientportal.gw.zip`). Unzip and run with:
  ```bash
  ./bin/run.sh root/conf.yaml &
  ```
  No GUI required, just a JVM. Once it's up, `curl -k https://localhost:5000/`
  should return the login page HTML.
- The `cpgw_keeper` service logs in automatically through the gateway's own login
  page, with headless Chromium (Playwright), so you never need to open the CPGW web UI.
  Credentials live in the file `secrets/ibkr_login` (mode 600), not in `.env`. See
  section 0B. The login code itself is not part of the repo: you wire it in with
  `core/ibkr_auth_private.py`, copied from `deploy/ibkr_auth_private.py.example`.

### Java gateway: export its certificate once

Skip this when you use the Docker gateway (`--profile gateway`): it writes its own certificate and you set `CPGW_CA_FILE=/app/gateway_tls/cert.pem` in `.env` (sections 0A and 0B). `docker-compose.yml` does not set `CPGW_CA_FILE` itself, so the keeper and the portfolio runner start with the host Java gateway too.

The keeper's login pins the gateway certificate in the browser and refuses to run without a CA file. With the host Java gateway, export its certificate once. Server, with the gateway running:

```bash
openssl s_client -connect 127.0.0.1:5000 -servername localhost </dev/null 2>/dev/null | openssl x509 -outform PEM > config/gateway_cert.pem
chmod 644 config/gateway_cert.pem
openssl x509 -in config/gateway_cert.pem -noout -subject -enddate
openssl x509 -in config/gateway_cert.pem -noout -ext subjectAltName
```

- The last command must list `DNS:localhost` and `IP Address:127.0.0.1`. IBKR's default Java gateway certificate may not cover them. If the list is empty or lacks both names, the pinned check can fail on the host name. Then either generate your own certificate for the Java gateway (as `docker/gateway/entrypoint.sh` does, with `subjectAltName=DNS:localhost,IP:127.0.0.1`) or use the Docker gateway.
- `*.pem` files are ignored by version control. This one is a public certificate, not a key, but still never commit it.
- Docker: `./config` is mounted read-only at `/app/config`, so set `CPGW_CA_FILE=/app/config/gateway_cert.pem` in `.env`. systemd: set `CPGW_CA_FILE=/opt/trading-bot/config/gateway_cert.pem` in `.env`.
- Export it again after the Java gateway regenerates its certificate.

> If you used Phase 5 with `ibapi` + IB Gateway (Java desktop with GUI login):
> the new flow replaces that entirely. You no longer need to log in via VNC daily.
> The Java CPGW process replaces the IB Gateway process, and `cpgw_keeper`
> handles the auth.

---

## 0A. Home server (Docker)

A fresh install on an Ubuntu box with Docker, reached over Tailscale, deployed by hand over SSH. It runs the gateway container, the dashboard, the keeper and the portfolio runner (dry run). The sentiment worker is off by default (compose profile `research`, see section 0B step 16).

Already running an older version of this stack? Use section 0B (Phase 1 upgrade) instead of starting here.

Notes before you start:

- This section uses `~/trading-bot`. The systemd path and `UBUNTU_SETUP.md` use `/opt/trading-bot`. Both work: compose uses relative paths and `deploy/backup.sh` finds its own directory.
- It changes no firewall rule and does not touch SSH. Docker bypasses ufw for published ports, so the gateway (5000) and the dashboard (8765) are bound to `127.0.0.1` only.
- Every command below says where it runs. "Server" means over SSH on the Ubuntu box. "Laptop" means your own machine.
- The portfolio stays in dry run. Adding `--live` to send paper orders is a separate, later decision and is not part of this guide.
- Use the Docker gateway or the Java gateway from section 0, not both. Both want port 5000.

### 1. Preflight (server)

```bash
uname -m                                  # expect x86_64
grep -c -w avx2 /proc/cpuinfo             # expect a number above 0
docker version --format '{{.Server.Version}}'   # must be 28 or newer
systemctl is-enabled docker               # expect enabled
tailscale status
id -u                                     # expect 1000
ss -Hltn '( sport = :5000 )'              # expect no output
```

- Docker Engine 28 or later is required. Older engines let other hosts on the same LAN reach ports published on `127.0.0.1`. If the version is older, upgrade Docker from Docker's apt repo before you continue. Do not start the gateway until the check prints 28 or higher.
- If `avx2` prints `0`, stop. The pinned gateway binary needs AVX2. Use the Java gateway from `UBUNTU_SETUP.md` section 3 instead.
- If something listens on port 5000 (for example the Java `cpgw` service), stop it first: `sudo systemctl disable --now cpgw`.
- If `id -u` is not 1000, the `logs/` folder needs `chown` to your uid (see Troubleshooting).

### 2. Clone (server)

```bash
git clone https://github.com/pechkornel/trading-bot.git ~/trading-bot
cd ~/trading-bot
mkdir -p logs
```

### 3. Environment file (server)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set these values:

```
PAPER=true
DRY_RUN=true
IB_PORT=5000
DASHBOARD_HOST=127.0.0.1
CPGW_CA_FILE=/app/gateway_tls/cert.pem
SENTIMENT_PROVIDER=stub
```

- The IBKR login is not in `.env`. It goes in the file `secrets/ibkr_login` (step 4).
- `CPGW_CA_FILE` is correct for the Docker gateway used in this section. With the host Java gateway, see "Java gateway: export its certificate once" in section 0.
- Do not put `IBKR_USERNAME` or `IBKR_PASSWORD` in `.env`. The keeper refuses to start while `IBKR_PASSWORD` is set in its environment or in `.env`. The login goes in `secrets/ibkr_login` (step 4).
- `SENTIMENT_PROVIDER` is `stub` (no key) or `alpha_vantage`. For `alpha_vantage`, also set `ALPHA_VANTAGE_API_KEY`. The sentiment sleeve is off either way (allocation 0%).
- Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for alerts (create the bot with BotFather on your phone). Fill the `EMAIL_*` values only if you use the single-symbol runner, which still sends Gmail.
- `.env` is git-ignored. Never commit it. It is loaded into every container, so it holds no broker secret.

### 4. Secrets and private login module (server)

The public repo has a stub login. The login flow itself is the tracked module `core/gateway_login.py`. The private file `core/ibkr_auth_private.py` is only a few lines of wiring. It stays on your machines and is never committed. The login and the gateway key password are files under `secrets/`, never in `.env`.

```bash
cd ~/trading-bot
mkdir -p secrets && chmod 700 secrets
umask 077
cp secrets/ibkr_login.example secrets/ibkr_login
nano secrets/ibkr_login                         # paper IBKR_USERNAME and IBKR_PASSWORD lines
chmod 600 secrets/ibkr_login
openssl rand -hex 24 > secrets/gateway_ssl_password
sudo chown 10001:10001 secrets/gateway_ssl_password
sudo chmod 600 secrets/gateway_ssl_password
cp deploy/ibkr_auth_private.py.example core/ibkr_auth_private.py
chmod 600 core/ibkr_auth_private.py
git check-ignore -v core/ibkr_auth_private.py secrets/ibkr_login secrets/gateway_ssl_password   # must print the rules
git status --short                              # must print nothing
```

- `secrets/ibkr_login` must be owned by uid 1000 and mode 600. The keeper container runs as uid 1000 and refuses a file with another owner or any group or other permission.
- `secrets/gateway_ssl_password` is for the gateway container (uid 10001). Details and checks are in section 0B.
- Never run `git add -f` on any of these files.
- The Docker build copies the private module into the local images. It holds no secret any more. Still, never push those images to a registry.

### 5. Gateway (server)

The gateway image is built from a pinned zip that you fetch first (the Dockerfile downloads nothing). It also needs `secrets/gateway_ssl_password` from step 4.

```bash
cd ~/trading-bot
test -s secrets/gateway_ssl_password || echo "missing: create it as in step 4"
scripts/fetch_gateway.sh
git status --short                        # must print nothing, vendor/ is git-ignored
```

Look for a line starting with `OK:`. If it prints `SHA-256 mismatch`, the script deleted the download: stop and do not build. If the build later says the `vendor` context is missing, this fetch did not run.

Then build and start the gateway:

```bash
docker compose --profile gateway up -d --build ibkr_gateway
docker compose ps ibkr_gateway            # wait until it shows healthy
ss -Hltn '( sport = :5000 )'              # expect only 127.0.0.1:5000
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:5000/
```

- Any HTTP code (for example 200 or 302) means the gateway answers. Then check the body: `curl -sk https://127.0.0.1:5000/ | head -c 300`.
- If the body text is "Access Denied", the gateway refused the source address. The native gateway always uses its built-in allow list (`192.*`, `131.216.*`, `127.0.0.1`, see ADR 0004), and Docker's proxy should arrive from the bridge address `192.168.250.1`. The status code alone does not show this, so read the body. Run `docker compose --profile gateway down` and stop. Nothing is exposed. Report it before going on.
- `000` or "Failed to connect" means nothing answers on 127.0.0.1:5000. Check `docker compose ps ibkr_gateway` and the logs.
- Mandatory check from a second machine. Laptop, on the same LAN or tailnet:

```bash
curl -sk --max-time 5 https://<server-lan-ip>:5000/
curl -sk --max-time 5 https://<server-tailnet-ip>:5000/
```

Both must fail to connect. If either one connects, run `docker compose --profile gateway down` on the server and stop. Do not go on until you know why.

### 6. Dashboard (server)

```bash
docker compose up -d --build dashboard
curl -fsS http://127.0.0.1:8765/api/health   # expect "paper":true, "dry_run":true, "ib_port":5000
ss -Hltn '( sport = :8765 )'                 # expect only 127.0.0.1:8765
```

Check from the laptop. Both must fail to connect. If either one connects, run `docker compose --profile gateway down` and `docker compose down` on the server and stop.

```bash
curl -sS --max-time 5 http://<server-lan-ip>:8765/api/health
curl -sS --max-time 5 http://<server-tailnet-ip>:8765/api/health
```

### 7. Tailnet access (server)

```bash
sudo tailscale serve --bg 8765
tailscale serve status
tailscale funnel status
```

- `serve status` shows an `https://<machine>.<tailnet>.ts.net` URL that proxies to `http://127.0.0.1:8765`. The first run may ask you to enable HTTPS certificates in the Tailscale admin console.
- `funnel status` must show no Funnel. Never run `tailscale funnel`: it publishes the dashboard to the public internet, and the dashboard has no authentication.
- Open the URL from a device on your tailnet. `http://<tailnet-ip>:8765` does not work any more because of the loopback bind.

### 8. Keeper, portfolio and sentiment (server)

```bash
docker compose --profile gateway --profile auto up -d --build
docker compose logs -f cpgw_keeper        # Ctrl+C to leave
docker compose logs portfolio | head
```

Check:

- The keeper banner shows `auth=private`, a masked username (two characters and `***`), then a successful login. `auth=stub` means the private module was not loaded (check step 4, then rebuild).
- The portfolio banner shows `dry_run=True`.
- The sentiment worker is not started by `--profile auto`. It is in profile `research` and the sleeve is off.

### 9. Update and stop (server)

```bash
cd ~/trading-bot
git pull
docker compose --profile gateway --profile auto up -d --build
```

Stop everything:

```bash
docker compose --profile gateway --profile auto down
sudo tailscale serve reset
```

For nightly backups, add this line with `crontab -e`:

```
15 3 * * * $HOME/trading-bot/deploy/backup.sh
```

### 10. Reboot check (server)

The services use `restart: unless-stopped` and Docker is enabled, so everything should come back after a reboot.

```bash
sudo reboot
```

After you reconnect over SSH:

```bash
cd ~/trading-bot
docker compose --profile gateway --profile auto ps   # all services up, gateway and dashboard healthy
tailscale serve status                               # still proxies to http://127.0.0.1:8765
```

If a service is missing, run `docker compose --profile gateway --profile auto up -d` and look at its logs.

### Rollback (server)

Stop the Docker gateway and the bot containers:

```bash
cd ~/trading-bot
docker compose --profile gateway --profile auto down
```

Go back to the host Java gateway. Step 1 may have disabled the `cpgw` service. Re-enable it only after the Docker gateway is down, because both want port 5000:

```bash
ss -Hltn '( sport = :5000 )'              # expect no output
sudo systemctl enable --now cpgw
```

Return to the previous commit and rebuild:

```bash
git log --oneline -5
git checkout <previous-commit>
docker compose --profile auto up -d --build
```

Run `git checkout main` later to leave the detached state.

### Troubleshooting

- "Access Denied" from the gateway: see step 5.
- The gateway restarts in a loop: run `docker compose logs ibkr_gateway`. `Failed to load configuration file` means an old image that still passes `--conf`: run `git pull` and rebuild with `--build`. For anything else, report the last log lines, run `docker compose --profile gateway down`, and fall back to the Java gateway (see Rollback).
- The gateway exits with code 132 or "Illegal instruction": the CPU lacks AVX2. Use the Java gateway.
- `Pool overlaps` when compose creates the network: another Docker network or your LAN uses 192.168.250.0/24. Pick another `192.168.x.0/24` in `docker-compose.yml` (it must start with `192.` so the gateway's built-in allow list admits Docker's proxy) and update `tests/test_deploy_config.py`.
- Permission errors in `logs/`: `sudo chown 1000:1000 logs`.
- Keeper `ModuleNotFoundError`: the private module needs a package that is not in `requirements.txt` or `requirements-login.txt`. Add it and rebuild.
- The keeper refuses to start and mentions `IBKR_PASSWORD`: remove `IBKR_USERNAME` and `IBKR_PASSWORD` from `.env` (and from the environment of the container). The login is read from `secrets/ibkr_login` only.
- The keeper is locked out: see section 0B step 15.
- Keeper alert "the login setup is wrong": no login form was sent, so the IBKR account was not touched. Check `CPGW_CA_FILE` (the gateway certificate file), the gateway URL and that Playwright is installed. Then clear the lockout as in section 0B step 15.
- Keeper alert "the gateway gave no clear login state": the keeper waits and does not log in. The gateway answered with something other than a clear 401 or `authenticated: false`, for example a network error or a 5xx. Server: `docker compose logs --tail 50 ibkr_gateway`. No reset is needed: the keeper goes on once the gateway answers clearly.
- Keeper alert "cannot confirm that the session is a paper account": the gateway gave no account list after 3 tries. The session is kept, nothing is locked, and the keeper checks again. Only an account id without the `DU` prefix logs the keeper out and locks it.

---

## 0B. Phase 1 upgrade (paper account only)

Upgrade an existing home-server stack to the Phase 1 code: the keeper logs in by itself through the gateway's login page, the gateway gets its own TLS key, the login moves out of `.env` into `secrets/`, Telegram alerts are added, and the sentiment sleeve is switched off. Paper account only. Nothing here sends orders: the portfolio stays in dry run, and this section never changes that.

Read this first:

- You run every step by hand over SSH on the Ubuntu server. Several checks below can only be confirmed on your server. Each one says what to look for and gives a safe fallback. Every fallback fails closed: nothing is exposed and nothing trades.
- Do not skip a check. If one fails and its fallback is not enough, stop, run the stop command from step 3, and report the last log lines.
- Do the first login on a weekday, outside 00:45 to 01:30 New York time, and not on a Saturday. The keeper waits in those windows.
- A failed login is counted. A wrong password, a second-factor prompt, an unknown login page state, a login setup error or a non-paper account locks the keeper out at once. Three submitted logins in 24 hours with no good login in between lock it out too. A lockout waits for you to reset it (see step 15). The login cap is the only lockout that ends by itself, when old logins leave the 24 hours.

### 1. Back up first (server)

Run the backup script, then keep a second copy of `.env` and the private files outside the repo:

```bash
cd ~/trading-bot
deploy/backup.sh
umask 077
mkdir -p ~/backups/tradingbot/manual-$(date -u +%Y%m%d)
cp .env ~/backups/tradingbot/manual-$(date -u +%Y%m%d)/
cp core/ibkr_auth_private.py ~/backups/tradingbot/manual-$(date -u +%Y%m%d)/
git rev-parse HEAD > ~/backups/tradingbot/manual-$(date -u +%Y%m%d)/commit.txt
ls -l ~/backups/tradingbot/manual-$(date -u +%Y%m%d)/
```

If a `secrets/` folder already exists, copy it too with `cp -a secrets ~/backups/tradingbot/manual-$(date -u +%Y%m%d)/`. A first-time upgrade has none. If `core/ibkr_auth_private.py` does not exist, skip that `cp` line.

Look for: the backup script prints the archive it wrote, and every file in the manual folder is mode `-rw-------`. The backup archive holds `logs/` (with the session cookie), so keep `~/backups/tradingbot/` private.

### 2. Check memory and the current state (server)

```bash
free -m
docker stats --no-stream
docker compose --profile gateway --profile auto ps
```

Look for: about 3.6 GB available in `free -m`. The new memory limits add up to 2560m for the normal stack (gateway 768m, keeper 640m, portfolio 640m, dashboard 512m), and 2816m with the sentiment worker (256m). If much less than 3 GB is available, stop and report.

### 3. Stop the stack (server)

```bash
docker compose --profile gateway --profile auto --profile research down
```

This is also the safe stop command for every later step. It keeps the `ibkr_gateway_tls` volume and your files.

### 4. Update the code (server)

```bash
git fetch
git checkout main
git pull
git status --short
```

Do this after the pull request is merged. Look for: no output from `git status --short`.

### 5. Fetch the pinned gateway (server)

The gateway image is built from a zip you fetch first. The Dockerfile no longer downloads anything.

```bash
scripts/fetch_gateway.sh
git status --short
```

Check 3, the hash. Look for: a line starting with `OK:` and the file `vendor/ibkr-gateway/ibkr-gateway-linux-amd64.zip`. The script checks the zip against `docker/gateway/gateway.sha256`, and the image build checks it again. `git status --short` must print nothing, because `vendor/` is git-ignored.

If it prints `SHA-256 mismatch`, the script deleted the download. Stop. Do not build. The file is not the pinned one, and the gateway handles your login session.

If the build later says the `vendor` context is missing, you skipped this step.

### 6. Create the secrets (server)

```bash
mkdir -p secrets
chmod 700 secrets
umask 077
cp secrets/ibkr_login.example secrets/ibkr_login
nano secrets/ibkr_login
chmod 600 secrets/ibkr_login
openssl rand -hex 24 > secrets/gateway_ssl_password
sudo chown 10001:10001 secrets/gateway_ssl_password
sudo chmod 600 secrets/gateway_ssl_password
git check-ignore -v secrets/ibkr_login secrets/gateway_ssl_password
```

In `secrets/ibkr_login` put your paper username and password as `IBKR_USERNAME=...` and `IBKR_PASSWORD=...` lines. No quotes, no spaces around `=`, no other keys. `git check-ignore -v` must print the `.gitignore` rule for both files.

Check 1, the keeper's user id. The keeper container runs as uid 1000 (`user: "1000:1000"` in `docker-compose.yml`, `useradd --uid 1000` in `docker/Dockerfile`). Compose file secrets keep the host owner and mode. `core/secret_store.py` refuses a file that is not owned by the reading uid, or that has any group or other permission.

```bash
id -u
stat -c '%u:%g %a' secrets/ibkr_login
```

Look for: `1000` and `1000:1000 600`. If the file has another owner, run:

```bash
sudo chown 1000:1000 secrets/ibkr_login
sudo chmod 600 secrets/ibkr_login
```

If your server user is not uid 1000, the `logs/` folder also needs that owner (see Troubleshooting). If the keeper must run as another uid on your host, change the uid in both `docker/Dockerfile` and `docker-compose.yml`, in one commit, and update the test that pins it. Do not loosen the file mode instead.

Check 2, the gateway key password. The file must hold only letters and digits, 32 to 128 characters (`openssl rand -hex 24` gives 48). The gateway container runs as uid 10001 and must be able to read the file. With owner 10001 and mode 600, only that uid can read it. Your own user then needs `sudo` to read it, which is intended.

```bash
sudo stat -c '%u:%g %a' secrets/gateway_ssl_password
sudo grep -Ecx '[0-9A-Za-z]{32,128}' secrets/gateway_ssl_password
```

Look for: `10001:10001 600`, then `1`. If the second command prints `0`, the file has the wrong characters or length: recreate it with `openssl rand -hex 24`. The gateway entrypoint checks this too, and exits with a clear message. If it says it cannot read the secret, the owner or mode is wrong. Do not make the file world readable to get around it.

### 7. Update `.env` (server)

```bash
nano .env
```

- Delete the `IBKR_USERNAME` and `IBKR_PASSWORD` lines. The keeper refuses to start while `IBKR_PASSWORD` is set.
- Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (create the bot with BotFather on your phone and send it one message first).
- Leave `PAPER=true`, `DRY_RUN=true`, `IB_PORT=5000` and `DASHBOARD_HOST=127.0.0.1` as they are.
- Leave `IBKR_LOGIN_SECRET_FILE` empty in Docker. Set `CPGW_CA_FILE=/app/gateway_tls/cert.pem` when you run the `ibkr_gateway` service (compose does not set it for you). With the host Java gateway, point it at the exported certificate instead (see section 0).

`.env` is loaded into every container, so the Telegram token is visible to all of them. It is not a broker secret, but keep `.env` mode 600.

### 8. Private login module (server)

```bash
cp deploy/ibkr_auth_private.py.example core/ibkr_auth_private.py
chmod 600 core/ibkr_auth_private.py
git status --short
```

Look for: no output from `git status --short`. This replaces your old private login module. The new file holds no secret. It is wiring over the tracked `core/gateway_login.py`. If step 12 shows that the login page selectors differ, you override them in this file.

### 9. Start the gateway and check it (server)

The gateway must come up first. It writes its certificate into the `ibkr_gateway_tls` volume, and the keeper needs that certificate. This step covers checks 4 to 7.

```bash
docker compose --profile gateway up -d --build ibkr_gateway
docker compose --profile gateway ps ibkr_gateway
docker compose --profile gateway logs ibkr_gateway
```

Wait until `ps` shows `healthy`.

Check 4, TLS from the generated keystore. The entrypoint creates the gateway's own RSA key and a PKCS12 keystore (alias `vertx`) in the volume, encrypted with your secret. On every start it writes a copy to `/opt/ibkr-gateway/vertx.jks` under the gateway's built-in password, because the native gateway can only open it with that (ADR 0004). IBKR's default `vertx.jks` is deleted in the image. A test on the server confirmed that the gateway serves the key from that file.

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:5000/
openssl s_client -connect 127.0.0.1:5000 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
docker compose --profile gateway exec ibkr_gateway cat tls/cert.pem | openssl x509 -noout -fingerprint -sha256
```

Look for: a normal HTTP code (200 or 302, not `000`), and two identical SHA256 fingerprints. Identical means the gateway serves its own new certificate, not IBKR's default. If the container is unhealthy or restarts in a loop, TLS did not start. That fails closed: nothing is exposed. Run the stop command from step 3 and report the log lines. The fallback is a follow-up task, a real JKS keystore built with `keytool` in a one-off container. Do not bring back IBKR's default keystore.

Check 5, the built-in config. The native gateway ignores config files and always uses the config compiled into it, so there is no `conf.yaml` to mount and no `--conf` flag (ADR 0004).

Look for: no keystore, `Failed to load configuration file` or file-not-found error in `docker compose --profile gateway logs ibkr_gateway`, plus the check 4 results above. If it fails to start, nothing is exposed. Stop and report.

Check 6, the allow list. The built-in list allows `192.*`, `131.216.*` and `127.0.0.1`. Docker's proxy delivers every connection to the published port from the bridge gateway address `192.168.250.1`, which `192.*` admits, and the container's own healthcheck comes from `127.0.0.1`. The keeper runs on the host network and reaches the gateway at `127.0.0.1:5000`, so it arrives from `192.168.250.1` too. The LAN cannot connect because the port is published on `127.0.0.1` only (the laptop check below confirms it).

```bash
curl -sk https://127.0.0.1:5000/ | head -c 300
```

Look for: login page HTML, not "Access Denied". If you see "Access Denied", the source address was not admitted (the gateway logs `ipv4 address {} is forbidden`). Check that `docker network inspect trading-bot_ibkr_gateway` shows the subnet `192.168.250.0/24`, then stop and report.

Check 7, the certificate in the volume. The keeper reads `/app/gateway_tls/cert.pem` from the named volume. Compose prefixes the volume name with the project name (the folder name, so usually `trading-bot_ibkr_gateway_tls`).

```bash
docker compose --profile gateway exec ibkr_gateway ls -l tls
docker volume ls | grep ibkr_gateway_tls
```

Look for: `cert.pem` (mode 644) and `keystore.p12` in the listing, and one volume whose name ends in `ibkr_gateway_tls`. Use the real name from the second command in any `docker volume` command. If the keeper starts before `cert.pem` exists, its login refuses to run (it fails closed). So always start the gateway first.

If the entrypoint says the password does not match the keystore, you changed `secrets/gateway_ssl_password` after the first start. It never regenerates the key by itself. To start over, stop the gateway, remove the volume with `docker volume rm <real volume name>`, and start it again. That also makes a new certificate.

Then repeat the check from section 0A step 5 on your laptop: the LAN and tailnet addresses on port 5000 must still fail to connect.

### 10. Test Telegram (server)

Check 12.

```bash
docker compose run --rm --no-deps portfolio python -m live.telegram_notifier --test
echo $?
```

Look for: the text `telegram test message sent`, exit code 0, and one message on your phone that starts with `[tradingbot paper]`. `NOT sent` with exit code 1 means a wrong token or chat id, or no network. Fix `.env`. The notifier never raises, and its error text is redacted. The keeper and runners work without Telegram. You only lose the alerts.

### 11. Build the stack and check sizes (server)

```bash
docker compose --profile gateway --profile auto build
docker image ls | grep tradingbot
```

Look for: the keeper image `tradingbot-keeper:local` builds. It installs Playwright 1.64.1 and only Chromium's headless shell (`--only-shell chromium`), with no X server. Expect it to be roughly 300 to 450 MB larger than `tradingbot:local`. Note the real sizes and check the free disk with `df -h /var/lib/docker`.

If the build fails while installing Chromium, it is most likely a network problem with Playwright's download. Run it again. The browser download is pinned only through the `playwright==1.64.1` version, not by file hash.

### 12. Check the login page without logging in (server)

Checks 8 and 9. This probe opens the gateway login page in the keeper's own headless Chromium and prints what it finds. It never types a username or password and never clicks, so it cannot burn a login attempt. It also shows whether Chromium honours the pinned certificate key. The browser starts with `--ignore-certificate-errors-spki-list=<pin>` for exactly one key (the gateway's own) and the code never turns off certificate checks.

```bash
docker compose --profile gateway --profile auto run --rm --no-deps -T cpgw_keeper python - <<'PY'
from pathlib import Path
from core.gateway_login import GatewayLoginConfig, playwright_page, spki_pin_from_pem

try:
    from core import ibkr_auth_private
except ModuleNotFoundError:
    ibkr_auth_private = None
# Same config as a real login: the private file's overrides when it defines login_config().
make_config = getattr(ibkr_auth_private, "login_config", None)
cfg = make_config() if make_config else GatewayLoginConfig.from_env()
base = cfg.base_url or "https://127.0.0.1:5000"
pin = spki_pin_from_pem(Path(cfg.cert_file).read_bytes())
with playwright_page(cfg, pin) as page:
    page.goto(base + "/")
    raw = page._page
    print("url:", raw.url)
    print("title:", raw.title())
    for name in ("username_selector", "password_selector", "submit_selector"):
        selector = getattr(cfg, name)
        print(name, selector, "matches:", raw.locator(selector).count())
    for html in raw.eval_on_selector_all("input,button", "els => els.map(e => e.outerHTML)"):
        print(html[:200])
PY
```

Look for:

- No error. A `RuntimeError: navigation failed` means the page did not load. Either Chromium rejected the certificate (this headless shell did not honour the pin flag), or the gateway is down or denies the request. Check the gateway first (step 9). If `curl` works but the probe fails, the pin flag is the cause. That fails closed. Stop and report. The fix is a follow-up decision. Never switch to ignoring HTTPS errors.
- `matches: 1` for each of the three selectors. The defaults in `core/gateway_login.py` are best guesses:
  - username field: `input[name='username']`
  - password field: `input[name='password']`
  - submit button: `button[type='submit']`
- The `outerHTML` lines show the real field names. Compare them with the defaults.

The three result markers are texts that only appear after a submit, so no probe can check them safely:

- success: `text=Client login succeeds`
- bad credentials: `text=Invalid username password combination`
- second factor: `text=Challenge`

A wrong marker is safe but stops the keeper. Success is decided by the gateway API (`/iserver/auth/status`), not by page text, so a wrong success marker only costs up to the 30 second API wait. A wrong bad-credentials or second-factor marker ends as "needs a manual step", which locks the keeper out after that one attempt (see step 15). Nothing is retried.

If a selector shows `matches: 0`, override it in the private file. Edit `core/ibkr_auth_private.py` (copied from `deploy/ibkr_auth_private.py.example`) and change `login_config()` to return a changed copy of the config. Both `login()` and the step 12 probe call `login_config()`, so the probe shows the overrides. For example:

```python
import dataclasses

from core.gateway_login import GatewayLoginConfig, login_via_gateway_page


def login_config():
    return dataclasses.replace(
        GatewayLoginConfig.from_env(),
        username_selector="input#user",
    )


def login(creds, *, port, **kwargs):
    return login_via_gateway_page(
        creds,
        port=port,
        config=login_config(),
        on_submit=kwargs.get("on_submit"),
    )
```

`password_selector`, `submit_selector` and `markers` (a dict with the keys `success`, `bad_credentials` and `second_factor`) can be replaced the same way. Rebuild the keeper image after you edit the file (`docker compose --profile gateway --profile auto build cpgw_keeper`) and run the probe again until all three selectors match. Keep the file mode 600 and never commit it.

### 13. First login, watched (server)

Check 10 is part of this step.

First, the cookie-less 401 check. Before the first automatic login, find out how the gateway answers a request that carries no cookie. The keeper reads a cookie-less 401 as "nobody is logged in". That is only safe when the gateway session is gateway-wide. Log in once by hand in a browser (or use a gateway that is already authenticated), then run this on the Ubuntu server:

```bash
curl -sk -X POST https://127.0.0.1:5000/v1/api/iserver/auth/status
```

- It reports `"authenticated":true`: keep the default (`cookieless_401_means_logged_out: true` in `config/symbols.yaml`).
- It returns 401 while you are logged in: set `cookieless_401_means_logged_out: false` in the `keeper:` block of `config/symbols.yaml`. A cookie-less 401 then counts as "unsure": the keeper never submits on it and alerts after repeated unsure checks. Then expect to log in by hand after a session loss, because the keeper cannot tell a lost session from a cookie-bound one.

```bash
docker compose --profile gateway --profile auto run --rm --no-deps cpgw_keeper python -m live.cpgw_keeper --once 2>&1 | tee /tmp/keeper_first_login.txt
```

Look for: a banner with `auth=private`, a masked username and `paper=True`, then the line `login OK, account=***NN`. The account id is masked to its last two digits, so the `DU` prefix does not show. The keeper itself logs out and locks out when an account id does not start with `DU`. Telegram shows "login OK, session restored".

Then the leak check. It counts the lines that contain your password in the keeper output, the event log and the keeper state. Process substitution keeps the password out of the command line and the shell history.

```bash
grep -c -F -f <(sed -n 's/^IBKR_PASSWORD=//p' secrets/ibkr_login) /tmp/keeper_first_login.txt
grep -c -a -F -f <(sed -n 's/^IBKR_PASSWORD=//p' secrets/ibkr_login) logs/bot.sqlite logs/keeper_state.json
rm /tmp/keeper_first_login.txt
```

Look for: `0` for the first command, and `0` for each file in the second. If any count is above 0, stop, change your IBKR password, and report it.

Check 10, the empty cookie. The gateway may hold the session itself, so the browser may have no cookie to hand over. The keeper then confirms the login through `CPGWClient` with that empty cookie and the gateway's `auth/status`. The `login OK` line above is that confirmation. To see whether the cookie was empty:

```bash
python3 -c "import json; print(bool(json.load(open('logs/cpgw_session.json'))['cookie']))"
```

Either answer is fine. `False` means an empty cookie and a working login. If the keeper says `login finished but the gateway is not authenticated`, or fails after the form was submitted, that is a counted failed attempt. Read step 15 before you try again, and do not run the first login in a loop.

If the keeper fails with `needs a manual step`, the selectors or markers do not match: go back to step 12. The keeper is then locked out and needs `--reset-lockout` (step 15).

If the keeper logs `paper check inconclusive`, the gateway gave no account list. That is not a lockout. The session is kept, the keeper checks again on later ticks, and it alerts at most once an hour. Wait for the next check before you act. If it reports `login setup is wrong`, nothing was submitted to IBKR: fix the setup named in the alert, then reset (step 15).

### 14. Start the stack and check it (server)

```bash
docker compose --profile gateway --profile auto up -d
docker compose --profile gateway --profile auto ps
docker stats --no-stream
curl -fsS http://127.0.0.1:8765/api/health
```

Look for: every service running, each under its memory limit in `docker stats`, and the health answer with `"paper":true`, `"dry_run":true` and `"ib_port":5000`. The portfolio banner must show `dry_run=True`.

Check 11, memory. Chromium peaks (roughly 250 to 400 MB) only while a login runs, then it closes. After a day, and after the first nightly gap, run:

```bash
docker stats --no-stream
sudo journalctl -k --since "1 day ago" | grep -i oom
```

Look for: no service above about 80% of its limit, and no `oom` lines. An out-of-memory kill during a login still counts as a submitted attempt (it is recorded before the form is submitted), which is the safe side. If a service sits above 80%, raise its `mem_limit` and `memswap_limit` together in `docker-compose.yml` (they stay equal). A test pins these numbers, so update `tests/test_deploy_config.py` in the same commit. Training through the dashboard inside the 512m container is likely to be killed, so train out of band as before.

### 15. Lockout: read it and reset it (server)

Check 14. The keeper stops logging in, and keeps tickling, after a wrong password, a second-factor prompt, an unknown login page state, a login setup error (a CA file, gateway URL or Playwright problem, refused before any form is sent), a non-paper account, three submitted logins in 24 hours with no good login in between, or six submitted logins in 24 hours even when each worked (`keeper.max_logins_per_window`, a sign that the session keeps dropping). A session that lasted under 10 minutes (`keeper.min_session_sec`) also makes the next drop back off. It sends one Telegram alert that says what is needed, and repeats it once a day while the lockout lasts. The lockout is saved in `logs/keeper_state.json`, so a container restart does not clear it.

Read the state:

```bash
python3 -m json.tool logs/keeper_state.json
docker compose logs --tail 50 cpgw_keeper
```

Look for: `locked_out_reason` (`null` means not locked out. A setup error reads `setup: ...`. The login cap shows only in the keeper logs and the alert, and ends by itself when old logins roll out of the 24 hours), `failed_attempts` (submitted logins since the last good login), `submit_times` (every submitted login, kept across good logins and used for the login cap), `consecutive_failures`, `last_login_at` and `authenticated_since`. The file holds no secret. A corrupt state file also counts as locked out.

Fix the cause first: the password in `secrets/ibkr_login`, the second factor on the IBKR account, the selectors from step 12, the account itself, or for a setup error the CA file (`CPGW_CA_FILE`), the gateway URL or Playwright. Then clear the lockout:

```bash
docker compose run --rm --no-deps cpgw_keeper python -m live.cpgw_keeper --reset-lockout
docker compose --profile auto restart cpgw_keeper
```

The reset clears the reason, the failure counters and the submit times (so it also lifts the login cap) and keeps the session times. It also clears the saved alert cooldowns for the lockout, not-paper and corrupt-state alerts, so the next one of each kind is sent at once. If the cause is not fixed, the next attempt fails and counts again. If a wrong password was the cause, fix it on IBKR's own web site before you reset, because repeated bad passwords can lock the IBKR account itself.

### 16. Sentiment check (server)

Check 13. The sentiment sleeve is off (status `research`, allocation 0%), because Alpha Vantage `NEWS_SENTIMENT` is now a premium endpoint. The `sentiment` service moved to the compose profile `research` and does not start with `--profile auto`. You can still check that the data problem is reported loudly:

```bash
docker compose --profile research run --rm --no-deps sentiment python -m live.sentiment_worker --check
echo $?
```

Look for: with `provider: alpha_vantage` and the premium endpoint, exit code 2 and one Telegram message of kind `health`. That proves the failure is loud. With the `stub` provider the exit code is 0 and it prints that there is nothing to check. The check writes no sentiment rows.

### 17. Watch the first days (server)

Watch the dashboard events and Telegram for the first night (the gap around 01:00 New York time), the first Saturday, and the first 24 hour reset. Expect an info alert in each case, not a lockout. The portfolio runner sends one Telegram message per order (tagged `[DRY-RUN]`) and a daily summary after 17:00 New York time.

### 18. Rollback (server)

Check 15.

```bash
cd ~/trading-bot
docker compose --profile gateway --profile auto --profile research down
git checkout <commit from step 1>
cp ~/backups/tradingbot/manual-<date>/.env .env
cp ~/backups/tradingbot/manual-<date>/ibkr_auth_private.py core/ibkr_auth_private.py
chmod 600 .env core/ibkr_auth_private.py
docker compose --profile gateway --profile auto up -d --build
```

Use the folder date and commit hash from step 1. The old code ignores `secrets/`, `vendor/` and the `ibkr_gateway_tls` volume, so they can stay. If you went back to the host Java gateway, follow the Rollback in section 0A instead. Run `git checkout main` later to leave the detached state.

### Known limits

- The dashboard can start the keeper, the runners and the sentiment worker as subprocesses inside its own container. That container reads `CPGW_CA_FILE` from `.env` like every service, but it does not mount the `ibkr_gateway_tls` volume, so the certificate file does not exist there. It also has no login secret and no Chromium. A keeper started there cannot log in, and a runner started there raises `CPGWError` at start because it cannot load the certificate (it fails closed, it does not skip verification). Run the workers as compose services. Do not rely on the dashboard control page for the keeper.
- Two keepers (one from the dashboard, one from the container) are not supported. The lockout file blocks both, but they would still compete for the session.
- The Telegram token in `.env` is visible to every container. A token-file variant is a follow-up.
- The Chromium sandbox is off. It is set explicitly in `core/gateway_login.py` (`chromium_sandbox=False`), because Docker blocks the user namespaces it needs. The browser only opens the local gateway address, and every other address is blocked.
- `logs/cpgw_session.json` can hold a session cookie and is in the nightly backup. Protect the backup folder.

---

## 1. Get the code onto the server

```bash
# pick a stable location
sudo mkdir -p /opt/trading-bot
sudo chown "$USER":"$USER" /opt/trading-bot
git clone <your-fork-or-archive> /opt/trading-bot
cd /opt/trading-bot

# secrets - rotate the Gmail app password that was on Desktop
cp .env.example .env
$EDITOR .env
```

`.env` should have (the IBKR login does not go here: it is the file `secrets/ibkr_login`, see section 0B):
```
PAPER=true
IB_HOST=localhost
IB_PORT=5000
IB_CLIENT_ID=0
EMAIL_USER=you@gmail.com
EMAIL_PASS=<16-char gmail app password>
EMAIL_RECIPIENTS=you@gmail.com
ALPHA_VANTAGE_API_KEY=your_av_key
```

Set permissions so it's not world-readable:
```bash
chmod 600 .env
```

---

## 2A. Path A - Docker compose

### Install Docker
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# log out + back in for group to take effect
```

### Start the dashboard
```bash
cd /opt/trading-bot
docker compose up -d --build
docker compose ps
# dashboard reachable on http://127.0.0.1:8765
```

### Start the portfolio runner alongside
```bash
docker compose --profile auto up -d --build
docker compose logs -f portfolio
```

Stays in dry-run by default. To send paper orders, edit `docker-compose.yml`:
```yaml
services:
  portfolio:
    command:
      - python
      - -m
      - live.portfolio_runner
      - --live           # <- add this
```
Then `docker compose --profile auto up -d`.

### Updates / restart
```bash
git pull
docker compose --profile auto up -d --build
```

### Logs
```bash
docker compose logs -f dashboard
docker compose logs -f portfolio
# or tail the SQLite log file directly:
docker compose exec dashboard sqlite3 logs/bot.sqlite \
  "SELECT ts,symbol,prediction,confidence FROM predictions ORDER BY id DESC LIMIT 20"
```

---

## 2B. Path B - systemd (no Docker)

### Install Python + venv
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev build-essential
```

### Set up the venv
```bash
cd /opt/trading-bot
mkdir -p logs          # the units mount logs/ and models/artifacts/ read-write
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Install the systemd units

The unit files are templates (`%i` expands to the user instance, which the service runs as). They assume the repo is cloned at `/opt/trading-bot`. Replace `user` with your username, then:

```bash
sudo cp deploy/systemd/tradingbot-dashboard.service /etc/systemd/system/tradingbot-dashboard@.service
sudo cp deploy/systemd/tradingbot-portfolio.service /etc/systemd/system/tradingbot-portfolio@.service
# optional: sentiment worker and CPGW keeper
sudo cp deploy/systemd/tradingbot-sentiment.service /etc/systemd/system/tradingbot-sentiment@.service
sudo cp deploy/systemd/tradingbot-cpgw-keeper.service /etc/systemd/system/tradingbot-cpgw-keeper@.service

# only needed if your clone is not at /opt/trading-bot: edit WorkingDirectory, EnvironmentFile,
# ExecStart and ReadWritePaths in each unit

sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-dashboard@user
sudo systemctl enable --now tradingbot-portfolio@user    # optional

sudo systemctl status tradingbot-dashboard@user
journalctl -u tradingbot-dashboard@user -f
```

(`@user` is the instance name - pass your username there. `%i` in the unit gets substituted with it.)

Warning: with the systemd units (not Docker), never leave `DASHBOARD_HOST=` empty in `.env`. systemd then passes an empty host and uvicorn listens on all interfaces, and the dashboard has no authentication. Set it to `127.0.0.1` or remove the line.

### Going live
Edit `/etc/systemd/system/tradingbot-portfolio@.service`, change `ExecStart` to add `--live`, then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart tradingbot-portfolio@user
```

---

## 3. Remote access - Tailscale (recommended)

Do **not** expose port 8765 to the public internet. There's no auth.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8765
tailscale serve status
```

The dashboard binds to `127.0.0.1` (`DASHBOARD_HOST`), so `http://<tailnet-ip>:8765` does not work. Open the `https://<machine>.<tailnet>.ts.net` URL that `serve status` prints, from any device on the same tailnet. Never use `tailscale funnel`.

For external services that need callbacks (none right now, but in case Phase 6 adds webhooks): `tailscale serve` or a Cloudflare Tunnel with auth. Don't open the port.

---

## 4. Backups

```bash
# Run once to confirm it works:
/opt/trading-bot/deploy/backup.sh

# Schedule nightly at 03:15 UTC:
crontab -e
# add:
15 3 * * *  /opt/trading-bot/deploy/backup.sh
```

Outputs to `~/backups/tradingbot/tradingbot-YYYYMMDD-HHMMSS.tar.gz`. Keeps the last 14 by default (`TRADINGBOT_BACKUP_KEEP` env override). Contains `logs/`, `models/artifacts/`, and `config/symbols.yaml`.

To restore: `tar -xzf <file>.tar.gz -C /opt/trading-bot/`.

---

## 5. Day-2 ops

| Want to… | Docker | systemd |
|---|---|---|
| See current state | `docker compose ps` | `systemctl status tradingbot-*` |
| Tail logs | `docker compose logs -f portfolio` | `journalctl -u tradingbot-portfolio@USER -f` |
| Restart bot | `docker compose --profile auto restart portfolio` | `sudo systemctl restart tradingbot-portfolio@USER` |
| Pull updates | `git pull && docker compose --profile auto up -d --build` | `git pull && sudo systemctl restart tradingbot-*` |
| Stop everything | `docker compose --profile auto down` | `sudo systemctl stop tradingbot-*` |
| Train a model | dashboard /control, or `docker compose run --rm dashboard python -m scripts.train_symbol --symbol SPY --kind xgboost` | `sudo -u USER /opt/trading-bot/.venv/bin/python -m scripts.train_symbol --symbol SPY --kind xgboost` |

---

## 6. Verifying the paper-only lock on the server

After deploy, sanity-check:
```bash
docker compose exec dashboard curl -fsS http://127.0.0.1:8765/api/health
# expect:  {"ok":true,"paper":true,"ib_port":5000,...}
```

If you ever see `"paper":false` or any `"ib_port"` other than `5000`, the bot will refuse to start any IB connection (`core.config.assert_paper_mode` raises before the socket opens). The dashboard will still come up so you can see what's misconfigured.

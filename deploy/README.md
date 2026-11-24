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
- The `cpgw_keeper` service runs the SRP-6a login automatically - you never need
  to open the CPGW web UI. Credentials live in `.env` at the repo root (`IBKR_USERNAME`,
  `IBKR_PASSWORD`); they go through SRP so the password never crosses the wire
  in plaintext.

> If you used Phase 5 with `ibapi` + IB Gateway (Java desktop with GUI login):
> the new flow replaces that entirely. You no longer need to log in via VNC daily.
> The Java CPGW process replaces the IB Gateway process, and `cpgw_keeper`
> handles the auth.

---

## 0A. Home server (Docker)

A fresh install on an Ubuntu box with Docker, reached over Tailscale, deployed by hand over SSH. It runs the gateway container, the dashboard, the keeper, the portfolio runner (dry run) and the sentiment worker.

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
IBKR_USERNAME=<your paper username>
IBKR_PASSWORD=<your paper password>
SENTIMENT_PROVIDER=stub
```

- `SENTIMENT_PROVIDER` is `stub` (no key) or `alpha_vantage`. For `alpha_vantage`, also set `ALPHA_VANTAGE_API_KEY`.
- Fill the `EMAIL_*` values too if you want notifications.
- `.env` is git-ignored. Never commit it.

### 4. Private login module

The public repo has a stub login. The real one is a private file that stays on your machines and is never committed.

Laptop (macOS zsh or Windows PowerShell, same command):

```bash
scp ibkr_auth_private.py <user>@<server>:~/trading-bot/core/ibkr_auth_private.py
```

Use the Tailscale name or IP of the server for `<server>`.

Server:

```bash
cd ~/trading-bot
chmod 600 core/ibkr_auth_private.py
git check-ignore -v core/ibkr_auth_private.py   # must print the .gitignore rule
git status --short                              # must print nothing
```

- Never run `git add -f` on this file.
- The Docker build copies it into the local image `tradingbot:local`. Never push that image to a registry.

### 5. Gateway (server)

```bash
cd ~/trading-bot
docker compose --profile gateway up -d --build ibkr_gateway
docker compose ps ibkr_gateway            # wait until it shows healthy
ss -Hltn '( sport = :5000 )'              # expect only 127.0.0.1:5000
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:5000/
```

- Any HTTP code (for example 200 or 302) means the gateway answers. Then check the body: `curl -sk https://127.0.0.1:5000/ | head -c 300`.
- If the body text is "Access Denied", the gateway ignored the mounted `docker/gateway/conf.yaml` (this behaviour of the pinned build is not verified yet). The status code alone does not show this, so read the body. Run `docker compose --profile gateway down` and stop. Nothing is exposed. Report it before going on.
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
docker compose logs sentiment | head
```

Check:

- The keeper banner shows `auth=private`, then a successful login. `auth=stub` means the private module was not loaded (check step 4, then rebuild).
- The portfolio banner shows `dry_run=True`.
- The sentiment banner shows `provider=` with the value you chose.

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
- The gateway restarts in a loop: run `docker compose logs ibkr_gateway`. One likely cause is that the unverified `--conf ../root/conf.yaml` path is wrong for the pinned build. Report the last log lines, run `docker compose --profile gateway down`, and fall back to the Java gateway (see Rollback).
- The gateway exits with code 132 or "Illegal instruction": the CPU lacks AVX2. Use the Java gateway.
- `Pool overlaps` when compose creates the network: another Docker network uses 172.31.250.0/24. Change the subnet in `docker-compose.yml` and the allow pattern in `docker/gateway/conf.yaml` together. The tests check that they match.
- Permission errors in `logs/`: `sudo chown 1000:1000 logs`.
- Keeper `ModuleNotFoundError`: the private module needs a package that is not in `requirements.txt`. Add it and rebuild.

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

`.env` should have:
```
PAPER=true
IB_HOST=localhost
IB_PORT=5000
IB_CLIENT_ID=0
IBKR_USERNAME=your_ibkr_login
IBKR_PASSWORD=your_ibkr_password
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

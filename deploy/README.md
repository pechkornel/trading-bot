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
  to open the CPGW web UI. Credentials live in `bot/.env` (`IBKR_USERNAME`,
  `IBKR_PASSWORD`); they go through SRP so the password never crosses the wire
  in plaintext.

> If you used Phase 5 with `ibapi` + IB Gateway (Java desktop with GUI login):
> the new flow replaces that entirely. You no longer need to log in via VNC daily.
> The Java CPGW process replaces the IB Gateway process, and `cpgw_keeper`
> handles the auth.

---

## 1. Get the code onto the server

```bash
# pick a stable location
sudo mkdir -p /opt/trading-bot
sudo chown "$USER":"$USER" /opt/trading-bot
git clone <your-fork-or-archive> /opt/trading-bot
cd /opt/trading-bot/bot

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
cd /opt/trading-bot/bot
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
cd /opt/trading-bot/bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Install the systemd units

The two unit files are templates (`%i` expands to the user instance). Replace `user` with your username, then:

```bash
sudo cp deploy/systemd/tradingbot-dashboard.service /etc/systemd/system/tradingbot-dashboard@.service
sudo cp deploy/systemd/tradingbot-portfolio.service /etc/systemd/system/tradingbot-portfolio@.service

# edit the WorkingDirectory paths if your install isn't at /home/USER/trading-bot
sudo $EDITOR /etc/systemd/system/tradingbot-dashboard@.service
sudo $EDITOR /etc/systemd/system/tradingbot-portfolio@.service

sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-dashboard@user
sudo systemctl enable --now tradingbot-portfolio@user    # optional

sudo systemctl status tradingbot-dashboard@user
journalctl -u tradingbot-dashboard@user -f
```

(`@user` is the instance name - pass your username there. `%i` in the unit gets substituted with it.)

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
# read off your Tailscale IPv4 - e.g. 100.x.x.x
```

Open `http://100.x.x.x:8765` from any device logged into the same Tailnet.

For external services that need callbacks (none right now, but in case Phase 6 adds webhooks): `tailscale serve` or a Cloudflare Tunnel with auth. Don't open the port.

---

## 4. Backups

```bash
# Run once to confirm it works:
/opt/trading-bot/bot/deploy/backup.sh

# Schedule nightly at 03:15 UTC:
crontab -e
# add:
15 3 * * *  /opt/trading-bot/bot/deploy/backup.sh
```

Outputs to `~/backups/tradingbot/tradingbot-YYYYMMDD-HHMMSS.tar.gz`. Keeps the last 14 by default (`TRADINGBOT_BACKUP_KEEP` env override). Contains `logs/`, `models/artifacts/`, and `config/symbols.yaml`.

To restore: `tar -xzf <file>.tar.gz -C /opt/trading-bot/bot/`.

---

## 5. Day-2 ops

| Want to… | Docker | systemd |
|---|---|---|
| See current state | `docker compose ps` | `systemctl status tradingbot-*` |
| Tail logs | `docker compose logs -f portfolio` | `journalctl -u tradingbot-portfolio@USER -f` |
| Restart bot | `docker compose --profile auto restart portfolio` | `sudo systemctl restart tradingbot-portfolio@USER` |
| Pull updates | `git pull && docker compose --profile auto up -d --build` | `git pull && sudo systemctl restart tradingbot-*` |
| Stop everything | `docker compose --profile auto down` | `sudo systemctl stop tradingbot-*` |
| Train a model | dashboard /control, or `docker compose run --rm dashboard python -m scripts.train_symbol --symbol SPY --kind xgboost` | `sudo -u USER /opt/trading-bot/bot/.venv/bin/python -m scripts.train_symbol --symbol SPY --kind xgboost` |

---

## 6. Verifying the paper-only lock on the server

After deploy, sanity-check:
```bash
docker compose exec dashboard curl -fsS http://127.0.0.1:8765/api/health
# expect:  {"ok":true,"paper":true,"ib_port":4002,...}
```

If you ever see `"paper":false` or `"ib_port":4001`/`7496`, the bot will refuse to start any IB connection (`core.config.assert_paper_mode` raises before the socket opens). The dashboard will still come up so you can see what's misconfigured.

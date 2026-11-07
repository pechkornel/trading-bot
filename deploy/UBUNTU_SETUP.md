# Ubuntu home-server setup - headless walkthrough

Copy-paste, top-to-bottom. Assumes a fresh Ubuntu 22.04 or 24.04 LTS server you
reach over SSH. Everything is terminal-only, no GUI on the server.

Replace `user` with your username, `192.168.x.x` with your server's LAN IP,
and `/opt/trading-bot` with wherever you want the code to live.

---

## 0. From your laptop - first SSH in + initial hardening

```bash
# Make sure your SSH key is on the server (skip if you set this up at install time)
ssh-copy-id user@192.168.x.x

# Log in
ssh user@192.168.x.x
```

Inside the server:

```bash
# Update + upgrade
sudo apt update && sudo apt -y upgrade

# Timezone (use IANA name - your local one, not UTC, so log timestamps make sense)
sudo timedatectl set-timezone Europe/Bratislava
timedatectl                                  # verify

# Auto security updates (recommended - set-and-forget)
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades

# Hostname (optional but nice in logs)
sudo hostnamectl set-hostname tradingbot

# Basic firewall - deny everything by default, will allow SSH below.
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
```

**SSH access - pick one, then run the matching commands below.**

The bot itself never opens any port other than 22; the dashboard lives on the Tailnet (via `network_mode: host` + Tailscale, set up later).

- **A. Tailscale-only SSH + keep password login** *(recommended if you log in from multiple personal machines)* - port 22 is only reachable from your Tailnet, so brute-forcing it requires already being on your Tailnet. You install Tailscale in step 2 below; come back here and run this *after* step 2:

  ```bash
  sudo ufw allow in on tailscale0 to any port 22
  sudo ufw --force enable
  # password login stays on - fine, only Tailnet peers can reach the port
  ```

- **B. Open SSH + key-only auth** - port 22 reachable from anywhere on your LAN (or wherever), but passwords are disabled so brute force can't get in:

  ```bash
  sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sudo systemctl restart ssh
  sudo ufw allow 22/tcp
  sudo ufw --force enable
  ```

- **C. Both** - Tailscale-only **and** key-only. Belt + suspenders; what you'd do if the server lived in a colo. Run the commands from both A and B.

If you're using A: don't enable UFW yet - finish step 2 (install Tailscale) first, then come back. Otherwise you'll lock yourself out.

---

## 1. Install Docker

The bot runs in Docker. CPGW (IBKR's gateway daemon) runs on the host because it
needs Java and persistent state - Docker would be more friction than help.

```bash
# Docker engine + compose plugin (official one-liner)
curl -fsSL https://get.docker.com | sudo sh

# Let your user run docker without sudo
sudo usermod -aG docker $USER

# Log out + back in for the group change to take effect
exit
ssh user@192.168.x.x

# Verify
docker version
docker compose version
```

---

## 2. Install Tailscale (for remote dashboard access)

Without this, you'd have to SSH-tunnel port 8765 every time you want to look at
the dashboard. Tailscale makes the bot reachable at a stable `100.x.x.x`
address from any device on your Tailnet, with no port exposure to the public
internet.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                            # follow the URL it prints
tailscale ip -4                              # note this IP, you'll hit the dashboard at <ip>:8765
tailscale status
```

---

## 3. Install Java + CPGW (the only thing not in a container)

```bash
sudo apt install -y openjdk-17-jre-headless unzip
java -version

# Download Client Portal Gateway from IBKR
sudo mkdir -p /opt/cpgw && sudo chown $USER:$USER /opt/cpgw
cd /opt/cpgw
wget https://download2.interactivebrokers.com/portal/clientportal.gw.zip
unzip clientportal.gw.zip
rm clientportal.gw.zip
ls                                           # expect: bin/  build/  dist/  doc/  root/

# Smoke-test that CPGW starts (kill it with Ctrl+C after you see "Listening on port 5000")
./bin/run.sh root/conf.yaml
# ^C to quit
```

Now wrap it as a systemd unit so it restarts on reboot:

```bash
sudo tee /etc/systemd/system/cpgw.service >/dev/null <<EOF
[Unit]
Description=IBKR Client Portal Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$USER
WorkingDirectory=/opt/cpgw
ExecStart=/opt/cpgw/bin/run.sh /opt/cpgw/root/conf.yaml
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cpgw
sudo systemctl status cpgw                   # active (running)

# Confirm it's listening on 5000
curl -sk https://localhost:5000/ | head -1
```

---

## 4. Get the bot code onto the server

```bash
sudo mkdir -p /opt/trading-bot && sudo chown $USER:$USER /opt/trading-bot

# Option A - clone from your git remote (recommended)
git clone <your-fork-url> /opt/trading-bot

# Option B - rsync from your laptop:
#   (on the laptop)
#   rsync -avh --exclude='.venv' --exclude='__pycache__' --exclude='logs' \
#       ./ \
#       user@192.168.x.x:/opt/trading-bot/

cd /opt/trading-bot
```

---

## 5. Secrets - fill in `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env                                    # or vim, whatever you use
```

What to put in `.env`:

```
PAPER=true
DRY_RUN=true
IB_HOST=localhost
IB_PORT=5000
IB_CLIENT_ID=0

# IBKR credentials - used by cpgw_keeper for SRP-6a login
IBKR_USERNAME=your_ibkr_paper_login
IBKR_PASSWORD=your_ibkr_paper_password

# Gmail SMTP for trade alerts (generate an App Password)
EMAIL_USER=you@gmail.com
EMAIL_PASS=your_16_char_gmail_app_password
EMAIL_RECIPIENTS=you@gmail.com

# Alpha Vantage news + sentiment (free key)
ALPHA_VANTAGE_API_KEY=your_alpha_vantage_key
```

Sanity check it's readable only by you:

```bash
stat -c '%a %n' .env                         # expect: 600 .env
```

---

## 6. First run - bring everything up

```bash
cd /opt/trading-bot
docker compose --profile auto up -d --build

# Watch logs while things spin up
docker compose ps
docker compose logs -f cpgw_keeper           # wait for "login OK - account=<id>"
```

Once the keeper says `login OK`, everything else lights up automatically:
- `dashboard` (port 8765)
- `cpgw_keeper` (auth + tickle every 60s)
- `sentiment` (Alpha Vantage poll every hour)
- `portfolio` (multi-sleeve runner, **dry-run** by default)

Open the dashboard from any Tailnet device:

```
http://<tailnet-ip>:8765
```

Verify the paper lock and that everything's authenticated:

```bash
curl -s http://localhost:8765/api/health
# expect: {"ok":true,"paper":true,"dry_run":true,"ib_port":5000,...}
```

---

## 7. Train initial models

The portfolio runner needs a per-symbol XGBoost model in the registry before
the tactical sleeve can predict anything. Train them one at a time (Docker
runs the command inside the container that already has the env + cookies):

```bash
for sym in SPY AAPL QQQ GLD; do
  docker compose run --rm dashboard \
    python -m scripts.train_symbol --symbol $sym --kind xgboost --classifier multiclass
done
```

Each one takes 1-5 minutes depending on how aggressive the grid search runs.
Verify:

```bash
ls models/artifacts/
# expect: AAPL_5min_xgboost/  GLD_5min_xgboost/  QQQ_5min_xgboost/  SPY_5min_xgboost/  legacy/
```

Or check via dashboard: **Models** page shows each one with test accuracy +
metadata.

---

## 8. Switch from dry-run to paper orders (when you're ready)

Dry-run = the bot logs every decision but never sends an order. Useful for
weeks of "watching" before you trust it with real (paper) orders.

When ready:

```bash
nano docker-compose.yml
# find the `portfolio:` service, in its `command:` block append:
#   - --live
# save + exit

docker compose --profile auto up -d portfolio
docker compose logs -f portfolio
```

You'll see `PAPER tactical BUY ...` lines instead of `DRY-RUN tactical BUY ...`.
Orders go to the IBKR paper account, not real money - even if you somehow
edited the .env wrong, `assert_paper_mode()` won't let the keeper authenticate
against a non-paper port.

---

## 9. Daily backups

```bash
crontab -e
# add:
15 3 * * *  /opt/trading-bot/deploy/backup.sh
```

Writes `~/backups/tradingbot/tradingbot-YYYYMMDD-HHMMSS.tar.gz` containing
SQLite logs, trained models, and the watchlist config. Keeps the last 14, prunes
older. Restore with `tar -xzf <file>.tar.gz -C /opt/trading-bot/`.

---

## 10. Day-2 reference

| Task | Command |
|---|---|
| See what's running | `docker compose ps` |
| Tail keeper | `docker compose logs -f cpgw_keeper` |
| Tail portfolio runner | `docker compose logs -f portfolio` |
| Tail sentiment worker | `docker compose logs -f sentiment` |
| Restart everything | `docker compose --profile auto restart` |
| Stop everything (preserve state) | `docker compose --profile auto down` |
| Update from git | `git pull && docker compose --profile auto up -d --build` |
| Open SQLite directly | `docker compose exec dashboard sqlite3 logs/bot.sqlite` |
| Last 10 predictions | `docker compose exec dashboard sqlite3 logs/bot.sqlite "SELECT ts,symbol,prediction,confidence FROM predictions ORDER BY id DESC LIMIT 10"` |
| CPGW status | `sudo systemctl status cpgw` |
| Restart CPGW | `sudo systemctl restart cpgw` (then the keeper re-logs in within ~60s) |
| Force a fresh login | `docker compose restart cpgw_keeper` |

---

## 11. Sanity checklist (after first deploy)

- [ ] `sudo systemctl is-active cpgw` → `active`
- [ ] `docker compose ps` → all 4 services `Up`
- [ ] `docker compose logs cpgw_keeper | grep "login OK"` → at least one match
- [ ] `curl -s http://localhost:8765/api/health` → `"paper":true,"ib_port":5000`
- [ ] Dashboard reachable from another Tailnet device at `http://<tailnet-ip>:8765`
- [ ] `ls models/artifacts/` → at least one trained model
- [ ] `chmod 600 .env` (no group/world read)
- [ ] `crontab -l` shows the nightly backup line
- [ ] `ufw status` shows only port 22 allowed

If all 8 are ✓, you're done.

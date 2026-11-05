#!/usr/bin/env bash
# Daily backup: snapshots logs/ and models/artifacts/ to ~/backups/tradingbot/.
# Keeps the last 14 backups.
#
# Cron example (run nightly at 03:15):
#   15 3 * * *  /home/USER/trading-bot/bot/deploy/backup.sh

set -euo pipefail

# Resolve the bot/ directory regardless of where this script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"

DEST="${TRADINGBOT_BACKUP_DIR:-$HOME/backups/tradingbot}"
KEEP="${TRADINGBOT_BACKUP_KEEP:-14}"

mkdir -p "$DEST"

stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$DEST/tradingbot-$stamp.tar.gz"

echo "[backup] snapshotting $BOT_DIR -> $out"
tar -C "$BOT_DIR" -czf "$out" \
    --exclude="logs/*.log" \
    --exclude="logs/processes.json" \
    logs models/artifacts config/symbols.yaml 2>/dev/null

# Prune old backups
mapfile -t files < <(ls -1t "$DEST"/tradingbot-*.tar.gz 2>/dev/null || true)
if (( ${#files[@]} > KEEP )); then
    for old in "${files[@]:KEEP}"; do
        echo "[backup] pruning $old"
        rm -f -- "$old"
    done
fi

echo "[backup] done — kept ${#files[@]} files in $DEST"

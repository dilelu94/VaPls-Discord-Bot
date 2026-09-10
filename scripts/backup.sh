#!/usr/bin/env bash
# backup.sh — Backs up production data/ directory and secret configuration files
#
# Runs locally on the server (via systemd timer vapls-backup.timer or manually).
# 1. Creates a local mirror of data/ and .env files in $HOME/vapls-backups/
# 2. Syncs to backup server free-02 (ubuntu@193.122.210.127) if SSH key exists.
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/vapls-discord-bot}"
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-$HOME/vapls-backups}"
REMOTE_HOST="${BACKUP_REMOTE_HOST:-ubuntu@193.122.210.127}"
SSH_KEY="${BACKUP_SSH_KEY:-$HOME/.ssh/free-02}"

echo "==> [BACKUP] Starting backup at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# 1. Local backup creation
mkdir -p "$LOCAL_BACKUP_DIR/data"

if [ -d "$DEPLOY_DIR/data" ]; then
    echo "==> [BACKUP] Syncing $DEPLOY_DIR/data -> $LOCAL_BACKUP_DIR/data..."
    rsync -aq "$DEPLOY_DIR/data/" "$LOCAL_BACKUP_DIR/data/"
fi

for env_file in .env userbot/.env golive/.env; do
    if [ -f "$DEPLOY_DIR/$env_file" ]; then
        echo "==> [BACKUP] Copying $env_file..."
        mkdir -p "$LOCAL_BACKUP_DIR/$(dirname "$env_file")"
        cp -f "$DEPLOY_DIR/$env_file" "$LOCAL_BACKUP_DIR/$env_file"
    fi
done

echo "==> [BACKUP] Local backup updated at $LOCAL_BACKUP_DIR"

# 2. Remote backup sync to free-02 if SSH key is available
if [ -f "$SSH_KEY" ]; then
    echo "==> [BACKUP] Syncing to remote backup server ($REMOTE_HOST)..."
    if rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10" \
        --delete \
        "$LOCAL_BACKUP_DIR/" "$REMOTE_HOST:~/backups/vapls-discord-bot/"; then
        echo "==> [BACKUP] Remote sync to $REMOTE_HOST completed successfully."
    else
        echo "⚠️ [BACKUP] Remote sync to $REMOTE_HOST failed (server unreachable?). Local backup intact."
    fi
else
    echo "ℹ️ [BACKUP] Remote SSH key ($SSH_KEY) not found. Local backup intact."
fi

echo "==> [BACKUP] Finished at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

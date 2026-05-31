#!/usr/bin/env bash
# deploy.sh — idempotent server-side deploy script.
#
# Run ON the server (not locally). CI calls it over SSH after every green
# push to master. Safe to run manually as well — all steps are idempotent.
#
# Required env:
#   DEPLOY_DIR  — path to the repo checkout (default: $HOME/vapls-discord-bot)
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/vapls-discord-bot}"

echo "==> Deploy started at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "    Repo: $DEPLOY_DIR"

# ── 1. Change into the repo directory ────────────────────────────────────────
cd "$DEPLOY_DIR"

# ── 2. Fetch the latest state from origin ────────────────────────────────────
echo "==> git fetch origin"
git fetch origin

# ── 3. Remove untracked files that collide with tracked upstream paths ────────
# This handles stale duplicates (e.g. a geminiKeys.py that was edited on the
# server but is byte-identical to origin/master). We only remove files whose
# path exists in origin/master — genuinely server-only untracked files
# (.env, .env backups, audio_output/, downloads/) are left untouched because
# they are not tracked in the upstream tree.
echo "==> Removing untracked files that collide with origin/master..."
git ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
    if git cat-file -e "origin/master:$f" 2>/dev/null; then
        echo "    removing colliding untracked file: $f"
        rm -f "$f"
    fi
done

# ── 4. Reset to origin/master and capture old → new SHAs ─────────────────────
OLD=$(git rev-parse HEAD)
echo "==> git reset --hard origin/master  (was $(git rev-parse --short "$OLD"))"
git reset --hard origin/master
NEW=$(git rev-parse HEAD)
echo "    $(git rev-parse --short "$OLD") → $(git rev-parse --short "$NEW")"

# ── 5. Smart dependency install ───────────────────────────────────────────────
# Only reinstall if the relevant requirements file changed in this update.
DEPS_REINSTALLED=""

if [ "$OLD" != "$NEW" ]; then
    # Main bot requirements
    if git diff --name-only "$OLD" "$NEW" -- requirements.txt | grep -q .; then
        echo "==> requirements.txt changed — reinstalling main venv deps"
        venv/bin/pip install -r requirements.txt
        DEPS_REINSTALLED="${DEPS_REINSTALLED} requirements.txt"
    fi

    # Userbot requirements
    if git diff --name-only "$OLD" "$NEW" -- userbot/requirements.txt | grep -q .; then
        echo "==> userbot/requirements.txt changed — reinstalling userbot venv deps"
        userbot/venv/bin/pip install -r userbot/requirements.txt
        DEPS_REINSTALLED="${DEPS_REINSTALLED} userbot/requirements.txt"
    fi
else
    echo "==> No new commits — skipping dependency check."
fi

if [ -z "$DEPS_REINSTALLED" ]; then
    echo "==> No dependency files changed — skipping pip install."
fi

# ── 6. Restart services ───────────────────────────────────────────────────────
echo "==> Restarting discord-bot and vapls-userbot..."
sudo systemctl restart discord-bot vapls-userbot

# ── 7. Verify both services came back active ──────────────────────────────────
# systemd marks Type=simple units "active" the instant the process spawns, so a
# crash-on-boot (bad import, missing dep) would slip past an immediate check.
# Give them a few seconds to settle so a fast crash-loop is caught here.
sleep 5
FAILED=""
for svc in discord-bot vapls-userbot; do
    STATUS=$(systemctl is-active "$svc" 2>/dev/null || true)
    if [ "$STATUS" != "active" ]; then
        echo "ERROR: $svc is '$STATUS' (expected 'active')"
        FAILED="${FAILED} $svc"
    fi
done

if [ -n "$FAILED" ]; then
    echo ""
    echo "✘ Deploy FAILED — service(s) did not come back active:$FAILED"
    echo "  Check logs with: journalctl -u <service> -n 50"
    exit 1
fi

# ── 8. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "✔ Deploy complete"
echo "  Commits: $(git rev-parse --short "$OLD") → $(git rev-parse --short "$NEW")"
echo "  Deps reinstalled:${DEPS_REINSTALLED:- (none)}"
echo "  Services active: discord-bot, vapls-userbot"
echo "  Finished at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

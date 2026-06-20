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

    # GoLive userbot requirements
    if git diff --name-only "$OLD" "$NEW" -- golive/requirements.txt | grep -q .; then
        echo "==> golive/requirements.txt changed — reinstalling golive venv deps"
        golive/venv/bin/pip install -r golive/requirements.txt
        DEPS_REINSTALLED="${DEPS_REINSTALLED} golive/requirements.txt"
    fi
else
    echo "==> No new commits — skipping dependency check."
fi

if [ -z "$DEPS_REINSTALLED" ]; then
    echo "==> No dependency files changed — skipping pip install."
fi

# ── 5b. Playwright browser install (idempotent) ───────────────────────────
# Only needed when playwright is installed (fresh venv or version bump).
# If already installed, this is a quick no-op.
if python -c "import playwright" 2>/dev/null; then
    echo "==> playwright detected — ensuring chromium is installed"
    python -m playwright install --with-deps chromium 2>&1 || true
fi

# ── 5c. GoLive userbot: create venv + .env + service if missing ────────────
if [ -d "$DEPLOY_DIR/golive" ]; then
    if [ ! -d "$DEPLOY_DIR/golive/venv" ]; then
        echo "==> Creating golive/venv..."
        python3 -m venv "$DEPLOY_DIR/golive/venv"
        "$DEPLOY_DIR/golive/venv/bin/pip" install --upgrade pip
        "$DEPLOY_DIR/golive/venv/bin/pip" install -r "$DEPLOY_DIR/golive/requirements.txt"
        echo "    golive venv created."
        # Force reinstall discord.py-self in case discord-ext-voice-recv polluted the namespace
        "$DEPLOY_DIR/golive/venv/bin/pip" uninstall -y discord.py 2>/dev/null || true
        "$DEPLOY_DIR/golive/venv/bin/pip" install --force-reinstall --no-deps "discord.py-self @ git+https://github.com/dolfies/discord.py-self"
    fi

    if [ ! -f "$DEPLOY_DIR/golive/.env" ]; then
        echo "==> Creating golive/.env from .env.example..."
        cp "$DEPLOY_DIR/golive/.env.example" "$DEPLOY_DIR/golive/.env"
        echo "    ⚠️  Edit golive/.env with GOLIVE_TOKEN to enable GoLive streaming."
    else
        echo "    golive/.env already exists."
    fi

    if [ ! -f /etc/systemd/system/golive-userbot.service ]; then
        echo "==> Installing golive-userbot.service..."
        sudo cp "$DEPLOY_DIR/golive/golive-userbot.service" /etc/systemd/system/golive-userbot.service
        sudo systemctl daemon-reload
    fi
fi

# ── 5d. Ensure GoLive relay vars in main .env ──────────────────────────
# The main bot's .env needs GOLIVE_RELAY_URL and a GOLIVE_RELAY_SECRET
# that matches the one in golive/.env. This section reads the secret from
# golive/.env (or uses the default from .env.example) and writes both
# vars to the main .env if they are missing.
MAIN_ENV="$DEPLOY_DIR/.env"
GOLIVE_ENV="$DEPLOY_DIR/golive/.env"
DEFAULT_SECRET="vapls-golive-shared-secret"

# Read the current relay secret from golive/.env, preferring an explicit
# non-empty value over the default.
GOLIVE_SECRET=""
if [ -f "$GOLIVE_ENV" ]; then
    GOLIVE_SECRET=$(grep '^GOLIVE_RELAY_SECRET=' "$GOLIVE_ENV" | tail -1 | cut -d= -f2-)
fi
if [ -z "$GOLIVE_SECRET" ]; then
    GOLIVE_SECRET="$DEFAULT_SECRET"
    # Write it to golive/.env so it survives future deploys
    if [ -f "$GOLIVE_ENV" ]; then
        # If the key exists but is empty, replace it; otherwise append
        if grep -q '^GOLIVE_RELAY_SECRET=' "$GOLIVE_ENV" 2>/dev/null; then
            sed -i "s|^GOLIVE_RELAY_SECRET=.*|GOLIVE_RELAY_SECRET=$GOLIVE_SECRET|" "$GOLIVE_ENV"
        else
            echo "GOLIVE_RELAY_SECRET=$GOLIVE_SECRET" >> "$GOLIVE_ENV"
        fi
        echo "    Set golive/.env GOLIVE_RELAY_SECRET=$GOLIVE_SECRET"
    fi
fi

# Ensure main .env has GOLIVE_RELAY_URL
if [ -f "$MAIN_ENV" ]; then
    if ! grep -q '^GOLIVE_RELAY_URL=' "$MAIN_ENV" 2>/dev/null; then
        # Add before the closing section or at end of file
        echo 'GOLIVE_RELAY_URL=http://127.0.0.1:8082' >> "$MAIN_ENV"
        echo "    Added GOLIVE_RELAY_URL=http://127.0.0.1:8082 to .env"
    fi
    # Ensure main .env has GOLIVE_RELAY_SECRET matching golive/.env
    if grep -q '^GOLIVE_RELAY_SECRET=' "$MAIN_ENV" 2>/dev/null; then
        # Update existing value
        sed -i "s|^GOLIVE_RELAY_SECRET=.*|GOLIVE_RELAY_SECRET=$GOLIVE_SECRET|" "$MAIN_ENV"
    else
        echo "GOLIVE_RELAY_SECRET=$GOLIVE_SECRET" >> "$MAIN_ENV"
    fi
    echo "    Set .env GOLIVE_RELAY_SECRET=$GOLIVE_SECRET"
fi

# ── 6. Restart services ───────────────────────────────────────────────────────
echo "==> Restarting services..."
SERVICES="discord-bot indio-userbot"
if [ -f /etc/systemd/system/golive-userbot.service ]; then
    SERVICES="$SERVICES golive-userbot"
fi
sudo systemctl restart $SERVICES

# ── 7. Verify both services came back active ──────────────────────────────────
# systemd marks Type=simple units "active" the instant the process spawns, so a
# crash-on-boot (bad import, missing dep) would slip past an immediate check.
# Give them a few seconds to settle so a fast crash-loop is caught here.
sleep 5
FAILED=""
CHECK_SERVICES="discord-bot indio-userbot"
if [ -f /etc/systemd/system/golive-userbot.service ]; then
    CHECK_SERVICES="$CHECK_SERVICES golive-userbot"
fi
for svc in $CHECK_SERVICES; do
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
echo "  Services active: $CHECK_SERVICES"
echo "  Finished at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

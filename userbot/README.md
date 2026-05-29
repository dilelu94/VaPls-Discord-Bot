# VaPls Userbot — Spanish voice transcription

This sub-project runs a Discord **user account** (not a bot) that joins voice
channels, captures speech, and transcribes Spanish with VOSK.

Why a userbot? Discord's DAVE (End-to-End Encryption) does not give bots the
MLS keys needed to decrypt voice. A real user account participates in MLS
naturally, so audio is decrypted client-side and we can route it to VOSK.

> ⚠️ User-account automation technically violates Discord ToS. Use at your
> own risk on accounts you own. Discord generally enforces against spam /
> abuse, not personal projects.

## What this does (and doesn't)

| | |
|---|---|
| ✅ Auto-joins voice channels when humans enter | |
| ✅ Auto-leaves when the channel is empty | |
| ✅ Per-user Spanish transcription via VOSK | |
| ✅ Posts transcripts to a configurable text channel | |
| ✅ Optional HTTP forward to the main bot's API | |
| ❌ Plays audio (the main bot still handles /play, /soundpad) | |
| ❌ Slash commands (those live in the main bot) | |
| ❌ Soundboard greeting (main bot handles it on its own join) | |

## Setup (Oracle server)

### 1. Create the Discord user account

Use a **secondary account** dedicated to this role. Enable 2FA and don't
share its password. Pick a discreet display name (e.g. `🎙️ Transcriptor`)
and a low-profile avatar — anyone in the voice channel will see it.

Join the account to the same guild(s) as the main bot. Give it permission
to connect + speak in the relevant voice channels.

### 2. Get the user token

> The token is the equivalent of a password. Treat it as a secret and
> never commit it.

In a browser logged in as the secondary account:
1. Open Discord in the browser.
2. Open DevTools (F12) → **Network** tab.
3. Reload the page or send a message.
4. Click any request to `discord.com/api/v9/...`.
5. Under **Request Headers**, copy the value of `authorization`.

### 3. Install on the Oracle server

```bash
ssh -i ssh-key-2026-05-27.key ubuntu@129.80.59.99
cd /home/ubuntu/vapls-discord-bot/userbot
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
deactivate
cp .env.example .env
# Edit .env and paste your USER_TOKEN
nano .env
```

The Spanish VOSK model is shared with the main bot at
`/home/ubuntu/vapls-discord-bot/models/vosk-model-small-es-0.42` — no copy
needed.

### 4. Register as a systemd service

```bash
sudo cp vapls-userbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vapls-userbot.service
sudo systemctl status vapls-userbot.service
```

### 5. Watch the logs

```bash
sudo journalctl -u vapls-userbot.service -f
```

Look for:

```
✅ Spanish VOSK model loaded.
Userbot online as <user>#<tag>
[VOICE] Connecting to <channel>
[VOICE] Starting listener in <channel>
[VOSK] First packet received (user_id=...)
[VOSK][es] user_id=...: hola probando uno dos tres
```

## Config reference

See `.env.example` for the full list. Highlights:

- `USER_TOKEN` — required, see step 2.
- `MODEL_PATH_ES` — defaults to the main bot's models dir; rarely needs changing.
- `GUILD_ALLOWLIST` — comma-separated guild IDs. Empty = listen everywhere.
- `IGNORE_USER_IDS` — recommended to add the main bot's user ID so we
  don't transcribe its music playback.
- `TRANSCRIPT_CHANNEL_NAME` — e.g. `bot-testing`. Empty = log to stdout only.
- `ENABLE_HTTP_FORWARD` — set to `true` later when the main bot exposes a
  `/transcript` endpoint to receive these.

## Stop / restart

```bash
sudo systemctl restart vapls-userbot.service
sudo systemctl stop vapls-userbot.service
sudo systemctl disable vapls-userbot.service
```

## Troubleshooting

- **`401 Unauthorized` on startup** → Token is invalid/expired. Repeat step 2.
- **No `[VOSK] First packet received`** → Voice library may not support
  receive in the version you have. Try installing
  `discord.py-self` from `main`:
  `pip install -U git+https://github.com/dolfies/discord.py-self`.
- **Transcripts are garbage / partial** → Spanish small model is light;
  for better accuracy upgrade to the larger model
  (`vosk-model-es-0.42`, ~1.4 GB).
- **High RAM** → Disable the larger model, or run on a beefier VM.

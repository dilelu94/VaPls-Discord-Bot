# Architecture

## Overview
VaPls runs as two cooperating processes:
- **Main bot (`bot.py`)**: Handles slash commands, voice playback, greetings,
  analytics, and the HTTP API server.
- **Userbot (`userbot/bot.py`)**: Logs in with a real user account to receive
  voice audio (DAVE E2EE), transcribes Spanish with Vosk, and optionally forwards
  transcripts to the main bot's HTTP API.

## Entry points
- `bot.py`: Main Discord bot.
- `userbot/bot.py`: Voice transcription userbot.
- Scripts: `run.sh`, `runMonitored.sh`, `autoRestart.sh`, `deploy.sh`.
- Systemd: `discord-bot.service` (created by `deploy.sh`) and
  `userbot/vapls-userbot.service`.

## Module responsibilities and interactions
- **bot.py**: Registers slash commands and event handlers. Calls into
  `playCommand`, `soundpadCommand`, `geminiCommand`, `greeting`, `analytics`,
  and starts `apiServer`.
- **playCommand.py**: `GuildPlayer` queue lifecycle, yt-dlp downloads, FFmpeg
  playback, and control UI. Emits analytics and uses `greeting` for entry
  triggers.
- **soundpadCommand.py**: `SoundpadView` UI for browsing audio clips. Reuses
  `playCommand` state to prevent overlapping music playback. Emits analytics.
- **geminiCommand.py**: `/vapls` and `/indio` logic. Uses `geminiClient` and
  `analytics`.
- **geminiClient.py**: Async HTTP client for Gemini generateContent. Multi-key
  pool with **sticky** selection (stay on one key until it 429s, then rotate)
  so Gemini's per-key implicit prompt cache keeps hitting the stable
  system-prompt + tools prefix. Callers pass per-turn volatile data via
  `volatile_context=` (sent last, in the user turn) to keep that prefix
  byte-stable; `GeminiReply.cached_tokens` reports cache-hit tokens.
- **apiServer.py**: HTTP API for status, members, queue, and audio playback.
- **analytics.py**: PostHog wrapper; no-ops if disabled.
- **greeting.py**: Greeting trigger + throttling. Uses `users.py` and config.
- **users.py**: Per-user greeting audio mapping.
- **config.py / userbot/config.py**: Environment-driven configuration.
- **tests/testSoundpad.py**: Soundpad UI and pagination tests.

## Data flows
### Voice playback
1. `/play` → `playLogic` → `GuildPlayer` queues songs.
2. `GuildPlayer` runs yt-dlp to download and plays audio via FFmpeg.
3. `/soundpad` → `SoundpadView` → plays local audio clips.
4. `greeting.set_pending_trigger` + voice state updates trigger entry sounds.

### Transcription
1. Userbot joins voice channels and attaches `TranscriberSink`.
2. PCM is resampled to 16 kHz and processed by Vosk.
3. `on_transcript` logs text, posts to a text channel, and optionally forwards
   to `BOT_API_BASE` (expects an external `/transcript` handler).

### Gemini responses
1. `/vapls` or `/indio` → `geminiCommand`.
2. `geminiClient.generate` calls Gemini API.
3. Responses are chunked and sent back to Discord.

## Dependency boundaries
- The main bot does **not** receive voice; the userbot is required for
  transcription in DAVE E2EE channels.
- HTTP API calls require `X-API-Secret`.
- Analytics are optional and disabled without `POSTHOG_API_KEY`.

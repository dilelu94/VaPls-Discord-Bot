# Configuration

## Main bot (.env)
| Variable | Required | Default | Description / implications |
| --- | --- | --- | --- |
| `TOKEN` | ✅ | none | Discord bot token. Required to start `bot.py`. |
| `MODEL_PATH_ES` | ❌ | `models/vosk-model-small-es-0.42` | Legacy/unused by main bot; kept for parity with older STT flow. |
| `MODEL_PATH_EN` | ❌ | `models/vosk-model-small-en-us-0.15` | Legacy/unused by main bot. |
| `AUDIO_DIR` | ❌ | `audio/` | Legacy path (used in tests). |
| `CUSTOM_AUDIO_PATH` | ❌ | `/var/home/dilelu/Desktop/Output` | Base directory for soundpad clips and greeting audio. |
| `YT_DLP_PATH` | ❌ | `yt-dlp` | Path to yt-dlp binary used by `/play`. |
| `DEBUG_GUILD_IDS` | ❌ | empty | Comma-separated guild IDs for instant command registration. |
| `RAM_THRESHOLD_MB` | ❌ | `300` | Currently unused; reserved for resource monitoring. |
| `PLAY_COOLDOWN` | ❌ | `5` | Currently unused; reserved for rate limiting. |
| `POSTHOG_API_KEY` | ❌ | empty | Enables analytics when set. |
| `POSTHOG_HOST` | ❌ | `https://us.i.posthog.com` | PostHog host URL. |
| `API_HOST` | ❌ | `127.0.0.1` | HTTP API bind host. |
| `API_PORT` | ❌ | `8080` | HTTP API port. |
| `API_SECRET` | ⚠️ | empty | Required to authorize API requests; if empty, API returns 503. |
| `GEMINI_API_KEY` | ⚠️ | empty | Required for `/vapls` and `/indio`. |
| `GEMINI_MODEL` | ❌ | `gemini-2.5-flash` | Gemini model name para `/indio` y `/vapls`. |
| `GEMINI_DECIFRAR_MODEL` | ❌ | `gemini-2.5-flash-lite` | Modelo para `decifrarTranscripcion` (limpieza ASR). Lite tiene 1000 RPD vs 250 de flash, libera cupo del modelo grande. |
| `VOICE_IDLE_TIMEOUT_SECONDS` | ❌ | `60` | Segundos sin reproducir/pausado tras los cuales el bot se desconecta solo del canal de voz (manejado por `idleWatchdog.py`). |

## Userbot (.env in userbot/)
| Variable | Required | Default | Description / implications |
| --- | --- | --- | --- |
| `USER_TOKEN` | ✅ | none | Discord **user** token. Required to start `userbot/bot.py`. |
| `WHISPER_MODEL` | ❌ | `small` | faster-whisper model size. `small` on the Ampere A1 4/24 server; revert to `base` on smaller VMs. |
| `WHISPER_COMPUTE_TYPE` | ❌ | `int8` | CTranslate2 quantization. `int8` for CPU; `float16` for GPU. |
| `WHISPER_CACHE_DIR` | ❌ | empty | Where to cache the downloaded Whisper model. |
| `WHISPER_CPU_THREADS` | ❌ | `4` | CTranslate2 threads. Match vCPU count of the host. |
| `MAX_CONCURRENT_IDLE` | ❌ | `5` | Max overlapping utterances transcribed when the main bot is idle. |
| `MAX_CONCURRENT_WHILE_PLAYING` | ❌ | `3` | Max overlapping utterances while main bot plays audio (ffmpeg headroom). |
| `GUILD_ALLOWLIST` | ❌ | empty | Comma-separated guild IDs to join; empty = all. |
| `IGNORE_USER_IDS` | ❌ | empty | User IDs to ignore for transcription (e.g., main bot). |
| `TRANSCRIPT_CHANNEL_NAME` | ❌ | empty | Text channel name for posting transcripts. |
| `ENABLE_HTTP_FORWARD` | ❌ | `false` | Enables HTTP forwarding to the main bot API. |
| `BOT_API_BASE` | ❌ | `http://127.0.0.1:8080` | Base URL for HTTP forwarding. |
| `BOT_API_SECRET` | ❌ | empty | Must match `API_SECRET` when forwarding. |
| `MAIN_BOT_API_BASE` | ❌ | `http://127.0.0.1:8080` | Main bot API for `/playing` polling and `/indio` invocation. |
| `MAIN_BOT_API_SECRET` | ❌ | empty | Auth for `MAIN_BOT_API_BASE`; must match main bot's `API_SECRET`. |
| `IDLE_LEAVE_SECONDS` | ❌ | `60` | Segundos sin humanos en ningún canal de voz del guild antes de que el userbot se desconecte. El timer se cancela apenas alguien (re)entra. `0` = legacy (desconectar al instante). |
| `LOG_LEVEL` | ❌ | `INFO` | Python logging level for the userbot. |

## Security notes
- Never commit `TOKEN` or `USER_TOKEN`.
- If you enable HTTP forwarding, keep `API_SECRET` and `BOT_API_SECRET` aligned.

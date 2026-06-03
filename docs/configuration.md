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
| `VOICE_IDLE_TIMEOUT_SECONDS` | ❌ | `60` | Segundos sin reproducir/pausado tras los cuales el bot se desconecta solo del canal de voz (manejado por `idleWatchdog.py`). |
| `DECIFRAR_FEEDBACK_ENABLED` | ❌ | `true` | Master toggle del feedback inline de calidad del ASR. Cuando está activo, 1 de cada N transcripciones de voz recibe reacciones 👍/❌ para que los usuarios marquen falsos positivos. |
| `DECIFRAR_FEEDBACK_SAMPLE_RATE` | ❌ | `3` | 1 de cada N transcripciones de voz recibe el par de reacciones. Subir para sampleo menor (menos ruido en el canal). |
| `DECIFRAR_FEEDBACK_TIMEOUT_MINUTES` | ❌ | `60` | Minutos antes de que el sweeper limpie las reacciones de un sample que nadie votó. |
| `DECIFRAR_FALSE_POSITIVES_LOG_PATH` | ❌ | `data/false_positives.jsonl` | Path al JSONL persistente donde se loggean los ❌ (raw whisper + VOSK N-best) para debug offline de la calidad del ASR. Gitignored. |
| `INDIO_REPLY_CHANNEL_ID` | ❌ | `1490008278275461280` | Canal único donde el Indio postea sus respuestas, sin importar el trigger (`/indio`, wake-word de texto, voz, HTTP). Cuando el `/indio` se invoca desde otro canal, se postea un aviso público "<@user> te respondo en <#TARGET>" en el canal del slash. `0` = comportamiento clásico (responde donde se lo invoca). |
| `AUTODJ_GRACE_SECONDS` | ❌ | `15` | Segundos que el Auto-DJ muestra la sugerencia antes de reproducirla sola. Durante esa ventana cualquiera puede vetarla ("ese tema no" o el botón). |
| `AUTODJ_MAX_CHAIN` | ❌ | `10` | Cuántos temas seguidos pone el Auto-DJ sin intervención humana antes de apagarse solo (evita que suene a un canal vacío toda la noche). |
| `AUTODJ_MENU_CHANNEL_ID` | ❌ | `451607097432604672` | Canal **fallback** para el panel del modo DJ. Normalmente el panel se postea en el canal donde se corrió `/dj` (o donde el Indio detectó el pedido); este ID se usa solo si no se puede resolver ese canal. |

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
| `WAKE_SOUND_ENABLED` | ❌ | `true` | Master toggle del sonidito de confirmación que se reproduce cuando VOSK detecta la wake word. `false` desactiva la feature. |
| `WAKE_SOUND_PATH` | ❌ | empty | Path al audio. Si es relativo se resuelve contra `CUSTOM_AUDIO_PATH`. Vacío = feature inactiva aunque `WAKE_SOUND_ENABLED=true`. El repo trae un beep bakeado en `userbot/assets/wake.ogg` — apuntalo con path absoluto en el `.env` del server. |
| `WAKE_SOUND_THROTTLE_SECONDS` | ❌ | `0.0` | Mínimo de segundos entre dos sonidos en el mismo canal. `0` = sin throttle (cada detección suena), útil mientras se calibra. Subir si en producción molesta el spam. |
| `LOG_LEVEL` | ❌ | `INFO` | Python logging level for the userbot. |

## Security notes
- Never commit `TOKEN` or `USER_TOKEN`.
- If you enable HTTP forwarding, keep `API_SECRET` and `BOT_API_SECRET` aligned.

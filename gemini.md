# VaPls-Discord-Bot - Documentación para Gemini 🎙️🤖

Este archivo es una guía rápida para asistentes de IA sobre la arquitectura,
flujos principales y convenciones del proyecto.

## 📚 Documentación
- [Arquitectura](docs/architecture.md)
- [Configuración](docs/configuration.md)
- [HTTP API](docs/api.md)
- [Comandos](docs/commands.md)
- [Operaciones](docs/operations.md)
- [Testing](docs/testing.md)
- [Contribución y docstrings](docs/contributing-docs.md)

## 📋 Descripción General
**VaPls-Discord-Bot** corre en dos procesos:
- **Main bot**: comandos, playback de audio, soundpad, Gemini y HTTP API.
- **Userbot**: transcripción de voz (DAVE/E2EE) con Vosk.

## 🛠️ Stack Tecnológico y Dependencias
- **Lenguaje:** Python 3.10+
- **Discord bot:** `py-cord`
- **Userbot:** `discord.py-self` + `discord-ext-voice-recv`
- **STT:** `vosk` (offline)
- **Audio:** `FFmpeg`, `audioop`
- **HTTP:** `aiohttp`
- **Configuración:** `python-dotenv`
- **Analytics (opcional):** `posthog`
- **Descargas:** `yt-dlp`

## 📂 Arquitectura y Estructura de Archivos
Referencia rápida (detalle completo en [docs/architecture.md](docs/architecture.md)):
- `bot.py`: entrada principal y slash commands.
- `userbot/bot.py`: transcripción de voz y forwarding opcional.
- `playCommand.py`: cola de música y yt-dlp.
- `soundpadCommand.py`: UI de soundpad.
- `geminiCommand.py`: `/vapls` y `/indio`.
- `apiServer.py`: HTTP API.
- `geminiClient.py`: cliente Gemini.
- `analytics.py`: wrapper PostHog.
- `greeting.py` / `users.py`: saludos.

## 🔬 Detalles de Implementación Clave
### 1) Parche de DAVE en userbot
El userbot envuelve `PacketDecryptor._decrypt_rtp_*` para aplicar
`dave.decrypt()` después del AEAD, permitiendo decodificar audio en canales E2EE.

### 2) Pipeline de transcripción (TranscriberSink)
1. Recibe PCM desde `voice_recv`.
2. Convierte a mono y re-samplea a 16 kHz.
3. Ejecuta Vosk y genera texto final.
4. `on_transcript` publica en un canal de texto y/o forwardea por HTTP.

### 3) Playback de música (GuildPlayer)
`/play` descarga con yt-dlp, reproduce con FFmpeg y mantiene cola/estado por
guild con pre-descarga en segundo plano.

## 🛠️ Comandos de Discord (Slash Commands)
- `/play`: reproduce música de YouTube.
- `/soundpad`: panel de clips locales.
- `/vapls`: respuestas Gemini sin memoria.
- `/indio`: persona con memoria corta por guild + memoria de largo plazo destilada por Gemini.
- `/parar`: detiene playback y desconecta.
- `/quit`: desconecta sin limpiar cola.

## 🧪 Pruebas Unitarias
Las pruebas viven en `tests/` y corren con **pytest** (+ `pytest-asyncio`). La
filosofía es testear *comportamiento observable*, no detalle de implementación:
se mockea solo en los bordes reales (Discord, la API HTTP de Gemini, PostHog, el
filesystem) y se asienta sobre los resultados (qué ve el usuario, qué estado
queda), no sobre el texto exacto ni los conteos de llamadas — así el código se
puede refactorizar sin romper los tests. Detalle completo en [docs/testing.md](docs/testing.md).

Cobertura actual (primer pase, ~80%):
- `test_keywords.py`: detección de palabras clave (es/en, case-insensitive).
- `test_config.py`: parseo/defaults de variables de entorno (recarga el módulo).
- `test_discord_chunking.py`: corte de respuestas largas en chunks de Discord.
- `test_error_messages.py` / `test_user_header.py`: mensajes de error por persona y header de cita.
- `test_gemini_client.py`: parseo de respuestas y clasificación de errores de `geminiClient.generate` (boundary HTTP fakeado).
- `test_vapls_logic.py` / `test_indio_logic.py`: lógica de `/vapls` y `/indio` (memoria por-guild, reset con `nuevo`, TTL, persistencia).
- `test_long_term_memory.py`: helpers puros de la memoria a largo plazo.
- `test_greeting.py`: saludo al entrar a un canal (throttle, resolución de path, skips).
- `tests/testSoundpad.py`: suite original del soundpad.

Instalá las dependencias de test y corré la suite:
```bash
pip install -r requirements-dev.txt
pytest
```

Pendiente para un segundo pase: `playCommand`, `apiServer`, `userbot` y extender
`soundpadCommand`. CI: `.github/workflows/ci.yml` corre `pytest` en cada push/PR.

## 📜 Doc generation
Sphinx + napoleon recomendado. Ver [docs/contributing-docs.md](docs/contributing-docs.md).

## 💡 Guía de Modificación para Gemini
1. **Mantener DAVE patch:** No eliminar el patch en `userbot/bot.py` salvo que
   haya cambios claros en la API de Discord.
2. **Config y .env:** Toda nueva variable de entorno debe documentarse en
   `docs/configuration.md` (y `.env.example` si aplica).
3. **Docs primero:** Mantener `README.md` y los docs alineados si cambia la
   arquitectura o comandos.

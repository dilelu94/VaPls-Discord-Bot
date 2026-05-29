# VaPls-Discord-Bot — Guía para agentes de IA 🎙️🤖

Guía rápida sobre la arquitectura, flujos principales y convenciones del
proyecto. Es la **fuente canónica** de instrucciones para asistentes de IA.

> **Setup de archivos:** este `AGENTS.md` es el original. `CLAUDE.md`, `GEMINI.md`
> y `.github/copilot-instructions.md` son symlinks a este archivo; Codex lee
> `AGENTS.md` directamente. Las skills viven en `.agents/skills/` y `.claude` es
> un symlink a `.agents`. **Editá siempre los originales (`AGENTS.md` y `.agents/`),
> nunca los symlinks.**

## 🧠 Skills disponibles
- [`behavioral-testing`](.agents/skills/behavioral-testing/SKILL.md): cómo escribir
  tests en este repo. **Usala siempre que escribas o modifiques tests.**

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

## 🧪 Testing
Los tests viven en `tests/` y corren con **pytest** (+ `pytest-asyncio`). La
filosofía es testear *comportamiento observable*, no detalle de implementación:
se mockea solo en los bordes reales (Discord, la API HTTP de Gemini, PostHog, el
filesystem) y se asienta sobre los resultados (qué ve el usuario, qué estado
queda), no sobre el texto exacto ni los conteos de llamadas — así el código se
puede refactorizar sin romper los tests.

**Antes de tocar tests, leé la skill [`behavioral-testing`](.agents/skills/behavioral-testing/SKILL.md).**
Detalle de cobertura en [docs/testing.md](docs/testing.md).

```bash
pip install -r requirements-dev.txt
pytest
```

CI: `.github/workflows/ci.yml` corre `pytest` en cada push/PR.
Pendiente para un segundo pase: `playCommand`, `apiServer`, `userbot` y extender
`soundpadCommand`.

## 📜 Doc generation
Sphinx + napoleon recomendado. Ver [docs/contributing-docs.md](docs/contributing-docs.md).

## 💡 Guía de Modificación
1. **Tests primero (o junto al cambio):** seguí la skill `behavioral-testing`. No
   marques una tarea como completa sin tests verdes.
2. **Mantener DAVE patch:** No eliminar el patch en `userbot/bot.py` salvo que
   haya cambios claros en la API de Discord.
3. **Config y .env:** Toda nueva variable de entorno debe documentarse en
   `docs/configuration.md` (y `.env.example` si aplica).
4. **Docs primero:** Mantener `README.md` y los docs alineados si cambia la
   arquitectura o comandos.

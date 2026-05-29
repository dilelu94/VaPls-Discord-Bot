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
- `/indio`: persona con memoria corta.
- `/parar`: detiene playback y desconecta.
- `/quit`: desconecta sin limpiar cola.

## 🧪 Pruebas Unitarias
```bash
python3 -m unittest tests/testSoundpad.py
```

## 📜 Doc generation
Sphinx + napoleon recomendado. Ver [docs/contributing-docs.md](docs/contributing-docs.md).

## 💡 Guía de Modificación para Gemini
1. **Mantener DAVE patch:** No eliminar el patch en `userbot/bot.py` salvo que
   haya cambios claros en la API de Discord.
2. **Config y .env:** Toda nueva variable de entorno debe documentarse en
   `docs/configuration.md` (y `.env.example` si aplica).
3. **Docs primero:** Mantener `README.md` y los docs alineados si cambia la
   arquitectura o comandos.

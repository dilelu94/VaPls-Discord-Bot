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

## ✅ Definition of Done

Antes de dar por terminado **cualquier cambio**, todo agente (Claude, Gemini,
Codex, Copilot, u otro) DEBE cumplir esta checklist sin excepciones:

- [ ] Ejecutar `make check` (o `python -m pytest -q`) en el entorno local.
- [ ] El resultado debe ser **100 % verde** — cero errores, cero fallos.
- [ ] Los tests deben pasar sobre el Python local del agente antes de declarar
      el trabajo completo.
- [ ] No marcar una tarea como terminada si hay tests rojos, aunque el cambio
      parezca trivial.

**Hook de git (pre-push):** el repositorio incluye `.githooks/pre-push`, que
bloquea el `git push` automáticamente si la suite está roja. Activalo una sola
vez por clon:

```bash
git config core.hooksPath .githooks
```

Sin esto el hook no corre. CI es la última barrera, pero el hook local evita
que un cambio roto llegue siquiera al servidor remoto.

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
- **Main bot**: comandos, playback de audio, soundpad, Gemini, HTTP API y puente con Telegram.
- **Userbot** (alias *Indio*): transcripción de voz (DAVE/E2EE) con `faster-whisper` y captura de voice-reply para Telegram.

## 🌐 Servidor de producción (2026-05-30)
| | |
|---|---|
| **Host** | `ubuntu@141.148.84.55` |
| **OS** | Ubuntu 22.04 aarch64 |
| **Shape** | Oracle VM.Standard.A1.Flex — 4 OCPU / 24 GB RAM / 4 Gbps NIC |
| **SSH key (local)** | `/var/home/dilelu/.ssh/vapls` |
| **Repo path** | `/home/ubuntu/vapls-discord-bot/` |
| **Services** | `discord-bot.service` (main bot) + `vapls-userbot.service` (userbot) |

**Migrado desde** `ubuntu@129.80.59.99` (Oracle Linux amd64, instancia E2.1.Micro) el 2026-05-30. El server viejo quedó wipe-eado del bot pero sigue prendido para otros usos.

**Deploy workflow (automático):** push a `master` → CI (Python 3.10, la versión
de prod) → job `deploy` que SSHea al server y corre `scripts/deploy.sh` (`git reset --hard
origin/master`, reinstala deps si cambiaron, reinicia ambos servicios y verifica
que queden `active`). El server es un **pure deploy target**: no editar archivos
a mano ahí. Detalle completo en [docs/operations.md](docs/operations.md#cicd-pipeline).

**Deploy manual (fallback):**
```bash
rsync -avz -e "ssh -i /var/home/dilelu/.ssh/vapls" \
  <archivos cambiados> ubuntu@141.148.84.55:/home/ubuntu/vapls-discord-bot/
ssh -i /var/home/dilelu/.ssh/vapls ubuntu@141.148.84.55 \
  'sudo systemctl restart discord-bot.service'  # + vapls-userbot.service si cambió userbot/
```

## 🛠️ Stack Tecnológico y Dependencias
- **Lenguaje:** Python 3.10+
- **Discord bot:** `py-cord`
- **Userbot:** `discord.py-self` + `discord-ext-voice-recv`
- **STT:** `faster-whisper` (CTranslate2, offline, modelo `small` int8 en el server ARM 4/24)
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
3. Ejecuta `faster-whisper` (modelo `small`, `int8`, CPU threads = vCPU count) y genera texto final.
4. `on_transcript` publica en un canal de texto y/o forwardea por HTTP al main bot (`/indio` para wake word, opcional `/message` con `ENABLE_HTTP_FORWARD`).

### 3) Playback de música (GuildPlayer)
`/play` descarga con yt-dlp, reproduce con FFmpeg y mantiene cola/estado por
guild con pre-descarga en segundo plano.

## 📡 Integración con el bot de Telegram

El **main bot expone una HTTP API** en `127.0.0.1:8080` (loopback, protegida por header `X-API-Secret`) que un bot de Telegram externo usa como puente. Endpoints relevantes:
- `POST /message` — publica texto en un canal de Discord.
- `POST /play-audio` — descarga un audio de Telegram y lo reproduce en el canal de voz de Discord. Auto-elige canal (preferencia: donde está el Indio; fallback: el más poblado). Acepta un `replyCallbackUrl` opcional.
- `POST /indio` — invoca al Indio con una pregunta (memoria corta + lore destilado).
- `GET /status` `GET /members` `GET /user/{id}` `GET /queue` `GET /playing` — read-only.

**Voice-reply flow (Discord → Telegram):**
1. Telegram bridge llama `POST /play-audio` con `replyCallbackUrl=<url Telegram>`.
2. Main bot reproduce el audio en el canal de voz.
3. Al terminar, si `USERBOT_RECORD_URL` está seteado, el main bot le pide al **userbot** (loopback `POST 127.0.0.1:8081/record`) que grabe hasta `USERBOT_RECORD_DEFAULT_DURATION` segundos del mismo canal.
4. El userbot capta el audio (con VAD por RMS), lo encodea en OGG y hace `POST replyCallbackUrl` con el blob.
5. El bot de Telegram lo recibe y lo publica del lado Telegram.

**Indio relay (texto del Indio → Telegram via userbot):**
Cuando el main bot quiere que la respuesta del `/indio` salga con la identidad del **userbot real** (no con la del bot vapls), llama `POST 127.0.0.1:8081/say` del userbot con `INDIO_RELAY_SECRET`. Útil para que las respuestas autom. en chat parezcan venir del Indio "real".

## 🛠️ Comandos de Discord (Slash Commands)
- `/play`: reproduce música de YouTube.
- `/soundpad`: panel de clips locales.
- `/vapls`: respuestas Gemini sin memoria.
- `/indio`: persona con memoria corta por guild + memoria de largo plazo destilada por Gemini.
- `/parar`: detiene playback y desconecta.
- `/quit`: desconecta sin limpiar cola.

## 📁 lsyncd (sync local PC → soundpad del server)

El usuario corre **lsyncd** en su PC para mantener `audio_output/` del server espejado con `/home/dilelu/repos/RVC_WebUI/Output/` (donde RVC genera clips de voz). Sin esto, los clips nuevos no llegan al soundpad.

**Config (local, gitignored):** dos archivos en la raíz del repo, ambos con paths absolutos del usuario:
- `lsyncd_rvc.lua` — config de lsyncd (source/target/ssh-key).
- `lsyncd-rvc.service` — systemd user unit que corre `lsyncd -nodaemon lsyncd_rvc.lua`.

```lua
-- lsyncd_rvc.lua
source = "/home/dilelu/repos/RVC_WebUI/Output/"
host = "ubuntu@141.148.84.55"
targetdir = "/home/ubuntu/vapls-discord-bot/audio_output/"
identityFile = "/var/home/dilelu/.ssh/vapls"
delete = true   -- borra del server lo que se borró en local
```

**Instalación en una PC fresh:** symlinkear el unit a `~/.config/systemd/user/` y habilitarlo. Linger se activa para que arranque al boot sin login.
```bash
ln -s "$(pwd)/lsyncd-rvc.service" ~/.config/systemd/user/lsyncd-rvc.service
loginctl enable-linger "$USER"   # solo la primera vez
systemctl --user daemon-reload
systemctl --user enable --now lsyncd-rvc.service
```

Comandos útiles:
```bash
systemctl --user status lsyncd-rvc.service
systemctl --user restart lsyncd-rvc.service
journalctl --user -u lsyncd-rvc.service -f
```

Si cambia el server / la key SSH, hay que editar `lsyncd_rvc.lua` y `systemctl --user restart lsyncd-rvc.service`.

## ⚠️ Errores conocidos y workarounds

### 1) `Client.__init__() missing 'intents'` en el userbot (provisión fresh)
`discord-ext-voice-recv` arrastra `discord.py` como dep transitiva y le gana el namespace `discord` a `discord.py-self` después de `pip install -r userbot/requirements.txt`. La versión nueva de `discord.py` requiere `intents=...`, lo cual rompe `discord.Client(chunk_guilds_at_startup=False)` en `userbot/bot.py`.

**Workaround (ya bakeado en `deploy.sh`):**
```bash
pip install -r userbot/requirements.txt
pip uninstall -y discord.py 2>/dev/null || true
pip install --force-reinstall --no-deps \
  "discord.py-self[voice] @ git+https://github.com/dolfies/discord.py-self"
```
Si encontrás el error a futuro: re-correr los 3 comandos en el venv del userbot.

### 2) Modelo `faster-whisper` se descarga en el primer arranque
La primera vez que `vapls-userbot.service` levanta en un server fresh, baja el modelo (`Systran/faster-whisper-<size>`) de HuggingFace — agrega ~30-60s al startup. Cachea en `~/.cache/huggingface/` (o `WHISPER_CACHE_DIR` si está seteado).

### 3) DAVE patch en el userbot
El userbot monkey-patchea `PacketDecryptor._decrypt_rtp_*` para aplicar `dave.decrypt()` después del AEAD. **No eliminar** salvo cambio claro en la API de Discord (logs: `"DAVE decrypt monkey-patch installed"`). Si Discord cambia el protocolo DAVE, el userbot queda recibiendo audio cifrado que no puede transcribir.

### 4) `audio_output/` no está en el repo
El soundpad usa `audio_output/` (configurable con `CUSTOM_AUDIO_PATH`). En el server lo poblá lsyncd (ver sección 📁). En clon nuevo, el directorio queda vacío hasta que lsyncd corra desde tu PC.

### 5) FFmpeg estático amd64 vs arm64
El branch `dnf` de `deploy.sh` baja un binario estático según `uname -m` (`amd64` o `arm64`). En Ubuntu/Debian usa `apt install ffmpeg` directamente. Si agregás soporte para otra arch, actualizá ambos branches.

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

CI: `.github/workflows/ci.yml` corre `pytest` sobre **Python 3.10** (la única
versión soportada en prod — Ubuntu 22.04; ver [docs/operations.md](docs/operations.md#server))
en cada push/PR, y deploya a producción al pasar (ver sección de servidor +
[docs/operations.md](docs/operations.md#cicd-pipeline)).
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

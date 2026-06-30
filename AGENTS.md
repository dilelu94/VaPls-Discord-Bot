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
- [Observabilidad y Logs](docs/observability.md)
- [Contribución y docstrings](docs/contributing-docs.md)

## 📋 Descripción General

**VaPls-Discord-Bot** corre en dos procesos:

- **Main bot**: comandos, playback de audio, soundpad, Gemini, HTTP API y puente con Telegram.
- **Userbot** (alias _Indio_): transcripción de voz (DAVE/E2EE) con `faster-whisper` y captura de voice-reply para Telegram.

## 🛠️ Principios de Programación de Lógica

- **Centralización en "vapls":** Los comandos y la lógica de interacción principal deben programarse siempre en el **Main bot** ("vapls").
- **El Userbot es un usuario:** El _Indio_ (userbot) debe ser tratado como un usuario más. Si se le pide realizar una tarea, debe invocar los comandos de "vapls" programáticamente mediante una función que ejecute el comando slash con sus argumentos (ya que no puede usar comandos slash literales por texto).
- **Lógica mínima en Userbot:** No se debe programar lógica en el userbot a menos que sea estrictamente necesario por limitaciones técnicas o para su funcionamiento como IA que simula ser una persona real (ej. lógica de personalidad, comportamiento humano o integraciones que no puedan delegarse al bot "vapls").

## 🌐 Servidor de producción (2026-05-30)

|                     |                                                                      |
| ------------------- | -------------------------------------------------------------------- |
| **Host**            | `ubuntu@141.148.84.55`                                               |
| **OS**              | Ubuntu 22.04 aarch64                                                 |
| **Shape**           | Oracle VM.Standard.A1.Flex — 4 OCPU / 24 GB RAM / 4 Gbps NIC         |
| **SSH key (local)** | `/var/home/dilelu/.ssh/vapls`                                        |
| **Repo path**       | `/home/ubuntu/vapls-discord-bot/`                                    |
| **Services**        | `discord-bot.service` (main bot) + `indio-userbot.service` (userbot) |

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
  'sudo systemctl restart discord-bot.service'  # + indio-userbot.service si cambió userbot/
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
- `/entraindio`: hace que el userbot (Indio) entre al canal de voz del invocador (relay `/join`).
- `/sensibilidad` `1|2|3`: cambia la sensibilidad del wake-word del Indio en caliente (ver abajo).
- `/stream <canal>`: busca en iptv-org y transmite en Go Live dentro del canal de voz del invocador. Requiere el proceso `golive/bot.py` corriendo.
- `/stopstream`: detiene el stream activo en el servidor.
- `/banana` (Pausado/Inactivo): genera una imagen con Gemini (gratis, sin API key, usando Playwright). Actualmente en pausa por bloqueos de autenticación de Google.

## 📺 GoLive / IPTV (`/stream`)

El comando `/stream` busca canales en el playlist público de [iptv-org](https://github.com/iptv-org/iptv)
y los transmite por Go Live dentro del canal de voz del invocador.

### Arquitectura (3 procesos)

```
Usuario en Discord
  └── /stream <canal>
        ↓
  [main bot — bot.py]
  1. iptv.py busca en https://iptv-org.github.io/iptv/index.m3u (10 950+ canales)
  2. Si hay exactamente 1 resultado → POST http://<GOLIVE_RELAY_URL>/stream
        ↓
  [golive/bot.py — proceso separado con user token de Discord]
  3. Se une al canal de voz del invocador
  4. Establece conexión Go Live (screenshare) dedicada via GoLiveConnection (WebSocket/UDP secundario)
     a. Envía op 18 (STREAM_CREATE) y op 22 (STREAM_SET_PAUSED) en la gateway principal
     b. Obtiene credenciales y abre ws + socket UDP dedicados para el streaming
  5. Instancia VideoStream(url, conn)
        ↓
  [golive/streamer.py — VideoStream]
  6. Registra el video SSRC en la MLS DAVE session
  7. FFmpeg: URL HLS/IPTV → -c:v libx264/nvenc/vaapi → raw H.264 Annex-B pipe
  8. Parsea NAL units y reescribe SPS VUI
  9. Cifrado E2EE (DAVE) via `dave_session.encrypt_h264` + cifrado de transporte RTP (XSalsa20/XChaCha20)
  10. Envía paquetes RTP al UDP socket de la GoLiveConnection
```

### Señalización WebSocket (video_compat.py)

Sin estos patches Discord descarta silenciosamente todos los paquetes RTP de video:

- **`identify()`** — agrega `video: true` + `streams` descriptor al op `IDENTIFY` (op 0)
- **`select_protocol()`** — agrega codec H264 al `SELECT_PROTOCOL` (op 1)
- **`client_connect()`** — anuncia `video_ssrc` + `streams` al op `VIDEO` (op 12)

Se aplican en `golive/bot.py` antes de cualquier conexión de voz:

```python
vc.patch_video(discord.gateway)
```

### Archivos clave

| Archivo                       | Rol                                                                                                                          |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `bot.py`                      | Slash commands `/stream` y `/stopstream` (L1589–1739)                                                                        |
| `iptv.py`                     | Descarga/cachea el M3U de iptv-org, busca canales por nombre. Cache en `data/iptv_cache.m3u`, TTL 6h                         |
| `golive/bot.py`               | Proceso GoLive: relay HTTP (`POST /stream`, `POST /stopstream`), join de canal, instancia `GoLiveConnection` y `VideoStream` |
| `golive/davey_compat.py`      | Shim de compatibilidad para DAVE E2EE que envuelve a `dave.py` (DisnakeDev libdave)                                          |
| `golive/golive_connection.py` | Conexión Go Live secundaria: WebSocket de streaming, UDP socket, handshake y control                                         |
| `golive/streamer.py`          | FFmpeg → H.264 Annex-B → Cifrado DAVE + RTP encriptado → UDP de GoLiveConnection. SPS VUI rewriter                           |
| `golive/video_compat.py`      | Patches al gateway WS de discord.py-self para anunciar capacidad de video                                                    |
| `golive/config.py`            | `GOLIVE_TOKEN`, `GOLIVE_RELAY_SECRET`, `GOLIVE_RELAY_PORT` (default 8082)                                                    |

### Config necesaria

En el `.env` del **main bot**:

```env
GOLIVE_RELAY_URL=http://127.0.0.1:8082
GOLIVE_RELAY_SECRET=<mismo secret que el golive bot>
GOLIVE_RELAY_TIMEOUT=30
```

En `golive/.env`:

```env
GOLIVE_TOKEN=<user token de la cuenta Discord usada para Go Live>
RELAY_SECRET=<mismo secret que arriba>
RELAY_HOST=127.0.0.1
RELAY_PORT=8082
YT_DLP_POT_BASE_URL=http://127.0.0.1:4416  # (Requerido para YouTube) Proveedor anti-bloqueo PoT
```

### Búsqueda de canales

`iptv.py` hace substring match case-insensitive sobre el nombre del canal. Si hay más de 1 resultado, el bot pide más precisión. Ejemplos que dan exactamente 1 resultado:

- `Al Jazeera English`
- `France 24 English`
- `CGTN Español`
- `1TV` (canal georgiano público)

### Streams de prueba verificados (funcionales al 2026-06-20)

Estos URLs responden HTTP 200 y son legibles por ffmpeg desde el server de prod:

| Canal (nombre en M3U)      | URL                                                                           |
| -------------------------- | ----------------------------------------------------------------------------- |
| Al Jazeera English (1080p) | `https://live-hls-apps-aje-fa.getaj.net/AJE/index.m3u8`                       |
| France 24 Arabic (1080p)   | `https://live.france24.com/hls/live/2037222-b/F24_AR_HI_HLS/master_5000.m3u8` |
| CGTN (1080p)               | `https://amg00405-rakutentv-cgtn-rakuten-i9tar.amagi.tv/master.m3u8`          |
| 1TV Georgia (720p)         | `https://tv.cdn.xsg.ge/gpb-1tv/index.m3u8`                                    |
| 2M Maroc (1080p)           | `https://stream-lb.livemediama.com/2m-tnt/hls/master.m3u8`                    |

### Detección de encoder (`_detect_encoder`)

El streamer prueba encoders en orden: `h264_nvenc` → `h264_vaapi` → `libx264`.
**No usa `ffmpeg -encoders`** (que lista encoders compilados pero no verifica si el driver está disponible). En cambio, hace un encode real de 1 frame a `/dev/null` con `-f lavfi -i color=...`. Si el proceso falla (ej. `h264_nvenc` sin CUDA), pasa al siguiente. El encoder elegido se loggea al arrancar:

```
golive: encoder probe OK → libx264
```

### Bugs conocidos (historial)

1. **(2026-06-20) `h264_nvenc` seleccionado en server ARM sin CUDA**: `_detect_encoder()` usaba `ffmpeg -encoders` para detectar disponibilidad, pero el encoder figura como compilado incluso si libcuda no está disponible. El encode fallaba silenciosamente (FFmpeg salía con rc=1 inmediatamente) y el `send_loop` terminaba en ~1s sin emitir frames. **Fix**: `_detect_encoder()` ahora prueba cada encoder con un encode real de 1 frame. Ver `golive/streamer.py`.

2. **(2026-06-20) `send_loop` terminaba en el primer timeout con streams HLS**: streams HLS con múltiples renditions tardan 4-8s en probe antes de emitir el primer frame. El `asyncio.wait_for` con `timeout=2.0` expiraba, y en condiciones de race el loop detectaba `returncode is not None` (del encoder fallido) y abortaba. **Fix**: el primer `read()` tiene un timeout de 15s (`first_read=True`); los subsiguientes mantienen 2s.

3. **(2026-06-21) `/stream` con videos de YouTube (VODs) — troubleshooting histórico**:
   - _Causa original_: Faltaba configurar `YT_DLP_POT_BASE_URL` en `golive/.env`. YouTube exige PoT en IPs de Oracle.
   - **Fix inicial**: Se agregó `YT_DLP_POT_BASE_URL` y se cambió format a `"bestvideo[height<=1080][fps<=60]+bestaudio/best"` para 1080p60.
   - **Problemas secundarios**: `bestvideo` elegía AV1 (sin decoder en ARM), y devolvía tuple de URLs (video+audio separados) que el streamer no manejaba. Además, `-http_persistent 0` estaba aplicado a URLs YouTube y fallaba con "Option not found" en direct MP4 de googlevideo.com.
     - **Fix final (a0b0e2e)**: `"format": "best"` (combined H264, single URL, max 720p) y `-http_persistent 0` solo para URLs HTTP que NO sean googlevideo.com (IPTV sí, YouTube no).

## 📱 Instagram Reels Streaming (`/instagram`) [PENDIENTE]

El comando `/instagram` **aún no está implementado**. El diseño está documentado
acá para cuando se disponga de una cuenta de Instagram para el userbot GoLive.

### Estado

- **Implementación**: PENDIENTE — requiere crear una cuenta de Instagram para el userbot.
- **Diseño**: completo, documentado a continuación.

### Arquitectura planeada (3 procesos)

```
Usuario en Discord
  └── /instagram
        ↓
  [main bot — bot.py]
  1. Crea sesión de streaming con tipo "instagram"
  2. POST al relay del GoLive con tipo "instagram"
  3. Espera confirmación
        ↓
  [golive/bot.py — proceso separado con user token de Discord]
  4. Recibe el POST en relay HTTP (127.0.0.1:8082)
  5. Inicia GoLiveConnection (screenshare en Discord)
  6. Instancia InstagramFeedPlayer
        ↓
  [golive/instagram_feed.py + golive/instagram_streamer.py] (NUEVOS)
  7. InstagramFeed.login() via instagrapi con sesión persistente
  8. InstagramFeed.fetch() obtiene feed de reels (timeline / For You)
  9. Por cada reel: yt-dlp extrae URL directa .mp4
  10. FFmpeg decode → H.264 → DAVE encrypt → RTP → Discord GoLive
  11. Avanza al siguiente cuando termina (cola infinita)
  12. Cuando la cola se vacía → fetch() más reels
```

### Loop infinito

- `InstagramFeed` pre-fetch asíncrono: mantiene cola de 10+ reels.
- Cuando el reel actual termina, avanza al siguiente sin cortar el GoLive.
- Si la cola se vacía, fetchea más del feed automáticamente.
- El userbot nunca desconecta hasta `/stopstream`.

### Orientación vertical

Los reels son verticales (1080×1920). Se muestran con barras negras laterales
usando scale + pad en FFmpeg:

```
scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black
```

### Archivos planeados

| Archivo                        | Rol                                                                                       |
| ------------------------------ | ----------------------------------------------------------------------------------------- |
| `instagramCommand.py`          | **Nuevo.** Comando `/instagram` + view de control                                         |
| `golive/instagram_feed.py`     | **Nuevo.** Login Instagram (instagrapi), fetch feed, cola de reels, ciclo infinito        |
| `golive/instagram_streamer.py` | **Nuevo.** `InstagramReelPlayer` adapta `H264VideoPlayer` con loop + orientación vertical |
| `golive/streamer.py`           | **Modificar.** Agregar resolución vertical con `pad` (barras negras)                      |
| `golive/bot.py`                | **Modificar.** Endpoint `/instagram` en relay + orquestación de `InstagramReelPlayer`     |
| `golive/ytdlp.py`              | **Modificar.** Asegurar extracción de Instagram Reels con cookies                         |
| `bot.py`                       | **Modificar.** Registrar `/instagram` slash command                                       |
| `golive/requirements.txt`      | **Modificar.** Agregar `instagrapi`                                                       |

### Config necesaria (pendiente)

En `golive/.env`:

```env
INSTAGRAM_USER=cuenta_del_userbot
INSTAGRAM_PASS=contraseña
INSTAGRAM_SESSION_FILE=data/instagram_session.json
```

### Prerequisitos para implementar

1. Crear una cuenta nueva de Instagram para el userbot.
2. Seguir cuentas que posteen reels seguido para poblar el feed.
3. Configurar credenciales en `golive/.env`.
4. Probar que instagrapi funciona desde la IP de Oracle Cloud (riesgo de bloqueo).
5. Si el login directo falla, alternativa: cookies exportadas de Chrome.

### Riesgos conocidos

| Riesgo                               | Mitigación                                                                          |
| ------------------------------------ | ----------------------------------------------------------------------------------- |
| Instagram bloquea IP de Oracle Cloud | Probar en etapa de implementación. Sin fallback a URLs por ahora.                   |
| `instagrapi` incompatible con ARM64  | Es pure Python + dependencias comunes (Pillow, requests), debería funcionar.        |
| Instagram pide 2FA/challenge         | `instagrapi` tiene manejadores de challenge; si no alcanza, resolver manual.        |
| Sesión expira y no se re-loguea      | `INSTAGRAM_SESSION_FILE` persiste en disco; el bot refresca al detectar expiración. |

## 🎚️ Sensibilidad del wake-word (presets VOSK)

El detector de wake-word del userbot corre VOSK con una **gramática restringida**
(`KaldiRecognizer` con una lista cerrada de frases). Mecanismo clave: VOSK
_colapsa_ todo el audio hacia la entrada más parecida de esa lista. Si la lista
es muy chica, el ruido y la charla ambiente se fuerzan hacia las frases de
wake-word → falsos positivos. Si la lista es más grande (más "decoys": muletillas,
interrogativos, artículos, verbos comunes), VOSK tiene dónde mandar ese audio y
dispara menos. Dos palancas definen cada preset: **(a)** el set de pares de
wake-word que cuentan como hit (`_WAKE_PATTERNS` / `_active_wake_patterns`) y
**(b)** el pool de frases del grammar (`_build_vosk_grammar`).

Se cambia con `/sensibilidad` (main bot) o `POST /sensibilidad` (relay del
userbot). **El preset es in-memory y se resetea al default (4) al reiniciar el
userbot.** Implementación en `userbot/bot.py` (`_PRESETS`, `_build_vosk_grammar`,
`_set_sensitivity`, `_active_wake_patterns`); comando en `bot.py`.

| Preset          | Invocación                                                                          | Grammar pool                                                                                      | Idea                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1**           | `che/que/eh indio` + verbos                                                         | chico (solo wake-words + decoys mínimos)                                                          | El más sensible.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **2**           | solo `che indio` + verbos                                                           | chico                                                                                             | Saca `que`/`eh` (principal fuente de falsos positivos: `que` es palabra comunísima que VOSK confunde con `che`).                                                                                                                                                                                                                                                                                                                             |
| **3**           | `che/que/eh indio` + verbos                                                         | **grande** (muletillas, interrogativos, artículos, pronombres, verbos comunes — el pool original) | Menos sensible vía pool grande, pero re-habilita `eh/que indio`. Pensado para **editar a mano** las wake-words según lo que VOSK vaya escuchando mal.                                                                                                                                                                                                                                                                                        |
| **4** (default) | solo `che indio` + verbos (igual que preset 2), pero single-best + `SetWords(True)` | chico (igual que preset 2)                                                                        | Segunda capa post-VOSK: usa los **timestamps por palabra** de VOSK para cortar el audio EXACTO de la palabra `indio` (desde el buffer del segmento), y corre un pase corto de Whisper (`_run_whisper_wake`) solo sobre ese recorte. Si Whisper no confirma `indio`, descarta sin transcribir el comando. Aísla la wake word sin importar dónde dispare VOSK (vs. el prebuffer fijo, que fallaba cuando VOSK disparaba tarde sobre el verbo). |

**Tuning manual del preset 3:** los bloques de wake-words y filler en
`userbot/bot.py` están marcados para editarse a mano. Cuando VOSK colapse mal una
frase y dispare un falso positivo, se agrega esa frase al pool de filler (para que
tenga dónde caer) o se ajusta `_WAKE_PATTERNS`. Los logs `[WAKE]`/`[VOSK]`
imprimen el texto que VOSK escuchó en cada disparo para guiar ese ajuste.

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

## 🖼️ Generación de imágenes (/generarimagen y /banana)

Hay dos comandos para generar imágenes de manera gratuita:

1. **`/generarimagen` (Hugging Face)**:
   Usa la **Hugging Face Inference API** (free tier). El módulo que lo implementa es `huggingfaceImage.py`.
   **Setup:**
   1. Crear cuenta en https://huggingface.co/join
   2. Ir a Settings → Access Tokens → "New token" (tipo **read**)
   3. Poner el token en `.env`: `HUGGINGFACE_API_TOKEN=tu_token`

2. **`/banana` (Playwright / Gemini web UI) [EN DESUSO / INACTIVO]**:
   Genera imágenes usando automatización de navegador con Playwright conectándose a la UI de Gemini. Este comando ha quedado en desuso por un tiempo debido a los constantes bloqueos de seguridad de Google a cuentas automatizadas y las restricciones de login. El código se encuentra extraído y comentado en `geminiImage_legacy.py` y `geminiImage.py`.
   **Setup (si se llega a reactivar en el futuro):**
   1. Ejecutar `python setup_gemini_session.py` para abrir Chromium en modo interactivo, logearse con una cuenta de Google en Gemini y crear el archivo `/tmp/gemini_ready` para guardar la sesión en `gemini_auth.json`.

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

La primera vez que `indio-userbot.service` levanta en un server fresh, baja el modelo (`Systran/faster-whisper-<size>`) de HuggingFace — agrega ~30-60s al startup. Cachea en `~/.cache/huggingface/` (o `WHISPER_CACHE_DIR` si está seteado).

### 3) DAVE patch en el userbot

El userbot monkey-patchea `PacketDecryptor._decrypt_rtp_*` para aplicar `dave.decrypt()` después del AEAD. **No eliminar** salvo cambio claro en la API de Discord (logs: `"DAVE decrypt monkey-patch installed"`). Si Discord cambia el protocolo DAVE, el userbot queda recibiendo audio cifrado que no puede transcribir.

### 4) `audio_output/` no está en el repo

El soundpad usa `audio_output/` (configurable con `CUSTOM_AUDIO_PATH`). En el server lo poblá lsyncd (ver sección 📁). En clon nuevo, el directorio queda vacío hasta que lsyncd corra desde tu PC.

### 5) FFmpeg estático amd64 vs arm64

El branch `dnf` de `deploy.sh` baja un binario estático según `uname -m` (`amd64` o `arm64`). En Ubuntu/Debian usa `apt install ffmpeg` directamente. Si agregás soporte para otra arch, actualizá ambos branches.

### 6) `h264_nvenc` seleccionado en server ARM sin CUDA (`golive`)

`_detect_encoder()` originalmente usaba `ffmpeg -encoders` para detectar encoders disponibles. El encoder `h264_nvenc` aparece listado aunque no haya GPU/CUDA, porque está compilado en la librería. Al intentar usarlo, FFmpeg falla con `Cannot load libcuda.so.1` y sale con rc=1 inmediatamente — el `send_loop` termina en ~1s sin emitir ningún frame y el stream no aparece en Discord.

**Fix (en `golive/streamer.py`)**: `_detect_encoder()` ahora hace un encode de prueba real (1 frame, `-f lavfi -i color=...`, salida a `pipe:null`) para cada candidato. Si falla con `CalledProcessError`, pasa al siguiente. En el server ARM, `h264_nvenc` y `h264_vaapi` fallan y se usa `libx264`.

### 7) yt-dlp y FFmpeg sin soporte MP3

El binario de FFmpeg customizado/estático en el server de producción no incluye el encoder `libmp3lame`. Si se le pide a `yt-dlp` que post-procese audios descargados a formato MP3 (`--audio-format mp3`), va a fallar silenciosamente o tirar el error `Encoder not found`.
**Workaround:** `playCommand.py` y el bot en general usan `--audio-format opus` y manipulan archivos `.opus` en el código, que sí tiene soporte nativo en el ffmpeg del server y además no requiere re-codificar (YouTube entrega Opus de forma nativa). ¡No intentar usar MP3!

## 🧪 Testing

Los tests viven en `tests/` y corren con **pytest** (+ `pytest-asyncio`). La
filosofía es testear _comportamiento observable_, no detalle de implementación:
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

## 💡 Sistema de Sugerencias (/sugerencias)

Integración con GitHub Issues para rastrear y gestionar features pedidas por los usuarios.

### Flujo

1. User ejecuta `/sugerencias <idea>`
2. **Gemini Flash-Lite** clasifica: encaja en grupo existente o crea uno nuevo
3. Se persiste en disco (`data/suggestions.json`, en `.gitignore`)
4. Se crea/comenta el issue de GitHub:
   - **Grupo nuevo** → `POST /repos/{repo}/issues` con label `sugerencia`
   - **Match existente** → `POST /repos/{repo}/issues/{n}/comments` con +1, y opcional `PATCH` del body si Gemini detectó info nueva
5. Reply ephemeral al usuario (`ctx.followup.send`)

### Sincronización con GitHub Issues (cerrar/reabrir)

Cuando un issue se cierra, el grupo local se **oculta** (`hidden: True`) en vez de borrarse. Si se reabre, el grupo se restaura automáticamente. Dos mecanismos:

- **Startup sync**: `on_ready` llama `sync_closed_issues()` que consulta `GET /repos/{repo}/issues?state=closed` y oculta los grupos cuyo `issue_number` esté en la respuesta.
- **Webhook en tiempo real**: `POST /github-webhook` maneja eventos:
  - `action=closed` → oculta el grupo (`hidden: True`)
  - `action=reopened` → restaura el grupo (`hidden: False`)
  - Ignora issues sin la label configurada (`GITHUB_ISSUE_LABEL`)

### Archivos clave

| Archivo                 | Rol                                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------------------- |
| `suggestionsCommand.py` | Toda la lógica: modelo (`Group`, `Submission`), store, clasificación, GitHub sync                               |
| `githubIssues.py`       | Cliente asincrónico de la GitHub REST API (`create_issue`, `add_comment`, `update_issue`, `list_closed_issues`) |
| `bot.py:224-234`        | `on_ready`: auto-migrate + auto-sync al arrancar                                                                |
| `apiServer.py:808-839`  | Endpoint `/github-webhook`                                                                                      |

### Modelo de datos (`data/suggestions.json`)

```json
{
  "groups": [
    {
      "id": "g_<uuid4-short>",
      "title": "Comando para X",
      "summary": "Que el bot haga Y cuando Z",
      "created_at": "2026-05-30T12:34:56Z",
      "updated_at": "2026-05-30T12:34:56Z",
      "issue_number": 49,
      "hidden": true,
      "submissions": [
        {
          "user_id": "123",
          "user_name": "Mati",
          "text": "podrias agregar Y para que pase Z",
          "at": "2026-05-30T12:34:56Z"
        }
      ]
    }
  ]
}
```

El campo `completed` existe en el dataclass para backward compat pero **ya no se escribe** en nuevos archivos. El campo `hidden` se escribe solo cuando es `True` (grupo oculto por issue cerrado). Los grupos ocultos:

- No aparecen en `/sugerencias-ver`
- No se pasan al clasificador de Gemini (no se pueden matchear sugerencias nuevas contra ellos)
- Se restauran automáticamente si el issue se reabre

### Config necesaria (`.env`)

| Variable             | Ejemplo                      | Descripción                               |
| -------------------- | ---------------------------- | ----------------------------------------- |
| `GITHUB_TOKEN`       | `ghp_...`                    | Token de GitHub con permisos de issues    |
| `GITHUB_REPO`        | `dilelu94/VaPls-Discord-Bot` | Repo dueño/nombre                         |
| `GITHUB_ISSUE_LABEL` | `sugerencia`                 | Label para identificar issues del sistema |

### Infraestructura webhook

El endpoint `/github-webhook` está protegido por el middleware `X-API-Secret` como todos los endpoints del API. Para que GitHub pueda llegar sin conocer el secret, hay un **nginx** como reverse proxy:

- **Nginx** en el puerto 80 (`0.0.0.0:80`)
- Proxy reverso a `127.0.0.1:8080` (el API del bot)
- Solo para `/github-webhook` inyecta el header `X-API-Secret`
- Los demás endpoints pasan sin modificación (cada cliente debe enviar su propio secret)

**Firewall**: el server corre en Oracle Cloud. Además de iptables (puertos 22, 80, 443 abiertos), hay que agregar una regla **Ingress** en la **Security List** de la VCN para puerto 80.

### Estado actual en producción

- Grupos migrados a issues **#49–#57** (todos con label `sugerencia`)
- Auto-sync corre en cada reinicio del bot
- Nginx en puerto 80 como reverse proxy para webhook (Security List de Oracle Cloud abierta)
- Webhook de GitHub configurado para eventos `issues` → `http://141.148.84.55/github-webhook`

## 📊 Admin page MMR

La página de admin en `http://141.148.84.55/admin` muestra datos de MMR,
weights, config y activity. Se compone de:

- **`_ADMIN_HTML`** (en `apiServer.py`): template HTML con JavaScript inline.
  Usa placeholders `/*AUTH*/` y `/*DATA*/` que el servidor reemplaza.
- **`_checkAdminAuth()`**: Basic Auth contra `config.ADMIN_USER`/`config.ADMIN_PASS`.
- **`adminPage()`**: obtiene datos del relay server-side, embebe auth+data en HTML.
- **`adminData()` / `adminWeights()`**: endpoints REST proxy al userbot relay.

### Bugs encontrados y fixes

1. **URL joining** (`e1e9a22`): `relay.rstrip("/") + path` → `urljoin(relay, path)`.
   `INDIO_RELAY_URL=http://127.0.0.1:8081/say` producía `/say/admin/api/data`
   en vez de `/admin/api/data`.

2. **Credenciales hardcodeadas** (`10e73d4`): `_ADMIN_AUTH_USER`/`_ADMIN_AUTH_PASS`
   → `config.ADMIN_USER`/`config.ADMIN_PASS` (defaults `dilelu`/`indiovapls`).

3. **Auth en JS fetch** (`a1e1934`): `fetch('/admin/api/data')` no envía Basic Auth.
   Fix: servidor fetchea los datos server-side y los embebe como `var AUTH = ...`
   y `var allData = ...` en el HTML vía placeholders.

4. **JS SyntaxError por escapes** (`3504842`): Python `"""..."""` consume backslashes,
   `\'` → `'`. Fix: `\\'` para producir `\'` en el output JS.

5. **Tooltips + columna Name** (`08e650d`): `title` attributes en español en todas
   las celdas de las 4 tabs, y columna "Name" en tabla MMR.
6. **Tooltips descriptivos** (`902cdd5`): tooltips mucho mas detallados explicando
   cada columna y cada valor uno por uno.

## 🏆 Sistema MMR (Glicko-1)

### Base de datos (`userbot/activity_db.py`)

SQLite con 4 tablas:

- **`user_mmr`**: rating, deviation, volatility, total_activities, premium, por (user_id, guild_id).
- **`activity_log`**: cada evento individual con activity_type, duration_secs, quality_score, value, rating_delta.
- **`daily_stats`**: agregación diaria (voice_seconds, activity_count, mmr_delta, peak_rating).
- **`config`**: key-value store para pesos y configuración.

### Pesos default

| Actividad      | Peso |
| -------------- | ---- |
| voice_vad      | 0.4  |
| camera         | 0.8  |
| stream         | 1.5  |
| watch_stream   | 0.1  |
| message        | 0.3  |
| image          | 0.8  |
| file           | 0.6  |
| link           | 0.2  |
| sticker        | 0.05 |
| thread_post    | 1.5  |
| thread_create  | 5.0  |
| forum_post     | 2.0  |
| forum_create   | 8.0  |
| reaction       | 0.15 |
| slash_command  | 0.5  |
| event_create   | 6.0  |
| event_join     | 1.0  |
| channel_create | 5.0  |
| poll_create    | 3.0  |
| poll_vote      | 0.3  |

### Config default

| Key                  | Default | Efecto                                         |
| -------------------- | ------- | ---------------------------------------------- |
| initial_rating       | 1500    | Rating con el que arranca un usuario nuevo     |
| initial_deviation    | 350     | RD inicial (máxima incertidumbre)              |
| min_deviation        | 30      | Piso de RD (nunca baja de esto)                |
| max_deviation        | 500     | Techo de RD                                    |
| decay_per_day        | 10      | Cuánto sube la RD por día sin actividad        |
| decay_rating_per_day | 1       | Cuánto rating pierde por día sin actividad     |
| spam_window_seconds  | 10      | Ventana de tiempo para detectar spam           |
| spam_max_events      | 5       | Máximo de eventos del mismo tipo en la ventana |
| premium_multiplier   | 0.85    | Multiplicador de quality para usuarios premium |
| k_factor             | 1.0     | Escala del delta Glicko                        |

### Fórmula Glicko-1

1. `expected = _expected_score(r, rd)` — probabilidad de que el usuario "gane" la actividad (default vs system rating 1500/350).
2. `actual = 0.5 + (quality - 0.5) * weight_factor` — qué tan bien le fue, modulado por el peso de la actividad.
3. `delta = new_r - r` — ajuste Glicko-1 estándar con `g`, `d2`.
4. **Spam detection**: si hay más de `spam_max_events` del mismo tipo en `spam_window_seconds`, la calidad se reduce (0.5 → 0.3 → 0.1).
5. **Premium**: multiplica quality por `premium_multiplier` (0.85).
6. **Decay**: si pasó >1 día sin actividad, la RD sube y el rating converge a 1500.

### Cómo se disparan las actividades (`bot.py`)

Toda actividad se loggea vía `_log_activity()` que hace POST al relay del userbot (`/activity/log`):

| Evento                                      | Actividad                                         | Condición                                                           |
| ------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------- |
| Usuario habla en voz (whisper final)        | `voice_vad`                                       | Con duración, solo si hay ≥2 humanos en el canal                    |
| Usuario entra a voz                         | `voice_vad`                                       | Una vez, solo si hay ≥2 humanos                                     |
| Usuario se va de voz                        | `voice_vad`                                       | Con duración acumulada                                              |
| Usuario cambia de canal                     | `voice_vad`                                       | Duración parcial + nuevo evento                                     |
| Usuario prende cámara                       | `camera`                                          | Solo si hay ≥2 humanos                                              |
| Usuario empieza a streamear                 | `stream`                                          | Solo si hay ≥1 viewer; quality escala 0.4/0.6/0.8/1.0 según viewers |
| Viewer mirando stream                       | `watch_stream`                                    | Con duración, capped a 10 min/día                                   |
| Mensaje de texto                            | `message` / `link` / `sticker` / `image` / `file` | Según contenido                                                     |
| Creación de poll/channel/event/thread/forum | `poll_create` / `channel_create` / etc.           | Según tipo                                                          |
| Votar en poll                               | `poll_vote`                                       |                                                                     |
| Unirse a evento                             | `event_join`                                      |                                                                     |

### Stream cap

- **Sin viewers**: no se loggea (0 puntos).
- **Con viewers**: quality = `min(1.0, 0.2 + 0.2 * viewers)` (1 viewer=0.4, 2=0.6, 3=0.8, 4+=1.0).
- **Daily cap**: máximo `_STREAM_DAILY_MAX` (5) streams que puntúan por día por usuario.
- **Watch_stream**: capped a `_WATCH_DAILY_MAX` (600s = 10 min) por día por usuario.

### Relay HTTP endpoints (`userbot/bot.py`)

| Endpoint                | Método | Uso                                                   |
| ----------------------- | ------ | ----------------------------------------------------- |
| `/activity/log`         | POST   | Loggear una actividad (usado por main bot)            |
| `/activity`             | GET    | Listar actividades recientes (query: guild_id, limit) |
| `/activity/user`        | GET    | Stats de un usuario (query: user_id, guild_id)        |
| `/activity/leaderboard` | GET    | Top N por guild (query: guild_id, limit)              |
| `/admin`                | GET    | Admin page HTML (Basic Auth)                          |
| `/admin/api/data`       | GET    | JSON dump completo (Basic Auth)                       |
| `/admin/api/weights`    | POST   | Actualizar config/pesos (Basic Auth)                  |

### Proxy del main bot (`apiServer.py`)

- `GET /admin` → fetchea `relay/admin/api/data` server-side, enriquece cada row de mmr/activity con `user_name` y `user_display` desde `USERS` (cache de Discord del main bot), embehe todo en el HTML.
- `GET /admin/api/data` → proxy al relay (para refresh JS).
- `POST /admin/api/weights` → proxy al relay.

### Config

- `USERBOT_USER_ID` — excluye al Indio del conteo de ocupación en canales de voz
  para que su presencia no active actividades MMR que requieren ≥2 humanos.
  `_has_others()` en `bot.py` filtra `m.id != config.USERBOT_USER_ID`.
  Default: `0` (sin filtro). En prod: `519594605520486428`.

### Bugs conocidos (historial)

1. (e1e9a22) URL joining roto: `relay.rstrip("/") + path` → `urljoin`.
2. (10e73d4) Credenciales hardcodeadas → `config.ADMIN_USER`/`ADMIN_PASS`.
3. (a1e1934) Auth en JS fetch: server-side embed en vez de JS fetch.
4. (3504842) JS SyntaxError por escapes en Python `"""`.
5. (08e650d) Faltaban tooltips y columna Name.
6. (902cdd5) Tooltips más detallados.
7. (2026-06-07) Código JS huérfano en `apiServer.py:313-342` que rompía el render de las tabs. Fix: eliminado.
8. (2026-06-07) Bloque duplicado de stream+watch_stream tracking en `bot.py:547-569`. Fix: eliminado, y el restante ahora requiere viewers > 0 con quality escalada por cantidad de viewers + daily cap.

### 2026-06-12 — Files invisibles por deploy y delete_token auth

16. **`_index.json` reseteado por deploy**: `transfers/` agregado a `.gitignore`. El deploy con `git reset --hard` ya no pierde el tracking de sesiones.
17. **Recuperación de sesiones desde disco**: `_load_index()` ahora escanea `transfers/` por directorios no indexados y reconstruye sesiones desde `_history.jsonl` + metadata del archivo en disco. Los archivos viejos aparecen automáticamente después de un restart.
18. **`downloadRaw` y `downloadFile` sin sesión en memoria**: si la sesión no está cargada, los endpoints buscan el archivo directamente en disco. Ya no requieren `sess.ready`.
19. **`delete_token`**: cada sesión genera un `delete_token` único en `create_session()`. El endpoint `DELETE /upload/{token}` requiere `?dt=delete_token`. Solo la página de upload (que tiene el token embebido) puede borrar. El link de descarga en Discord NO sirve para borrar.
20. **Botón Cancelar en upload**: aparece durante la subida, detiene los chunks restantes y borra el archivo parcial via `DELETE /upload/{token}?dt=...`.
21. **`transferHistory` sin guard**: se removió el chequeo `if not mgr.sessions.get(token)` que retornaba vacío para tokens desconocidos. Ahora siempre devuelve el historial completo desde `_history.jsonl`.

## 📦 Últimos cambios

### 2026-06-20 — GoLive: fix encoder ARM + fix timeout HLS

1. **`_detect_encoder()` por probe real** (`golive/streamer.py`): reemplazado el chequeo de `ffmpeg -encoders` por un encode de 1 frame real a null por cada candidato. Soluciona que `h264_nvenc` se usara en el server ARM (sin CUDA) causando que FFmpeg fallara en rc=1 inmediatamente y el stream nunca emitiera frames.
2. **Timeout inicial de 15s en `_send_loop`** (`golive/streamer.py`): streams HLS con múltiples renditions tardan 4-8s en probe antes del primer frame. El timeout original de 2s hacía que el loop detectara un "proceso muerto" y abortara. El primer `read()` ahora espera 15s (`first_read=True`); los siguientes mantienen 2s.

### 2026-06-29 — Botones de Mascota: fix view dispatch + fix GIF animado

1. **Botones sin logs ni respuesta** (`bot.py`): los botones de `/mascota ver` (Mostrar, GIF, Evolucionar, etc.) mostraban This interaction failed sin ningún log. La causa era el patrón `safe_defer()` + `followup.send(ephemeral=True, view=view)`: py-cord almacena el View en el `ViewStore` sin `message_id` cuando `followup.send()` se llama con `wait=False` (default), y aunque `ViewStore.dispatch()` tiene un fallback a `message_id=None`, la interacción del botón no encontraba el view. **Fix**: reemplazar el defer + followup por `ctx.respond(msg, ephemeral=True, view=view)` directo, que registra el view a través de `InteractionResponse.send_message()` y además asigna `view.message` via `original_response()`. Las entradas en el log (`on_error`, `log.warning`) ahora se ven correctamente.

2. **GIF no se generaba** (`petGenerator.py:185`): `asciiAnimator.js` destructure `pet.parts.eyes.s` para los caracteres de ojos en la animación de parpadeo, pero el generador de mascotas solo guardaba `{name: ..., r: ...}` en `parts[eyes]`, omitiendo la clave `s`. **Fix**: agregar `s: eyes[s]` al dict de ojos. El GIF ahora se renderiza correctamente (25761 bytes, rc=0).

3. **Backfill para mascotas existentes** (`petGenerator.py:273`): mascotas guardadas antes del fix no tienen `eyes.s` en sus parts. Se agregó `_backfill_missing_eye_character()` que rellena `eyes.s` desde `PARTS["eyes"]` al cargar la mascota. Se invoca desde `get_or_create_pet()` y `get_pet()`.

### 2026-06-13 — Sistema de historias: prompt sin nombres forzados + memoria del Indio + aprobación vía DM del owner

34. **Prompt sin lista de nombres**: `_STORY_PROMPT` ya no dice "uno de los pibes (Viny, Fox...)". Gemini describe lo que realmente ve en la imagen. Si reconoce un famoso lo identifica; si no, hace un chiste sobre la situación sin inventar identidades.
35. **Memoria del Indio en chistes**: `_generate_story()` ahora inyecta la misma memoria larga que usa `/indio` (`_format_long_term()` de `geminiCommand`). El Indio sabe quiénes son sus amigos, anécdotas y chistes internos al generar el chiste.
36. **Aprobación vía DM del owner**: cuando alguien reacciona ✅, el Indio ya no guarda inmediatamente. Te manda **DM** con la imagen + chiste y espera tu respuesta. Respondés **"sí"** → se guarda con descripción + tags. **"no"** → se descarta. Sin timeout.

### 2026-06-13 — Refactor: validación de descripciones del usuario contra Gemini

26. **`_validate_candidate()`** reemplaza `_describe_with_gemini()`: un solo llamado
    a Gemini describe la imagen + extrae tags + valida si el texto del usuario coincide
    (prompt "describí CORRECTAMENTE el contenido, no el formato, sino el contenido real").
27. **Loop de 5 intentos**: si no coincide, incrementa `_retries` y pide nueva descripción.
    Al quinto fallo, saltea la imagen automáticamente.
28. **Descripción de Gemini oculta durante validación**: solo se muestra cuando coincide
    y pregunta "¿La guardo así?".
29. **Filename genérico** ya no describe con IA automáticamente — fuerza al usuario a
    escribir una descripción manualmente (va a `waiting_desc`).
30. **`_save_user_and_gemini_desc()`** guarda ambas descripciones (`description` del
    usuario + `gemini_description` de Gemini). Reemplaza `_save_with_filename`,
    `_save_with_user_description`, `_save_with_gemini_desc`.
31. **Menú simplificado**: `confirm` stage solo tiene **1** (filename), **cancelar**
    (skip), o **cualquier texto** (descripción propia). `confirm_save` stage acepta
    **sí** (primera palabra) o cualquier otra cosa (skip).
32. **Cache de imagen**: `_pending_data` evita re-descargar en cada reintento.
33. **`imageManager.add_image()`** recibe `gemini_description: str = ""`.

### 2026-06-12 — Indio DM: rechazo de filenames genéricos + analytics + fix coincidence de Gemini

22. **Filename genéricos rechazados**: `_is_generic_filename()` detecta nombres como
    `image.png`, `photo.jpg`, `IMG_1234.jpg` (tags vacíos, solo números o solo
    palabras genéricas). Si el usuario dice "sí" con un filename así, se redirige
    automáticamente a Gemini para que describa la imagen.
23. **Gemini ya no verifica coincidencia**: se eliminó el prompt que le preguntaba
    a Gemini si "image.png" coincide con la imagen — es al pedo porque Gemini dice
    "sí" al pedo (técnicamente es una imagen). La validación de filenames es
    ​100% client-side via `_is_generic_filename()` + `_extract_tags()`.
24. **Analytics events** en cada paso del flujo:
    - `indio_image_session_started` — inicio de sesión
    - `indio_image_action` — cada respuesta del usuario (describe_with_ai,
      user_description, skip, unrecognized, confirm_gemini_desc, reject_gemini_desc,
      generic_filename_rejected)
    - `indio_image_gemini_described` — resultado de la descripción de Gemini
    - `indio_image_saved` — imagen guardada (con método: filename/user_desc/gemini_desc)
    - `indio_image_session_finished` — sesión terminada
25. **`_extract_tags()` filtra números**: los tokens puramente numéricos ya no se
    consideran tags (ej: "2024" de "IMG_2024.jpg").

### 2026-06-11 — Fixes y mejoras en /transferir

1. **URL encoding de espacios**: `apiServer.py:1243` — `quote(sess.filename)` al construir URL del botón de Discord. Antes espacios rompían el embed con `400 Bad Request`.
2. **Botón en vez de texto**: `bot.py` — `/transferir` ahora envía `discord.ui.Button(label="⬆️ Subir acá", url=...)` en vez de texto plano.
3. **Página de descarga separada**: `transferCommand.py` — nuevo template `DOWNLOAD_HTML`. Sin botón de borrar, sin "Sesión expirada". Muestra icono, filename, tamaño, botón de descarga. Si el archivo no existe: "Archivo no disponible".
4. **Redirect a /dl/ al completar upload**: `transferCommand.py:683` — `window.location.href = "/dl/..."` en vez de mostrar sección "completado".
5. **Icono VaporPals**: `static/icon.jpg` — favicon + main icon en ambas páginas.
6. **Timer suave (segundo a segundo)**: `transferCommand.py` — reemplazado `updateTimer()` (saltos de 5s) por `syncTimer()` (fetch cada 5s para sincronizar) + `tick()` (contador local cada 1s).
7. **Ocultar upload al expirar**: `transferCommand.py` — cuando el timer detecta expiración, oculta `upload-section` y muestra `expired-section`. Antes solo cambiaba el texto.
8. **Borrado de archivos pesados**: Se liberaron ~7 GB borrando dos `Burglin Gnomes.zip` de 3.4 GB del server.

### 2026-06-11 — Seguridad

9. **XSS en HTML/JS**: `html.escape()` en `format_download_html`, función `esc()` en JS para escapar filenames en innerHTML. Atributo `href` sanitizado vía `url_quote()` en URLs.
10. **Path traversal**: rechazo de `/` y `\` en `init_upload`, `os.path.basename()` en endpoints de descarga.
11. **Auth middleware para /static/**: `apiServer.py` — agregado `/static` a la whitelist del middleware para que el icono se sirva sin token.

### 2026-06-12 — Restricción de tipos de archivo y auto-embed

12. **Extensiones permitidas**: solo se aceptan estos grupos:
    - **Archivos comprimidos**: `.zip`, `.rar`, `.7z`, `.tar`, `.gz`, `.tgz`, `.bz2`, `.xz`, `.zst`
    - **Imágenes**: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`, `.svg`, `.ico`, `.avif`
    - **Videos**: `.mp4`, `.webm`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`
    - Cualquier otra extensión responde `"formato no permitido"`.
    - Implementado en `transferCommand.py:_ext()`, `ALLOWED_EXTS`, validación en `init_upload()`.
13. **Auto-embed de media en Discord**: en `apiServer.py:uploadComplete()`, si es imagen/video se envía el link al endpoint `/raw` para que Discord lo incruste/reproduzca. Para archivos comprimidos se mantiene el embed con botón "Descargar".
14. **Página de descarga con preview de media**: `DOWNLOAD_HTML` + `format_download_html()` detecta si es imagen/video y muestra `<img>` o `<video>` directamente en vez del botón "Descargar". Content-Disposition `inline` para media, `attachment` para archivos.
15. **Logs y PostHog analytics** en todos los pasos: eventos `transfer_rejected` (con razón: `path_traversal`, `format_not_allowed`, `oversize`, `disk_full`), `transfer_init`, `transfer_complete` (con `embed_type`: `image`/`video`/`archive`), `transfer_embed_failed`.

## 🎭 Sistema de Historias del Indio (/story-test, pool de imágenes, review y aprobación)

El Indio genera **chistes automáticos** sobre imágenes del pool cuando el chat está
idle (>4h) o hay más de 2 humanos en voz. Las historias se postean en el canal de
review para que el grupo las vote con ✅/❌ o feedback por reply.

### Pool de imágenes (`imagePool.py`)

Las imágenes viven en `indio_images/pool/` (relativo a `POOL_DIR`). Se llenan cuando
alguien manda imágenes por DM al Indio y las rechaza (cancelar) o cuando falla la
descripción — vuelven al pool como candidatas para historias.

- `init_pool()`: escanea el directorio una vez, cachea la lista.
- `get_random_image(mgr)`: elige una al azar, excluyendo las que ya están en el
  manifiesto (dedup por `original_filename`).
- `remove_from_pool(rel_path)`: borra la imagen del pool cuando se aprueba.

### Pipeline de historia

1. `trigger_story()`: checks guards (daily max, pending review, min messages since
   last story) → pick del pool → Gemini genera chiste → postea al canal de review.
2. **Guards**: `_can_post_story()`: max `INDIO_MAX_STORIES_PER_DAY` por guild,
   no si hay review pendiente, no si no hubo `INDIO_STORY_MIN_MESSAGES_AFTER`.
3. **Watcher**: `start_story_watcher()` corre cada 60s, chequea idle por guild.
   Si pasó `INDIO_IDLE_MINUTES` + delay random (1-2h), dispara historia.

### Prompt (`_STORY_PROMPT`)

Gemini recibe instrucciones de hacer un chiste sobre **lo que realmente ve** en la
imagen. Sin lista de nombres hardcodeada. Si reconoce un famoso lo identifica; si no,
describe la situación de forma cómica. Además se le inyecta la **memoria larga**
del Indio (amigos, anécdotas, chistes internos del grupo).

### Flujo de review y aprobación

1. **Post**: `_post_review()` relayea el chiste + imagen y el texto de voto
   (`✅ · ❌ · respondé con otra idea`) vía userbot (el Indio real).
2. **✅ alguien reacciona** → el Indio **te manda DM** con la imagen + chiste.
   Respondés **"sí"** para guardar definitivamente o **"no"** para descartar.
   Sin timeout, sin condiciones.
   - Al decir "sí", `_save_approved_story()` describe la imagen con Gemini
     (detecta famosos, genera tags) y la guarda en `indio_images/manifest.json`.
   - Se borran los mensajes del review.
3. **❌ reacción** → el chiste se rechaza, la imagen vuelve al pool.
4. **Reply con feedback** → `handle_first_msg_after_story()` evalúa si el
   comentario se relaciona con el chiste (`_evaluate_reply_context()` con Gemini).
   - **Relacionado**: regenera el chiste con el feedback como contexto.
   - **No relacionado**: el Indio manda DM con la imagen original + chiste, y
     explica por qué llegó por DM. El usuario puede responder al DM y el Indio
     contesta naturalmente sobre la imagen (`handle_story_dm_reply()`).

### Archivos clave

| Archivo                  | Rol                                                                |
| ------------------------ | ------------------------------------------------------------------ |
| `storyManager.py`        | Toda la lógica: pool, generación, review, aprobación, DMs          |
| `imagePool.py`           | Pool de imágenes candidatas (`indio_images/pool/`)                 |
| `imageManager.py`        | Manifiesto de imágenes guardadas (`indio_images/manifest.json`)    |
| `apiServer.py:1112-1130` | Handler de DMs: story DM reply + owner approval via `/indio-image` |
| `bot.py:1931-1970`       | `/story-test` — forzar historia para testing                       |
| `config.py`              | `INDIO_STORY_CHANNEL_ID`, `INDIO_MAX_STORIES_PER_DAY`, etc.        |

## 🖼️ Colección de imágenes del Indio (userbot DM → relay → VaPls → catálogo)

El Indio (userbot de Discord) recibe imágenes por **DM** y las relayea al main
bot VaPls, que corre la state machine y las guarda en una colección curada
(`indio_images/manifest.json`). Las imágenes quedan disponibles para que el
Indio las use espontáneamente en conversaciones vía la tool `use_image`.

### Flujo por DM

1. Usuario manda DM con 1+ imágenes al **Indio** (userbot, no al bot VaPls).
2. Userbot relayea las imágenes a `POST /indio-image` de VaPls (`apiServer.py`).
3. VaPls corre la state machine `_ImageDMSession` y devuelve las respuestas.
4. Userbot envía las respuestas al DM channel del usuario.
5. Por cada imagen VaPls pregunta: "¿Qué hacemos?"
   - **1** → usar el filename como descripción candidate
     - Si el filename es **genérico** (image.png, photo.jpg, etc.) → `_is_generic_filename`
       lo rechaza, pide que el usuario escriba una descripción (waiting_desc)
     - Si el filename es **válido** → se valida contra Gemini
   - **cancelar** → saltea la imagen
   - **cualquier otro texto** → se usa como descripción candidate y se valida contra Gemini
6. **Validación con Gemini**: `_validate_candidate()` describe la imagen internamente y
   verifica si coincide con el texto del usuario. Si coincide → muestra descripción de Gemini
   y pregunta "¿La guardo así?". Si no coincide → loop de hasta 5 intentos (waiting_desc),
   mostrando solo "No coincide" sin revelar la descripción de Gemini.
7. En `confirm_save` → "**sí**" (primera palabra) guarda con ambas descripciones
   (`description` + `gemini_description`); cualquier otra cosa saltea.
8. Guarda imagen + metadata en `indio_images/manifest.json`
9. Al final muestra resumen de lo guardado.
10. Timeout de 5min sin respuesta: "te fuiste afk, más tarde seguimos".

### Tool `use_image`

El Indio puede llamar `use_image(image_id, caption?)` para mostrar una imagen
de la colección en el chat. El catálogo se inyecta en el system prompt como
`[IMÁGENES DISPONIBLES]`.

### Archivos clave

| Archivo                      | Rol                                                                                                             |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `imageManager.py`            | CRUD del manifiesto + archivos de imagen + generación del bloque de catálogo                                    |
| `geminiCommand.py`           | State machine de sesión DM (`_ImageDMSession`), dispatch de `USE_IMAGE`, inyección de catálogo en system prompt |
| `apiServer.py:1057-1134`     | Endpoint `POST /indio-image`: relay endpoint que recibe imágenes del userbot y corre la state machine           |
| `userbot/bot.py:2579-2682`   | Handler `_handle_indio_image_dm()`: relay de DMs con imágenes a VaPls via `/indio-image`                        |
| `gemini_keywords.py:119-125` | Trigger phrases para `use_image` en `SYSTEM_TRIGGERS`                                                           |
| `config.py:72-73`            | `INDIO_IMAGES_DIR` + `INDIO_IMAGE_GUILD_ID`                                                                     |
| `indio_images/manifest.json` | Catálogo persistente (gitignored)                                                                               |

### Config

```bash
INDIO_IMAGES_DIR=indio_images        # default
INDIO_IMAGE_GUILD_ID=0               # guild ID para role gate; 0 = desactivado
```

## 💡 Guía de Modificación

1. **Tests primero (o junto al cambio):** seguí la skill `behavioral-testing`. No
   marques una tarea como completa sin tests verdes.
2. **Mantener DAVE patch:** No eliminar el patch en `userbot/bot.py` salvo que
   haya cambios claros en la API de Discord.
3. **Config y .env:** Toda nueva variable de entorno debe documentarse en
   `docs/configuration.md` (y `.env.example` si aplica).
4. **Docs primero:** Mantener `README.md` y los docs alineados si cambia la
   arquitectura o comandos.

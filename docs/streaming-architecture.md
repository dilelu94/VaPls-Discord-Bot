# GoLive IPTV Streaming Architecture

## Overview

VaPls tiene **tres procesos** independientes que corren como systemd services en el server:

| Service          | User                         | Rol                                      | Puerto |
| ---------------- | ---------------------------- | ---------------------------------------- | ------ |
| `discord-bot`    | VaPls bot (token de bot)     | Slash commands, `/stream`, `/play`, etc. | —      |
| `indio-userbot`  | `espermabebo` (cuenta real)  | Voice receive, ASR, wake-word "indio"    | `8081` |
| `golive-userbot` | `espermabebo` (misma cuenta) | IPTV Go Live streaming                   | `8082` |

El **indio** y el **golive** usan la **misma cuenta de Discord** (`espermabebo`) pero corren en venvs separados con systemd services distintos. No comparten código de streaming entre sí.

---

## Flow: `/stream ESPN`

```
Usuario corre /stream ESPN
        │
        ▼
bot.py (VaPls bot principal)
  1. Busca "ESPN" en iptv-org.github.io M3U playlist
  2. Obtiene la URL M3U8 del canal
  3. POSTea a GOLIVE_RELAY_URL/stream
     (body: {guild_id, channel_id, url})
        │
        ▼
golive/bot.py (GoLive userbot)
  1. Recibe POST en relay HTTP (127.0.0.1:8082)
  2. Valida X-API-Secret
  3. Hace join al voice channel via discord.py-self
  4. Establece una conexión Go Live (screenshare) dedicada via `GoLiveConnection`
     a. Registra futuros de eventos `STREAM_CREATE` y `STREAM_SERVER_UPDATE` en el WebSocket principal del bot.
     b. Envía op 18 (`STREAM_CREATE`) y op 22 (`STREAM_SET_PAUSED`, paused=False) en el WebSocket principal.
     c. Recibe credenciales de transmisión (server_id, stream_key, endpoint, token).
     d. Crea un socket UDP y abre un WebSocket separado al servidor de stream.
     e. Realiza el handshake de voz secundario (IDENTIFY → READY → IP discovery → SELECT_PROTOCOL → SESSION_DESCRIPTION).
     f. Anuncia capacidad de video enviando op 12 (`VIDEO`) en el WebSocket de stream.
   5. Crea VideoStream(url, conn)
   6. VideoStream.start():
      a. Registra el video SSRC en la MLS DAVE session (`dave_session.register_video_ssrc`).
      b. Spawnea FFmpeg -i <M3U8> → encoder H.264 → pipe:1
         (encoder auto-detected: h264_nvenc > h264_vaapi > libopenh264)
      c. Aplica packet pacing: cada frame distribuye sus RTP packets en un % del intervalo
         para evitar bursts (configurable vía `STREAM_PACKET_PACE`, default 75%).
      d. _send_loop(): lee H.264, parte NALs, reescribe SPS,
         aplica encriptación DAVE E2EE (`dave_session.encrypt_h264`),
         encripta transporte RTP y envía por el socket de `GoLiveConnection`.
```

## Por qué funciona así (y por qué NO como antes)

### Go-Live / Screenshare real

Discord tiene **dos modos** para enviar video:

| Modo                      | Cómo se activa                                              | Apariencia                                            |
| ------------------------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| **Cámara** (self-video)   | `client_connect()` (op 12) en el WebSocket de voz principal | Aparece como que el userbot activó su cámara          |
| **Go-Live** (screenshare) | `op 18` → `op 22` → WebSocket separado + UDP dedicado       | Aparece como "Transmitiendo" con botón "Watch Stream" |

Anteriormente usábamos **modo cámara** (falso stream) porque era más simple, pero Discord bota la transmisión si no se hace el flow completo. Ahora implementamos **Go-Live real** abriendo un WebSocket secundario y negociando la transmisión directamente mediante el opcode 18 (`STREAM_CREATE`) y 22 (`STREAM_SET_PAUSED`).

### Compatibilidad DAVE E2EE para Video

El wrapper de DAVE por defecto en `discord.py-self` (`davey`) está roto para video. Para evitar que Discord tire los paquetes cifrados de video de E2EE en canales seguros, implementamos:

1. `golive/davey_compat.py`: Shim de compatibilidad con la biblioteca nativa `libdave` (instalada vía `dave.py` de PyPI).
2. Registro explícito del video SSRC (`dave_session.register_video_ssrc(video_ssrc)`) usando el codec H264.
3. Cifrado nativo de la unidad Annex-B antes de packetizar (`dave_session.encrypt_h264(...)`).

### Encoder detection y el problema de libx264

El encoder detection prueba en orden: `h264_nvenc` > `h264_vaapi` > `libopenh264`. **libx264 se saltea intencionalmente** porque Discord's video server descarta streams encodeados con libx264 (documentado por [slopsoil](https://github.com/dev-topsoil/slopsoil/)).

El server tiene FFmpeg 7.1 compilado desde source con `libopenh264` y `--enable-gnutls`. Si no hay encoder de hardware disponible (como en Oracle Cloud ARM), se usa `libopenh264` con `profile:v constrained_baseline`, que es el único encoder software que Discord acepta consistentemente.

### endpoint_port vs voice_port

El `VoiceClient` de discord.py-self v2.1.0 **no** tiene `endpoint_port`. El puerto del servidor de voz vive en `VoiceClient._connection.voice_port`.

**Bug original:** `bot.py` línea 174 buscaba `getattr(vc._connection, "endpoint_port", None)` que siempre devolvía `None` → HTTP 500 "endpoint not resolved".

**Fix:** Usar `getattr(conn, "voice_port", None)`.

---

## Archivos

### `golive/bot.py` (286 líneas)

Entry point del GoLive userbot. Corre como `python3 bot.py` desde `golive/`.

**Qué hace:**

- HTTP relay server en `127.0.0.1:8082` con endpoints:
  - `POST /stream` — inicia stream en un voice channel
  - `POST /stopstream` — detiene el stream activo
- Maneja join/reconnect a voice channels
- Inicializa `davey_compat` y parches de `libdave` antes del inicio de sesión.
- Instancia y conecta `GoLiveConnection` para establecer el canal de streaming secundario.

### `golive/davey_compat.py`

Shim de compatibilidad para DAVE E2EE que envuelve a `dave.py` (bindings de `libdave` de DisnakeDev) para reemplazar la implementación nativa defectuosa de `davey` en `discord.py-self`. Expone funciones para registrar y cifrar video SSRC (`register_video_ssrc` y `encrypt_h264`).

### `golive/golive_connection.py`

Maneja el ciclo de vida del stream de Go Live (screenshare) independiente. Envía opcodes de control a la gateway principal, abre el socket UDP y WebSocket secundarios al servidor del stream, y ejecuta el handshake de voz de la transmisión Go Live.

### `golive/video_compat.py` (117 líneas)

Parches a `discord.gateway.DiscordVoiceWebSocket` para que Discord sepa que mandamos video. Sin estos parches, Discord ignora todos los paquetes RTP de video.

Tres parches:

1. **`identify()`** (op 0) — agrega `"video": true` + `streams` descriptor
2. **`select_protocol()`** (op 1) — agrega codec H264
3. **`client_connect()`** (op 12, `self.VIDEO`) — agrega `video_ssrc`, `rtx_ssrc`, y metadata del stream

**Importante:** `client_connect()` declara **siempre** valores altos y fijos a Discord (`max_bitrate: 10_000_000`, `max_framerate: 60`, `max_resolution: 1920x1080`) independientemente de la resolución real del encoder. Esto hace que Discord asigne un jitter buffer más grande y más ancho de banda, eliminando el stuttering 1s-on/1s-off. Basado en [slopsoil](https://github.com/dev-topsoil/slopsoil/).

### `golive/streamer.py` (1250+ líneas)

Clase `H264VideoPlayer` (thread) — el pipeline completo de streaming en un thread daemon. Usa un solo proceso FFmpeg para audio y video desde la misma URL, eliminando A/V desync.

**Pipeline:**

1. **Encoder detection** (`_detect_encoder()`): prueba `h264_nvenc` > `h264_vaapi` > `libopenh264`. Saltea `libx264` (roto con Discord). Si un encoder hardware falla en runtime, reintenta con software (`_SW_ENCODER`).
2. **Spawnea FFmpeg**: `ffmpeg -i <M3U8> -c:v libopenh264 -b:v 10000k -r 30 -g 30 -profile:v constrained_baseline -f h264 pipe:1`
   - Video a stdout (pipe:1), audio PCM a FIFO (`/tmp/slopsoil_{ssrc}_audio.fifo`)
   - `-max_error_rate 1` tolera errores iniciales sin abortar
   - `-reconnect 1 -reconnect_streamed 1` para URLs HTTP
3. **Lee raw H.264 AnnexB** de stdout en chunks de 64KB
4. **Parte NAL units** por start codes (`\x00\x00\x00\x01`)
5. **Agrupa NALs en frames** — AUD (type 9) marca boundaries de frame, slice NALs (1-5) inician frame nuevo
6. **Reescribe SPS VUI** — fuerza `bitstream_restriction_flag=1` y `max_num_reorder_frames=0` o Discord bota con Error 2015
7. **Packet pacing** — Opcionalmente distribuye los RTP packets de cada frame en un % del intervalo (`STREAM_PACKET_PACE`, default 75%). Evita bursts que sobrecargan el receive buffer de Discord.
8. **Packetiza cada frame en RTP** (FU-A fragmentation si el NAL > 1188 bytes), RFC 6184
9. **Cifrado E2EE (DAVE)** — Aplica `encrypt_h264()` a nivel de frame Annex-B si la sesión DAVE está activa
10. **Encripta transporte RTP** — Según modo negociado (`aead_xchacha20_poly1305_rtpsize`, etc.) usando PyNaCl
11. **Envía** por `vc._connection.send_packet()` (UDP socket)
12. **Stats cada 10s**: fps real, late frames, read-block time, packets sent — loggeados para diagnóstico

**Presets de calidad** (configurables vía `STREAM_QUALITY`):

| Quality | Resolution | FPS | Bitrate |
| ------- | ---------- | --- | ------- |
| 720p    | 1280x720   | 30  | 10000k  |
| 1080p   | 1920x1080  | 60  | 12000k  |
| 4k      | 3840x2160  | 60  | 24000k  |

Default: `1080p` (alineado con slopsoil). Para cuentas free de Discord (capped a 720p30), setear `STREAM_QUALITY=720p`.

### `golive/config.py`

Lee `.env` con:

- `GOLIVE_TOKEN` — token de la cuenta de Discord
- `GOLIVE_RELAY_HOST/PORT` — para el relay HTTP
- `GOLIVE_RELAY_SECRET` — compartido con el bot principal
- `GOLIVE_GUILD_ALLOWLIST` — opcional, restringe guilds
- `STREAM_QUALITY` — preset de calidad: `720p`, `1080p`, `4k` (default `1080p`)
- `STREAM_RESOLUTION` — override manual de resolución (ej. `1280:720`)
- `STREAM_FPS` — override manual de fps
- `STREAM_VIDEO_BITRATE` — override manual de bitrate (ej. `5000k`)
- `STREAM_PACKET_PACE` — fracción del intervalo de frame para distribuir packets (0.0-0.95, default 0.75)

### `golive/requirements.txt`

```
discord.py-self @ git+https://github.com/dolfies/discord.py-self@v2.1.0
python-dotenv
aiohttp>=3.9
PyNaCl>=1.5.0
davey>=0.1.0
```

`davey` es necesario para el handshake DAVE E2EE — sin eso Discord cierra el WebSocket con código 4017.

### `golive/golive-userbot.service`

```ini
[Unit]
Description=VaPls GoLive IPTV streaming userbot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/vapls-discord-bot/golive
EnvironmentFile=/home/ubuntu/vapls-discord-bot/golive/.env
ExecStart=/home/ubuntu/vapls-discord-bot/golive/venv/bin/python3 bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### `golive/.env.example`

```env
GOLIVE_TOKEN=your-token-here
GOLIVE_RELAY_HOST=127.0.0.1
GOLIVE_RELAY_PORT=8082
GOLIVE_RELAY_SECRET=vapls-golive-shared-secret
GOLIVE_GUILD_ALLOWLIST=
LOG_LEVEL=INFO
STREAM_QUALITY=720p
STREAM_PACKET_PACE=0.75
```

---

## Indio userbot (`userbot/`)

El indio **no tiene nada de video streaming**. Se eliminó `userbot/streamer.py` y todo el relay `/stream`, `/stopstream`. Es exclusivamente voice-input:

- Voice receive via `discord-ext-voice-recv`
- ASR con faster-whisper + VOSK (wake word "indio")
- Transcripción → relay al bot principal vía `/indio`
- **NO** hace streaming de video
- **NO** tiene endpoint `/stream` en su relay

---

## Fuentes de IPTV

Los canales se obtienen de [iptv-org](https://github.com/iptv-org/iptv), que mantiene una playlist M3U pública global.

- `iptv.py` fetchea `https://iptv-org.github.io/iptv/index.m3u`
- Cachea localmente en `data/iptv_cache.m3u` por 6 horas
- Busca por substring match, prefiere prefix matches y nombres cortos
- `search(query, limit=5)` → devuelve hasta 5 resultados ordenados por score
- `get_best(query)` → devuelve el mejor match único

---

## Deploy

`scripts/deploy.sh` corre en el servidor via CD (GitHub Actions) después de cada push a master. Es idempotente.

**Lo que hace:**

1. `git fetch origin` + `git reset --hard origin/master`
2. Si `requirements.txt` cambió, reinstala pip en cada venv
3. Crea venv + `.env` + systemd service si es primera vez
4. Sincroniza `GOLIVE_RELAY_SECRET` entre `golive/.env` y `.env` principal
5. Reinicia servicios con `systemctl restart`
6. Verifica que todos los servicios queden `active`

**Dependencias condicionales:** Solo reinstala si el requirements.txt específico cambió.

---

## Problemas comunes

### `WebSocket closed with 4017 (reason: 'E2EE/DAVE protocol required')`

**Causa:** El canal de voz requiere DAVE y `davey` no está instalado en el venv.

**Fix:** `pip install davey` en el venv de golive. Ya está en `requirements.txt`.

### `RuntimeError: PyNaCl library needed in order to use voice`

**Causa:** `PyNaCl` no está instalado. discord.py-self lo necesita para encriptación de voz.

**Fix:** `pip install PyNaCl` en el venv. Ya está en `requirements.txt`.

### Stream se conecta pero no se ve (Error 2015 en logs de Discord)

**Causa:** El SPS no tiene `bitstream_restriction_flag=1` y `max_num_reorder_frames=0`.

**Fix:** El SPS rewriter en `golive/streamer.py` maneja esto automáticamente. Si el encoder usado no genera SPS con VUI, falla porque el encoder no se reconoce como high profile.

### El stream se ve trabado (1 segundo reproduce, 1 segundo congelado)

**Causa 1:** El encoder detectado es `libx264`. Discord descarta streams de libx264.

**Fix:** Verificar logs: `journalctl -u golive-userbot | grep "video encoder"`. Debe decir `libopenh264`. Si dice `libx264`, el server tiene libx264 disponible y el detection lo elige antes que libopenh264. Se parcheó `_detect_encoder()` para saltear libx264.

**Causa 2:** `video_compat.py` declara valores bajos en op 12 (VIDEO). Discord asigna un jitter buffer pequeño y menos ancho de banda, causando drops.

**Fix:** Declarar siempre `max_bitrate: 10_000_000`, `max_framerate: 60`, `max_resolution: 1920x1080` independientemente de la salida real del encoder (alineado con slopsoil).

**Causa 3:** Los packets de cada frame se envian en burst (todos juntos). Si el burst supera el buffer de Discord, se pierden y el decoder espera al próximo keyframe.

**Fix:** Activar packet pacing: `STREAM_PACKET_PACE=0.75` distribuye los packets en 75% del intervalo del frame.

### El bot principal responde HTTP 500

**Causa:** El golive userbot no está corriendo, o el relay no responde.

**Fix:** `sudo systemctl status golive-userbot` y revisar logs con `journalctl -u golive-userbot -n 50`.

---

## Alineación con slopsoil

El pipeline de streaming está basado en [slopsoil](https://github.com/dev-topsoil/slopsoil/). Las diferencias clave resueltas:

| Aspecto                 | VaPls (antes)                        | slopsoil / VaPls (ahora)     |
| ----------------------- | ------------------------------------ | ---------------------------- |
| Encoder                 | libx264 (roto)                       | libopenh264 (saltea libx264) |
| op 12 caps              | Dinámicas según env (2.5Mbps/720p30) | Fijas: 10Mbps/60fps/1080p    |
| Bitrate 720p            | 2200k                                | 10000k                       |
| Default quality         | 720p                                 | 1080p                        |
| Packet pacing           | No                                   | Sí (75%)                     |
| `client_connect` opcode | `getattr(self, "VIDEO", 12)`         | `self.VIDEO`                 |

## Referencias externas

- [slopsoil](https://github.com/dev-topsoil/slopsoil/) — inspiración para `video_compat.py`, el SPS rewriter y la configuración de streaming
- [discord.py-self](https://github.com/dolfies/discord.py-self) — fork de discord.py para user tokens
- [iptv-org](https://github.com/iptv-org/iptv) — playlist M3U pública de canales IPTV
- [RFC 6184](https://datatracker.ietf.org/doc/html/rfc6184) — RTP Payload Format for H.264 Video (FU-A fragmentation)

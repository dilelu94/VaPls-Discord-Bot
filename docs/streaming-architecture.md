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
     a. video_compat.patch_video() aplica parches globales
        a1. identify() (op 0): agrega video:true + streams descriptor
        a2. select_protocol() (op 1): declara codec H264
        a3. client_connect() (op 12): anuncia video_ssrc + streams
  4. Crea VideoStream(url, vc, ws, sock, ...)
  5. VideoStream.start():
     a. Spawnea FFmpeg -i <M3U8> → encoder H.264 → pipe:1
        (encoder auto-detected: h264_nvenc > h264_vaapi > libx264)
     b. _send_loop(): lee H.264, parte NALs, reescribe SPS,
       encripta RTP, envía por UDP socket de Discord
        (video_ssrc = audio_ssrc + 1, NO se envía op=12 extra)
```

## Por qué funciona así (y por qué NO como antes)

### Cámara (no Go-Live/Screenshare)

Discord tiene **dos modos** para enviar video:

| Modo                      | Cómo se activa                                              | Apariencia                                   |
| ------------------------- | ----------------------------------------------------------- | -------------------------------------------- |
| **Cámara** (self-video)   | `client_connect()` (op 12) en el WebSocket de voz principal | Aparece como que el userbot activó su cámara |
| **Go-Live** (screenshare) | `op 18` → `op 21` → WebSocket separado + UDP dedicado       | Aparece como "Transmitiendo" con thumbnail   |

Usamos **modo cámara** porque es más simple: no requiere un WebSocket separado, no necesita el flow `STREAM_CREATE`/`STREAM_SERVER_UPDATE`, y funciona con el mismo socket UDP que el audio.

### video_ssrc = audio_ssrc + 1 (no random)

`video_compat.py::_patched_client_connect()` declara `video_ssrc = audio_ssrc + 1` (VIDEO_SSRC_OFFSET) durante el handshake de voz. Si `streamer.py` después usa un SSRC random, Discord no reconoce los paquetes RTP y los descarta.

**Bug original:** `VideoStream` generaba `_rand_ssrc()`. Los paquetes RTP tenían un SSRC que Discord no esperaba → el stream "iniciaba" pero nadie veía nada.

**Fix:** `video_ssrc = audio_ssrc + 1` — coincide con lo declarado en `client_connect()`.

### NO enviar op=12 duplicado

`client_connect()` (patch de `video_compat.py`) ya envía op=12 con el payload completo incluyendo `streams`, `max_bitrate`, `max_framerate` y `max_resolution`. Si `VideoStream.start()` envía OTRO op=12 con formato distinto (sin `streams`, con campos `stream_id`/`type`), Discord se confunde.

**Bug original:** `_announce_video_ssrc()` enviaba un segundo op=12 con:

```json
{"op": 12, "d": {"audio_ssrc": ..., "video_ssrc": ..., "stream_id": "", "quality": 1, "type": 1}}
```

Pero `client_connect()` ya había enviado:

```json
{"op": 12, "d": {"audio_ssrc": ..., "video_ssrc": ..., "rtx_ssrc": ..., "streams": [{"type": "video", "ssrc": ..., "max_bitrate": 10000000, ...}]}}
```

**Fix:** Eliminar `_announce_video_ssrc()` — el `client_connect()` parcheado ya lo maneja.

### Encoder auto-detected

El server no tiene `libopenh264` compilado en FFmpeg (solo `libx264`, `h264_nvenc`, `h264_vaapi`). El código original hardcodeaba `libopenh264` → FFmpeg fallaba silenciosamente (stderr a DEVNULL) → send loop terminaba sin datos.

**Fix:** `_detect_encoder()` prueba `h264_nvenc` > `h264_vaapi` > `libx264` y usa el primero disponible. Se eliminó el hardcodeo de `libopenh264`.

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
- Aplica `video_compat.patch_video()` antes de conectar (crítico)

**Importante:** corre como `discord.Client()` (no Bot, no slash commands). Solo relay HTTP.

### `golive/video_compat.py` (104 líneas)

Parches a `discord.gateway.DiscordVoiceWebSocket` para que Discord sepa que mandamos video. Sin estos parches, Discord ignora todos los paquetes RTP de video.

Tres parches:

1. **`identify()`** (op 0) — agrega `"video": true` + `streams` descriptor
2. **`select_protocol()`** (op 1) — agrega codec H264
3. **`client_connect()`** (op 3, antes op 12) — agrega `video_ssrc`, `rtx_ssrc`, y metadata del stream (resolución, bitrate, fps)

Basado en [slopsoil](https://github.com/dev-topsoil/slopsoil/).

### `golive/streamer.py` (646 líneas)

Clase `VideoStream` — el pipeline completo de streaming. Asyncio-based.

**Pipeline:**

1. Spawnea FFmpeg: `ffmpeg -i <M3U8> -c:v libopenh264 -b:v 2500k -r 30 -f h264 pipe:1`
2. Lee raw H.264 AnnexB de stdout en chunks de 64KB
3. Parte NAL units por start codes (`\x00\x00\x00\x01`)
4. Agrupa NALs en frames (slice NAL = nuevo frame)
5. **Reescribe SPS VUI** — fuerza `bitstream_restriction_flag=1` y `max_num_reorder_frames=0` o Discord bota con Error 2015
6. Packetiza cada frame en RTP (FU-A fragmentation si el NAL es grande)
7. **Encripta** cada paquete con PyNaCl según el modo negociado (xsalsa20_poly1305, xchacha20, etc.)
8. Manda por `sock.sendto()` al endpoint UDP de Discord

**SPS/VUI rewriting:** Implementa un parser/escritor de H.264 RBSP bitstream completo. Soporta high profiles, scaling lists, HRD, etc.

**RTP encryption:** Soporta 4 modos de Discord:

- `xsalsa20_poly1305` — nonce = header de 12 bytes + padding
- `xsalsa20_poly1305_suffix` — nonce random de 24 bytes al final
- `xsalsa20_poly1305_lite` — nonce de 4 bytes contador al final
- `aead_xchacha20_poly1305_rtpsize` — AEAD con header AAD

### `golive/config.py` (17 líneas)

Lee `.env` con:

- `GOLIVE_TOKEN` — token de la cuenta de Discord
- `GOLIVE_RELAY_HOST/PORT` — para el relay HTTP
- `GOLIVE_RELAY_SECRET` — compartido con el bot principal
- `GOLIVE_GUILD_ALLOWLIST` — opcional, restringe guilds

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

### El bot principal responde HTTP 500

**Causa:** El golive userbot no está corriendo, o el relay no responde.

**Fix:** `sudo systemctl status golive-userbot` y revisar logs con `journalctl -u golive-userbot -n 50`.

---

## Referencias externas

- [slopsoil](https://github.com/dev-topsoil/slopsoil/) — inspiración para `video_compat.py` y el SPS rewriter
- [discord.py-self](https://github.com/dolfies/discord.py-self) — fork de discord.py para user tokens
- [iptv-org](https://github.com/iptv-org/iptv) — playlist M3U pública de canales IPTV
- [RFC 6184](https://datatracker.ietf.org/doc/html/rfc6184) — RTP Payload Format for H.264 Video (FU-A fragmentation)

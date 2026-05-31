# Diseño: sonido de "recibido" al recibir un pedido de audio

Fecha: 2026-05-31

## Problema

Cuando alguien pide música (al Indio) o se manda un audio desde Telegram, hay
un hueco silencioso de varios segundos antes de que empiece a sonar algo (yt-dlp
busca y descarga, o el bot se conecta a voz). El que pidió no tiene feedback de
que el bot lo escuchó y está trabajando.

## Objetivo

Que el **main bot** reproduzca un blip corto en el canal de voz apenas recibe el
pedido, como acuse de recibo ("te escuché, esperá"). Es feedback funcional, no
persona — por eso lo emite el main bot (que ya está conectado a voz y maneja
estos dos flujos), no el userbot.

## Alcance

Aplica a:
- **Pedidos de música del Indio** — `playFromIndio()` en `playCommand.py`.
- **Audio de Telegram** — `playAudio()` en `apiServer.py`.

Fuera de alcance (decisión explícita del usuario):
- `/play` (slash command de música).
- `/soundpad`.
- Reproducción del lado del userbot.

## Comportamiento

- El blip suena **solo cuando el bot está idle** (no reproduciendo ya algo). Si
  ya hay audio sonando, se saltea (no interrumpimos lo que está sonando; agregar
  a la cola ya da feedback visible).
- **Fire-and-forget**: el blip arranca y no se espera a que termine. Cuando el
  audio real está listo, corta el blip. Cero latencia agregada al pedido real.

## Fuente del clip

- Variable de entorno nueva: `ACK_SOUND_QUERY` (default vacío).
- Se resuelve como query fuzzy contra `CUSTOM_AUDIO_PATH` usando la lógica que
  ya existe en `soundpadCommand.py` (`find_best_match`).
- Si está vacía o no matchea nada, la feature es un **no-op silencioso**. Así un
  clon nuevo o un setup sin configurar nunca se rompe.

## Componentes

### 1. Helper compartido en `soundpadCommand.py`

`soundpadCommand.py` ya es dueño de la resolución de clips, así que el helper
vive ahí:

```python
def play_ack_clip(vc) -> bool
```

- Devuelve `False` (no-op) si: `vc` es `None`, `vc` ya está reproduciendo, o
  `ACK_SOUND_QUERY` está vacío / no resuelve a ningún clip.
- Si no, arranca el clip en el `vc` **ya conectado** y devuelve `True`
  inmediatamente (no espera el final — fire-and-forget).
- Reusa el patrón `discord.FFmpegOpusAudio(path, options='-af "dynaudnorm=...")`
  que ya usa `play_clip_by_query`.
- **No** conecta / mueve / desconecta: el caller ya es dueño de un voice client
  conectado. Es síncrono (solo dispara `vc.play()`).

### 2. Call site: `playFromIndio` (`playCommand.py`)

- Después de que el bloque de connect/move a voz tiene éxito y **antes** de
  `_yt_dlp_search(query)`: llamar `soundpadCommand.play_ack_clip(vc)`.
- El chequeo de idle/clip vive dentro del helper.
- **Cut-off**: `startPlayingCurrent()` hace `vc.play(...)` sin frenar lo que
  esté sonando. Agregar un guard `if vc.is_playing(): vc.stop()` antes de
  arrancar la canción real, para cortar el blip si todavía estuviera sonando
  (caso raro, el blip dura ~0.5–1s y yt-dlp tarda más).

### 3. Call site: `playAudio` (`apiServer.py`)

- Después de conectar a voz, llamar `play_ack_clip(vc)` antes del `vc.play(upload)`.
- Ya existe `if vc.is_playing(): vc.stop()` antes del play real, así que el blip
  se corta limpio cuando el audio de Telegram está listo. El propio busy-check
  del helper hace que, si ya había audio sonando al llegar el pedido, se saltee
  el blip (cumple la regla "skip cuando está ocupado").

## Manejo de errores

- Cualquier fallo al resolver o reproducir el clip se traga silenciosamente
  (log y seguir). El blip nunca debe romper ni demorar el flujo real de audio.

## Testing

Tests de comportamiento (mock solo en los bordes reales: `FFmpegOpusAudio` y el
voice client), siguiendo la skill `behavioral-testing`:

- Blip suena cuando: idle + `ACK_SOUND_QUERY` configurada + hay match.
- Blip se saltea cuando el vc ya está reproduciendo.
- Blip se saltea cuando `ACK_SOUND_QUERY` está vacía o no hay match.
- El audio real (música / Telegram) sigue su curso normal después en todos los
  casos.

## Config / docs

- Documentar `ACK_SOUND_QUERY` en `docs/configuration.md`.
- No hay `.env.example` en el repo, así que no se agrega ahí.

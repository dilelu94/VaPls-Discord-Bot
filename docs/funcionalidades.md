# VaPls-Discord-Bot — Funcionalidades completas 🎙️🤖

Resumen funcional de **todo lo que hace el bot**, organizado por área. Documento
descriptivo de alto nivel; para el detalle de implementación ver
[`architecture.md`](architecture.md), [`commands.md`](commands.md),
[`api.md`](api.md) y [`configuration.md`](configuration.md).

---

## Arquitectura: 2 procesos separados

| Proceso | Archivo | Cuenta | Rol |
|---|---|---|---|
| **Main bot** | `bot.py` | Bot de Discord (token de app) | Comandos, audio de salida, Gemini, HTTP API |
| **Userbot** ("el Indio") | `userbot/bot.py` | Cuenta de usuario real | Escucha y transcribe voz (E2EE), graba respuestas |

Se separan porque Discord **no le da las claves de cifrado E2EE (DAVE/MLS) a los
bots** — solo a cuentas de usuario reales. Por eso la transcripción la hace el
userbot. Los dos se comunican por HTTP en loopback.

---

## 1. Comandos slash (main bot, `bot.py`)

| Comando | Qué hace |
|---|---|
| **`/play [query]`** | Reproduce canción/playlist de YouTube (búsqueda o URL). Sin argumento, avisa que falta el query. |
| **`/soundpad [query]`** | Sin query: abre un panel interactivo de clips locales. Con query: busca el clip más parecido (fuzzy) y lo reproduce directo. **Requiere haber donado una API key de Gemini** (gating). |
| **`/vapls <pregunta>`** | Pregunta a Gemini **sin memoria** (stateless). Persona "bot del server". |
| **`/indio <charla>`** | Charla con la persona "el Indio" **con memoria** (corto + largo plazo). |
| **`/parar`** | Detiene la reproducción, vacía la cola y desconecta de voz. |
| **`/quit`** | Desconecta de voz **sin** tocar la cola. |
| **`/restart`** | Devtool: reinicia el proceso del bot (`os.execv`). |

**Eventos del bot:**
- `on_message` (solo DMs): si le mandás por privado una **API key de Gemini**
  (`AIzaSy…` o `AQ.Ab8…`), la detecta, la suma al pool y te lo agradece.
- `on_voice_state_update`: cuando el bot entra a un canal de voz, dispara el
  saludo y arranca el watchdog de inactividad; cuando sale, lo apaga.

---

## 2. Sistema de música (`playCommand.py`)

Es lo más complejo. Centro: la clase **`GuildPlayer`** (una por servidor, en el
dict global `guildPlayers`).

- **Descarga con yt-dlp**: baja el audio a `.mp3` con FFmpeg. Soporta
  `cookies.txt` (para sortear el bot-check de YouTube) y un proxy POT
  (`YT_DLP_POT_BASE_URL`).
- **Cola + historial**: `queue`, `history`, `currentSong`. Permite
  anterior/siguiente.
- **Pre-descarga en segundo plano** (`predownloadQueue`): mientras suena una
  canción, va bajando las siguientes para que no haya cortes.
- **Panel de control** (`PlayerControlView`): embed con botones ⏮️ Anterior /
  ⏸️ Pausar / ⏭️ Siguiente / ⏹️ Stop, que se actualiza solo.
- **Botón Cancelar** durante la descarga inicial (`CancelDownloadView`).
- **Diagnóstico de errores** (`_diagnoseYtDlpFailure`): traduce errores de yt-dlp
  a mensajes claros en español (video privado, restricción de edad, rate-limit
  429, cookies caducas, copyright, etc.).
- **Limpieza**: borra los `.mp3` al terminar cada canción y al limpiar el player.
- **Entradas programáticas** (sin slash): `playFromIndio` (cuando el Indio decide
  poner música) y `playSoundFromIndio` (reproducir un clip local, salvo que haya
  música sonando).
- **Desambiguación "¿cuál querés?"**: si la búsqueda devuelve varios resultados,
  en lugar de reproducir el primero a ciegas se ofrecen las opciones (numeradas
  con emojis). En `/play` es un menú desplegable que resuelve **quien corrió el
  comando** al instante. Con el **Indio** se abre una **votación que cierra
  cuando pasan 10 s sin votos nuevos** (cada voto reinicia la cuenta): cualquiera
  del canal vota y gana la más votada (empate → número más bajo; sin votos → la
  primera). Se puede votar de **tres formas que se combinan**: hablando,
  escribiendo el número, o **reaccionando** con el emoji (el bot pone las
  reacciones 1️⃣2️⃣3️⃣). Un voto por persona. Una URL directa se reproduce sin
  preguntar.

---

## 3. Soundpad (`soundpadCommand.py`)

Panel para reproducir clips de audio locales organizados en carpetas
(`CUSTOM_AUDIO_PATH`).

- **Panel interactivo** (`SoundpadView`): selects de **categoría → subcarpeta →
  sonido**, con paginación (25 por página) y botones Reproducir/Detener.
- **Modo query**: `/soundpad risas` busca el clip más parecido por nombre
  (`difflib`, cutoff 0.4) y lo reproduce con un botón ⏹️ Parar.
- **Normalización de volumen** con filtro FFmpeg `dynaudnorm`.
- **Reconexión robusta** a voz si el cliente quedó stale.
- **No pisa la música**: si hay una canción sonando, rechaza el soundpad.
- **Gated**: solo usable por quien donó una key de Gemini.

---

## 4. Personas Gemini (`geminiCommand.py` + `geminiClient.py`)

Dos personalidades sobre la API de Google Gemini (tier gratuito):

**`/vapls`** — asistente del server, sin memoria, español rioplatense, respuestas
concisas.

**`/indio`** — un "pibe más del grupo", con personalidad y **memoria**:
- **Memoria corto plazo**: historial verbatim por servidor (TTL 6 h).
- **Memoria largo plazo**: cuando el historial crece (>30 turnos), una llamada
  aparte a Gemini **destila** lo viejo en notas estructuradas (rasgos por
  usuario, anécdotas, chistes internos, eventos del grupo) y descarta el
  verbatim. Persiste en disco (`data/indio_memory.json`).
- **Roster de amigos**: lee `users.py` para saber quiénes son sus amigos.
- **Emojis custom**: conoce los emojis del server y los puede usar.
- **Tools (function calling)**: el Indio puede ejecutar acciones cuando se lo
  piden:
  - `play_music` (poner un tema), `play_sound` (clip del soundpad)
  - `skip_music`, `pause_music`, `resume_music`, `stop_music`

  Estas acciones se ejecutan, idealmente, **a través del userbot** para que en el
  chat aparezca como "El Indio usó /play".
- **Actúa primero, después confirma**: el Indio postea su confirmación breve en
  el momento ("dale, va Queen"), corre la acción, y **edita ese mismo mensaje en
  el lugar** con el resultado real: éxito → sufijo corto ("— listo 🔊/🎵/✅");
  falla → el motivo concreto ("— uh, no pude poner la música (no hay nadie en
  un canal de voz)"). Así un error sale a la luz en vez de dejar al usuario
  esperando música que no viene. La edición se hace vía el endpoint `POST /edit`
  del userbot (o directo sobre el mensaje cuando el relay está apagado). Los logs
  de debug por acción (`ok=/fail=`) se mantienen intactos; al usuario sólo le
  llega la línea corta.
- **Pide música con votación**: cuando le piden un tema (voz o chat) y hay varios
  resultados, el Indio lista las opciones (con emojis) y abre una **votación que
  cierra cuando pasan `_MUSIC_VOTE_WINDOW_SEC` (30 s por defecto) sin votos
  nuevos** — cada voto registrado reinicia la cuenta regresiva, así un voto al
  segundo 29 le da otros 30 s a quien quiera votar. **Cualquiera** del canal vota,
  y se puede votar de **tres formas combinadas en el mismo conteo**: hablando,
  escribiendo el número ("la dos", "la 3"), o **reaccionando** con el emoji del
  número (el bot siembra las reacciones 1️⃣2️⃣3️⃣). Un voto por persona (reacción
  y texto del mismo usuario cuentan una sola vez). Al cerrarse gana la más votada
  (empate → número más bajo; sin votos → la primera). Una URL la reproduce
  directo. (El voto hablado/escrito por voz necesita la wake word "indio", p. ej.
  "indio, la dos"; el voto por reacción no.)

**`geminiClient.py`** — cliente HTTP async con:
- **Pool de keys con failover**: rota entre varias keys, marca en cooldown (60 s)
  las que devuelven 429, y elige round-robin.
- Soporte de tools, manejo tipado de errores
  (config/http/timeout/blocked/empty/parse).

**Indio tolerante a ASR** — las transcripciones de voz llegan al `/indio`
prefijadas con `[voz] ` y el system prompt del Indio le instruye a tolerar
errores fonéticos típicos de Whisper (verbos partidos, nombres propios mal
oídos, dígitos perdidos). Esto reemplaza al viejo paso de `decifrarTranscripcion`
(una llamada Gemini extra para limpiar el ASR antes de razonar): ahora el
Indio razona sobre el raw y se ahorra una llamada al modelo.

**Feedback inline de calidad del ASR (`decifrarVoting.py`)** — 1 de cada N
transcripciones de voz recibe reacciones 👍 / ❌ en el mensaje de
transcripción. 👍 = entendió bien (no se loggea nada). ❌ = falso positivo
del wake-word o transcripción mala (se appendea al JSONL
`DECIFRAR_FALSE_POSITIVES_LOG_PATH` con el raw Whisper y el resultado N-best
de VOSK, para debug offline de la calidad del ASR).

---

## 5. Pool de API keys de Gemini (`geminiKeys.py`)

Sistema colaborativo: como el tier gratuito tiene cupo limitado, los usuarios
**donan sus propias keys** por DM.
- Detecta keys en texto, las valida, las suma al pool en caliente (sin reiniciar)
  y persiste en `gemini_keys.json` (gitignored, chmod 600).
- Lleva quién donó cada key (`owner_id`) → así `/soundpad` se "desbloquea" solo
  para quienes aportaron.
- Muestra una línea de "Contribuyentes actuales" como agradecimiento.

---

## 6. Saludos (`greeting.py`)

Cuando el bot entra a un canal de voz reproduce un audio de saludo:
- Audio personalizado por usuario (definido en `users.py`) o uno por defecto
  (`Fish Carrot.m4a`).
- Throttle de 15 s por canal, normalización de volumen, y espera a que el cliente
  UDP esté listo antes de reproducir.

## 7. Watchdog de inactividad (`idleWatchdog.py`)

Tarea por servidor que desconecta al bot tras `VOICE_IDLE_TIMEOUT_SECONDS` (60 s
por defecto) sin reproducir ni estar pausado. Avisa en el canal de texto antes de
irse.

---

## 8. HTTP API (`apiServer.py`) — puente con Telegram

Servidor aiohttp en `127.0.0.1:8080`, protegido por header `X-API-Secret`. Lo usa
un bot de Telegram externo:

| Endpoint | Qué hace |
|---|---|
| `GET /status` | Estado del bot, uptime, voice clients |
| `GET /members` | Canales de voz y miembros |
| `GET /user/{id}` | Datos de un miembro |
| `GET /queue` | Cola de reproducción de un guild |
| `GET /playing` | Si el bot está reproduciendo (lo usa el userbot para ajustar concurrencia) |
| `POST /message` | Publica texto en un canal (prefijado `[TG/...]`) |
| `POST /play-audio` | Reproduce un audio subido desde Telegram; opcionalmente pide al userbot que **grabe la respuesta de voz** y la mande de vuelta |
| `POST /indio` | Invoca al Indio desde una transcripción de voz (prefija `[voz] ` y opcionalmente seedea reacciones de feedback ASR) |
| `POST /gemini-key` | Recibe keys de Gemini reenviadas por el userbot |

---

## 9. Userbot (`userbot/bot.py`) — transcripción de voz

El proceso más técnico. Funciones clave:

**Transcripción:**
- Se une automáticamente a los canales de voz donde hay humanos; se va tras
  inactividad (`IDLE_LEAVE_SECONDS`).
- **DAVE patch**: monkey-patchea el descifrado de `voice_recv` para aplicar
  `dave.decrypt()` y poder oír audio en canales E2EE. (+ parche de resiliencia de
  Opus para no crashear.)
- **Pipeline STT**: recibe PCM → mono → resample a 16 kHz → **faster-whisper**
  (modelo `small`, int8, CPU).
- **Wake word con VOSK** (`WakeWordSink`): VOSK corre barato todo el tiempo con
  una gramática restringida; **solo cuando detecta "indio"** (y variantes
  fonéticas) dispara Whisper sobre el audio. Tiene pre-buffer (capta lo dicho
  antes del wake word), filtrado de falsos positivos y límites de concurrencia
  (3 mientras hay música, 5 si no).
- Al detectar pregunta válida → la manda al `/indio` del main bot (con
  `is_voice=True`); el main bot prefija el texto con `[voz] ` antes de
  pasárselo al Indio.

**Auto-reply en chat de texto:** si alguien escribe "indio" en un canal, reenvía
el mensaje al `/indio` (cooldown de 3 s por canal y tope horario por guild).

**DMs con keys:** reenvía keys de Gemini al endpoint `/gemini-key` del main bot.

**Grabación de respuestas de voz** (`RecorderSink` + `_run_recording`): graba
hasta N segundos del canal, mezcla speakers, recorta silencio, encodea a OGG/Opus
y lo POSTea a un callback (típicamente el bridge de Telegram).

**Servidor relay HTTP** (`127.0.0.1:8081`, secret-gated):

| Endpoint | Qué hace |
|---|---|
| `POST /say` | Postea un mensaje como el usuario real (así las respuestas del Indio salen con su identidad) |
| `POST /edit` | Edita en el lugar un mensaje ya posteado por el userbot (el Indio actúa primero y después edita su respuesta con el resultado real) |
| `POST /record` | Dispara una grabación de voz |
| `GET /members` | Lista miembros del guild (sin necesitar el intent privilegiado en el bot) |
| `POST /invoke_play` | Invoca el `/play` de VaPls como el usuario real |
| `POST /invoke_soundpad` | Invoca el `/soundpad` de VaPls como el usuario real |

---

## 10. Flujos integrados destacados

**Voice-reply Discord → Telegram:** Telegram manda audio → `/play-audio` lo
reproduce → al terminar, el main bot pide al userbot que grabe la respuesta → el
userbot la captura, encodea y la manda de vuelta a Telegram.

**El Indio pone música:** alguien le pide un tema al Indio → Gemini emite la tool
`play_music` → el main bot pide al userbot que invoque `/play` → en el chat
aparece como "El Indio usó /play".

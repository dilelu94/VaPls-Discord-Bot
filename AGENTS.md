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
| **Services**        | `discord-bot.service` (main bot) + `vapls-userbot.service` (userbot) |

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
- `/entraindio`: hace que el userbot (Indio) entre al canal de voz del invocador (relay `/join`).
- `/sensibilidad` `1|2|3`: cambia la sensibilidad del wake-word del Indio en caliente (ver abajo).
- `/banana` (Pausado/Inactivo): genera una imagen con Gemini (gratis, sin API key, usando Playwright). Actualmente en pausa por bloqueos de autenticación de Google.

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

La primera vez que `vapls-userbot.service` levanta en un server fresh, baja el modelo (`Systran/faster-whisper-<size>`) de HuggingFace — agrega ~30-60s al startup. Cachea en `~/.cache/huggingface/` (o `WHISPER_CACHE_DIR` si está seteado).

### 3) DAVE patch en el userbot

El userbot monkey-patchea `PacketDecryptor._decrypt_rtp_*` para aplicar `dave.decrypt()` después del AEAD. **No eliminar** salvo cambio claro en la API de Discord (logs: `"DAVE decrypt monkey-patch installed"`). Si Discord cambia el protocolo DAVE, el userbot queda recibiendo audio cifrado que no puede transcribir.

### 4) `audio_output/` no está en el repo

El soundpad usa `audio_output/` (configurable con `CUSTOM_AUDIO_PATH`). En el server lo poblá lsyncd (ver sección 📁). En clon nuevo, el directorio queda vacío hasta que lsyncd corra desde tu PC.

### 5) FFmpeg estático amd64 vs arm64

El branch `dnf` de `deploy.sh` baja un binario estático según `uname -m` (`amd64` o `arm64`). En Ubuntu/Debian usa `apt install ffmpeg` directamente. Si agregás soporte para otra arch, actualizá ambos branches.

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

### Config

- `USERBOT_USER_ID` — excluye al Indio del conteo de ocupación en canales de voz
  para que su presencia no active actividades MMR que requieren ≥2 humanos.
  `_has_others()` en `bot.py` filtra `m.id != config.USERBOT_USER_ID`.
  Default: `0` (sin filtro). En prod: `519594605520486428`.

## 💡 Guía de Modificación

1. **Tests primero (o junto al cambio):** seguí la skill `behavioral-testing`. No
   marques una tarea como completa sin tests verdes.
2. **Mantener DAVE patch:** No eliminar el patch en `userbot/bot.py` salvo que
   haya cambios claros en la API de Discord.
3. **Config y .env:** Toda nueva variable de entorno debe documentarse en
   `docs/configuration.md` (y `.env.example` si aplica).
4. **Docs primero:** Mantener `README.md` y los docs alineados si cambia la
   arquitectura o comandos.

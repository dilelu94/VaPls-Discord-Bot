# VaPls-Discord-Bot 🎙️🤖

[![CI](https://github.com/dilelu94/VaPls-Discord-Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dilelu94/VaPls-Discord-Bot/actions/workflows/ci.yml)

Bot de voz para Discord con reproducción de audio, soundpad y respuestas con Gemini. La transcripción de voz en canales E2EE se maneja con un **userbot** separado.

## Características principales

- **/play con yt-dlp:** reproduce canciones o playlists desde YouTube.
- **Soundpad interactivo:** panel para reproducir clips locales organizados por carpetas.
- **Personas Gemini:** `/vapls` y `/indio` con respuestas en español.
- **Saludos automáticos:** reproduce un audio al entrar a un canal de voz.
- **Transcripción opcional:** userbot con Vosk para canales con DAVE/E2EE.
- **/transferir [dias]:** sube archivos de hasta 10 GB via web y comparte el link en Discord. TTL configurable (1-30 días, default 1).
- **HTTP API:** status, miembros, cola y reproducción de audio.

## Requisitos previos

- **Python 3.10+**
- **FFmpeg** (instalado en el sistema y accesible en el PATH)
- Un bot de Discord creado en el [Developer Portal](https://discord.com/developers/applications) con los siguientes **Privileged Gateway Intents** activos:
  - Guild Members
  - Message Content
- (Opcional) una cuenta de usuario de Discord para el userbot de transcripción.

## Instalación

1. **Clonar el repositorio:**

   ```bash
   git clone https://github.com/dilelu94/VaPls-Discord-Bot.git
   cd VaPls-Discord-Bot
   ```

2. **Instalar dependencias:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configurar variables de entorno:**
   Copia el archivo de ejemplo y rellena tu token de Discord:

   ```bash
   cp .env.example .env
   # Edita .env y añade tu TOKEN=tu_token_aqui
   ```

4. **Descargar modelos de Vosk:**
   El bot requiere modelos ligeros para funcionar. Descárgalos y extráelos en la carpeta `models/`:

   ```bash
   mkdir -p models
   # Modelo Español
   curl -L https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip -o models/es.zip
   unzip models/es.zip -d models/
   # Modelo Inglés
   curl -L https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o models/en.zip
   unzip models/en.zip -d models/
   # Limpieza
   rm models/es.zip models/en.zip
   ```

5. **Añadir audios locales (soundpad / saludos):**
   Configura `CUSTOM_AUDIO_PATH` y coloca tus archivos por carpetas.

## Uso

1. Inicia el bot:
   ```bash
   python bot.py
   ```
2. En Discord, usa los siguientes comandos de barra (Slash Commands):
   - `/play`: Busca o reproduce un link de YouTube.
   - `/soundpad`: Abre el panel de soundpad.
   - `/vapls`: Pregunta al bot Gemini.
   - `/indio`: Charla con el personaje con memoria.
   - `/parar`: Detiene reproducción y desconecta.
   - `/quit`: Desconecta sin tocar la cola.
   - `/transferir [dias]`: Genera un link para subir archivos (hasta 10 GB, role-gated, TTL configurable 1-30 días).

## Documentación

- [Arquitectura](docs/architecture.md)
- [Configuración](docs/configuration.md)
- [HTTP API](docs/api.md)
- [Comandos](docs/commands.md)
- [Operaciones](docs/operations.md)
- [Testing](docs/testing.md)
- [Contribución y docstrings](docs/contributing-docs.md)

## Doc generation

Los docstrings siguen estilo Google y se pueden renderizar con Sphinx +
napoleon. Pasos sugeridos en [docs/contributing-docs.md](docs/contributing-docs.md).

## CI/CD

- **CI:** GitHub Actions corre `pytest` sobre una matriz de Python 3.10–3.14 en cada push y pull request.
- **CD:** al pasar la CI en `master`, un job de deploy SSHea al server y corre `scripts/deploy.sh` (reset a `origin/master`, reinstala deps si cambiaron, reinicia los servicios y verifica que queden `active`). Detalle en [docs/operations.md](docs/operations.md#cicd-pipeline).

## Estructura del proyecto

- `bot.py`: Lógica principal, comandos y reproducción.
- `userbot/bot.py`: Transcripción de voz con Vosk.
- **Nota de Desarrollo:** Los comandos se programan en "vapls". El userbot es un usuario más; solo debe contener lógica propia para su funcionamiento como IA que simula ser una persona real o por limitaciones técnicas. Para interactuar con funciones del sistema, debe invocar los comandos de "vapls" programáticamente.
- `playCommand.py`: Cola de música y yt-dlp.
- `soundpadCommand.py`: UI de soundpad.
- `apiServer.py`: HTTP API.
- `transferCommand.py`: Lógica de transferencia de archivos (sesiones, chunks, HTML).
- `config.py`: Configuración por entorno.

## /transferir — cómo funciona

`/transferir [dias]` permite a miembros con el rol `@Main Characters` (configurable vía `TRANSFER_REQUIRED_ROLE`) compartir archivos pesados sin depender de Discord (límite 25 MB) ni servicios externos. El argumento opcional `dias` (1-30, default 1) define cuántos días estará disponible el archivo antes de borrarse. Cada click en el botón `➕` de la página suma 24h al tiempo restante sin superar el máximo inicial.

### Flujo

1. **Usuario ejecuta `/transferir [dias]`** → el bot crea una sesión con token único y responde con un link ephemeral: `http://<server>/upload/<token>`. El argumento `dias` (1-30, default 1) define el TTL máximo del archivo.
2. **Usuario abre el link** → página web con drag/click para subir archivos
3. **Subida por chunks** (10 MB cada uno, resumible):
   - POST `/upload/{token}/init` — inicia la sesión con filename y tamaño
   - POST `/upload/{token}/chunk/{idx}` — envía cada chunk
   - POST `/upload/{token}/complete` — finaliza y gatilla la notificación
4. **`uploadComplete` postea embed en Discord** → el bot envía un mensaje embed con el nombre del archivo y un botón **🔗 Descargar** al canal donde se ejecutó `/transferir`. La URL del botón se codifica correctamente (espacios → `%20`, etc.)
5. **Receptor hace click** → GET `/dl/{token}/{filename}` descarga el archivo

### Expiración

| Etapa                                     | TTL                                                                 | Comportamiento                                                  |
| ----------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------- |
| Sesión sin actividad (antes de completar) | `TRANSFER_SESSION_TTL` (default 5 min)                              | Se invalida, link muestra "Sesión expirada"                     |
| Post-completado                           | `TRANSFER_SESSION_TTL` (default 5 min)                              | Upload page deja de mostrar info, solo el link directo funciona |
| Archivo en disco                          | `dias` × 24 h (default 24 h, configurable vía `/transferir [dias]`) | El sweeper borra el archivo, el download link deja de funcionar |

El botón `➕` en la página extiende +24h el tiempo restante, sin superar el máximo definido en `dias`.

### Seguridad

- Role-gated: solo `@Main Characters` puede ejecutar `/transferir`
- Sesiones expiradas no muestran archivos, historial ni botón de borrar
- El endpoint `/upload/{token}/complete` postea la notificación **directamente** (sin polling) — no se pierde si el bot se reinicia entre la creación del link y la subida

### Configuración relevante (`.env`)

| Variable                 | Default                | Descripción                      |
| ------------------------ | ---------------------- | -------------------------------- |
| `TRANSFER_DIR`           | `transfers/`           | Directorio de almacenamiento     |
| `TRANSFER_MAX_SIZE`      | `15GB`                 | Límite duro por archivo (server) |
| `TRANSFER_DEFAULT_LIMIT` | `10GB`                 | Límite mostrado al usuario       |
| `TRANSFER_SESSION_TTL`   | `300` (5 min)          | TTL de sesión inactiva           |
| `TRANSFER_EXPIRY_HOURS`  | `24`                   | Horas antes de borrar archivo    |
| `TRANSFER_CHUNK_SIZE`    | `10MB`                 | Tamaño de cada chunk             |
| `TRANSFER_REQUIRED_ROLE` | `Main Characters`      | Rol que puede usar el comando    |
| `TRANSFER_BASE_URL`      | `http://141.148.84.55` | URL base para links de descarga  |

## Changelog reciente

### Resumen de subidas resilientes y verificación de integridad

- **`add_chunk()` escribe en la posición correcta**: se reemplazó `"ab"` (append) por `"r+b"` con fallback a `"wb"`, por lo que `seek()` ahora funciona correctamente para todos los índices. Los chunks fuera de orden o re-subidas ya no corrompen el archivo.
- **`init_upload()` no resetea sesiones activas**: agregado guard `if sess.filename` para evitar que una doble llamada borre el tracking de chunks recibidos.
- **`complete_upload()` verifica integridad**: ahora valida que `filename` no esté vacío, `total_size > 0`, y que la cantidad de chunks recibidos coincida con el expected. Previene que el sistema marque "completado" un upload incompleto.
- **Reintentos en el frontend**: el loop de subida reintenta cada chunk hasta 3 veces con 1s de backoff en errores de red, mejorando la tolerancia a conexiones inestables.
- **Tests**: 25 tests (antes 20) cubriendo los nuevos guards y la escritura posicional.

### TTL configurable en /transferir y extend +24h

- **`/transferir [dias]`**: nuevo argumento opcional (1-30, default 1) que define cuántos días vive el archivo antes de borrarse. `dias=0` equivale a 24h históricas.
- **Extender suma tiempo**: el botón `➕` en la página de archivos activos ahora suma +24h al tiempo restante en vez de resetear a 24h.
- **Tope por sesión**: el extensor no puede llevar el TTL restante más allá del máximo definido al crear la sesión (`dias` × 24 h).

### Seguridad

- **Eliminada SSH key huérfana** (`repos/ssh-oracle.key`) que no conectaba a ningún server
- **Destrackeado `.agents/settings.local.json`** de git (contenía rutas de keys e IP del server)
- **`.gitignore` reforzado**: `ssh*.key`, `.agents/settings.local.json`, y se cambió `data/` → `data/*` para permitir exclusiones específicas

### Datos de usuarios editables

- **`data/users.json`**: datos estáticos de los 15 usuarios, `GROUP_LORE` y `NON_DISCORD_MEMBERS` migrados desde `users.py` a un JSON editable sin tocar código
- **`users.py`** refactorizado para cargar desde `data/users.json` con fallback al diccionario hardcodeado. Las importaciones existentes (`from users import USERS`) siguen funcionando igual
- **`USERS_PATH`** agregado como variable de entorno (default `data/users.json`) en `config.py` y `.env.example`

### Calidad de aprendizaje del Indio

- **Prompt `_COMPRESS_SYSTEM`** mejorado con reglas de filtrado estrictas: no guardar saludos/despedidas, emojis solos, URLs, comandos al bot, info técnica, ni repeticiones
- **Filtro pre-compresión**: nueva función `_is_trivial()` que descarta mensajes de 1-2 palabras, solo emojis, salitudes y acuses de recibo antes de que lleguen a Gemini para comprimir
- **Deduplicación mejorada**: `_merge_user_dossiers` ahora usa comparación accent-insensitive para evitar duplicados semánticos (ej. "programador" ≈ "programador jr" no se duplica si ya existe)
- **Prompt del Indio** modificado para que sea más wholesome: incluye instrucciones de mandar buena onda, bancar a los amigos y reconocer logros, sin perder la personalidad divertida

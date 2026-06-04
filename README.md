# VaPls-Discord-Bot 🎙️🤖
[![CI](https://github.com/dilelu94/VaPls-Discord-Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dilelu94/VaPls-Discord-Bot/actions/workflows/ci.yml)

Bot de voz para Discord con reproducción de audio, soundpad y respuestas con Gemini. La transcripción de voz en canales E2EE se maneja con un **userbot** separado.

## Características principales
- **/play con yt-dlp:** reproduce canciones o playlists desde YouTube.
- **Soundpad interactivo:** panel para reproducir clips locales organizados por carpetas.
- **Personas Gemini:** `/vapls` y `/indio` con respuestas en español.
- **Saludos automáticos:** reproduce un audio al entrar a un canal de voz.
- **Transcripción opcional:** userbot con Vosk para canales con DAVE/E2EE.
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
- **Nota de Desarrollo:** Los comandos se programan en "vapls". El userbot es un usuario más y no debe contener lógica propia; para interactuar, debe invocar los comandos de "vapls" programáticamente mediante una función que llame al comando slash con argumentos.
- `playCommand.py`: Cola de música y yt-dlp.
- `soundpadCommand.py`: UI de soundpad.
- `apiServer.py`: HTTP API.
- `config.py`: Configuración por entorno.

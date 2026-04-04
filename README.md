# VaPls-Discord-Bot 🎙️🤖

Un bot de voz para Discord que escucha canales de voz en tiempo real y reacciona a palabras clave específicas en español e inglés utilizando el motor de STT **Vosk** (offline).

## Características principales
- **Detección bilingüe:** Escucha simultáneamente palabras clave en español ("necesito", "pito") e inglés ("i need", "whistle").
- **Privacidad y rendimiento:** Procesamiento local (offline) con Vosk. Usa un vocabulario restringido para minimizar el uso de CPU.
- **Grabación continua:** Utiliza `WaveSink` de Py-cord para capturar el audio de todos los usuarios en el canal.
- **Respuesta automática:** Reproduce un audio específico (`audio/necesitopito.*`) cuando se detecta una coincidencia.

## Requisitos previos
- **Python 3.10+**
- **FFmpeg** (instalado en el sistema y accesible en el PATH)
- Un bot de Discord creado en el [Developer Portal](https://discord.com/developers/applications) con los siguientes **Privileged Gateway Intents** activos:
  - Guild Members
  - Message Content

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

5. **Añadir audios:**
   Coloca el archivo de audio que deseas reproducir en la carpeta `audio/` con el nombre `necesitopito` (el bot detectará la extensión automáticamente, ej: `.mp3`, `.wav`).

## Uso

1. Inicia el bot:
   ```bash
   python bot.py
   ```
2. En Discord, usa los siguientes comandos de barra (Slash Commands):
   - `/escuchar`: El bot se une a tu canal de voz actual y comienza a monitorear.
   - `/parar`: El bot detiene la escucha y se desconecta.

## Estructura del proyecto
- `bot.py`: Lógica principal y manejo de voz.
- `keywords.py`: Lista de palabras clave y lógica de detección.
- `config.py`: Gestión de configuración y variables de entorno.
- `models/`: Directorio para los modelos de Vosk.
- `audio/`: Directorio para los archivos de respuesta sonora.

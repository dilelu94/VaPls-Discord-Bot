# VaPls-Discord-Bot - Documentación para Gemini 🎙️🤖

Este archivo sirve como referencia técnica y guía de desarrollo para que **Gemini** (y otros asistentes de IA) entiendan el funcionamiento interno, la arquitectura y las particularidades del proyecto **VaPls-Discord-Bot**.

---

## 📋 Descripción General
**VaPls-Discord-Bot** es un bot de voz para Discord que se conecta a canales de voz, escucha en tiempo real el audio de los usuarios, realiza procesamiento del habla a texto (STT - Speech to Text) local e interactúa de forma autónoma reproduciendo sonidos al detectar palabras clave específicas.

---

## 🛠️ Stack Tecnológico y Dependencias
- **Lenguaje:** Python 3.10+
- **Biblioteca de Discord:** `py-cord` (versión 2.8+ recomendada)
- **Motor STT:** `vosk` (procesamiento local offline)
- **Procesamiento de Audio:** `audioop` (módulo estándar de Python para manipulación de audio raw/PCM) y `FFmpeg`
- **Control de Entorno:** `python-dotenv`
- **Dependencias del Sistema:** `libopus` (para codificación/decodificación de audio de Discord) y `FFmpeg` (para reproducción/conversión de audio).

---

## 📂 Arquitectura y Estructura de Archivos

- [bot.py](file:///var/home/dilelu/repos/vapls-discord-bot/bot.py): Punto de entrada y núcleo del bot. Maneja la conexión con Discord, implementa los slash commands, la captura de audio en tiempo real y el procesamiento de Vosk.
- [config.py](file:///var/home/dilelu/repos/vapls-discord-bot/config.py): Carga variables de entorno desde `.env` (ej. token de Discord, rutas de los modelos de Vosk y ruta del directorio de audios).
- [keywords.py](file:///var/home/dilelu/repos/vapls-discord-bot/keywords.py): Contiene las palabras clave vigiladas y la lógica para verificar si alguna coincide en el texto reconocido.
- **`models/`**: Carpeta que debe contener los modelos offline de Vosk descargados:
  - `vosk-model-small-es-0.42` para español.
  - `vosk-model-small-en-us-0.15` para inglés.
- **`audio/`**: Directorio donde se almacena el archivo `necesitopito.*` que se reproducirá en respuesta.
- [run.sh](file:///var/home/dilelu/repos/vapls-discord-bot/run.sh) / [runMonitored.sh](file:///var/home/dilelu/repos/vapls-discord-bot/runMonitored.sh) / [autoRestart.sh](file:///var/home/dilelu/repos/vapls-discord-bot/autoRestart.sh): Scripts auxiliares para el inicio, redirección de logs (`botOutput.log`, `monitorAutoKill.log`) y reinicio automático en caso de caída.

---

## 🔬 Detalles de Implementación Clave

### 1. Parches de Estabilidad (Monkey Patches)
Debido a la estabilización del protocolo de cifrado de extremo a extremo **DAVE** en Discord y posibles desconexiones, el bot parchea dos comportamientos críticos en `py-cord`:
- **`discord.opus.PacketDecoder._decode_packet`**: Captura errores `OpusError` del tipo "corrupted stream" o "invalid argument" durante el handshake de DAVE, devolviendo en su lugar 20ms de silencio PCM (evita que la grabación se rompa de inmediato al inicio).
- **`discord.VoiceClient.stop_recording`**: Silencia cualquier excepción lanzada durante el apagado del flujo de grabación para garantizar una desconexión limpia.

### 2. Receptor de Audio y Pipeline de Procesamiento (`KeywordDetectorSink`)
El bot hereda de `discord.sinks.WaveSink` para recolectar el audio PCM de cada usuario en el canal de voz.
El flujo de audio por cada usuario es:
1. **Captura:** Captura audio PCM de 48kHz, stereo, 16 bits (representa 960 muestras por canal en frames de 20ms).
2. **Conversión a Mono:** Convierte el PCM stereo a mono usando `audioop.tomono`.
3. **Resampling:** Reduce la frecuencia de muestreo de 48kHz a 16kHz utilizando `audioop.ratecv` (requerido por los modelos de Vosk).
4. **Vosk STT:** Se envía el flujo resampleado a `KaldiRecognizer`. Para optimizar la CPU, se restringe el vocabulario de reconocimiento de palabras a:
   - Español: `["necesito", "pito", "[unk]"]`
   - Inglés: `["i need", "whistle", "[unk]"]`
5. **Detección:** Si el texto parcial o final coincide con las palabras clave (definidas en `keywords.py`), se gatilla la reproducción de audio.

### 3. Evitar Colisiones de Audio
Para evitar que el audio de respuesta sea procesado de nuevo por el propio bot o se solape con sí mismo:
- `trigger_audio` verifica si `self.vc.is_playing()` es verdadero; si es así, aborta la reproducción.

---

## 🛠️ Comandos de Discord (Slash Commands)
- `/escuchar`: Une al bot al canal de voz actual del usuario. Estabiliza la conexión, inicializa los sumideros de audio y arranca la escucha.
- `/parar`: Detiene la grabación y desconecta limpiamente al bot del canal de voz.

---

## 🧪 Pruebas Unitarias
El proyecto cuenta con suites de pruebas usando la biblioteca `unittest`:
- [testConfig.py](file:///var/home/dilelu/repos/vapls-discord-bot/testConfig.py): Valida la correcta carga de variables de entorno y configuración.
- [testKeywords.py](file:///var/home/dilelu/repos/vapls-discord-bot/testKeywords.py): Valida que la lógica de búsqueda en español e inglés detecte las palabras clave y descarte las oraciones sin coincidencias.

Para ejecutar los tests, corre:
```bash
python3 -m unittest testConfig.py testKeywords.py
```

---

## 💡 Guía de Modificación para Gemini
Al modificar este repositorio, sigue estas directrices:
1. **Mantener Parches de Opus:** No alteres el monkeypatching al inicio de `bot.py` a menos que sea para solucionar fallos directos con DAVE o compatibilidad con versiones superiores de `py-cord`.
2. **Optimización de Vocabulario:** Si añades nuevas palabras clave en `keywords.py`, asegúrate de actualizar también `self.vocab_es` y `self.vocab_en` en `KeywordDetectorSink.__init__` para que Vosk las incluya en su diccionario de predicción restringido, evitando picos de CPU inesperados.
3. **Manejo de Libopus:** El bot intenta cargar `libopus` de forma dinámica para ser compatible con entornos Linux tradicionales y distribuciones donde la librería puede tener nombres ligeramente diferentes.
4. **Respeto a Variables de Entorno:** Cualquier nueva configuración del bot o de los modelos de IA debe incorporarse en `config.py` y documentarse en `.env.example`.

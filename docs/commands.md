# Slash commands

| Command | Behavior | Subsystems touched |
| --- | --- | --- |
| `/play` | Busca o recibe una URL de YouTube, descarga con yt-dlp y reproduce en voz. Si la búsqueda devuelve varios resultados, muestra un menú para que quien pidió elija cuál; una URL se reproduce directo. | `playCommand`, `config`, `analytics`, FFmpeg |
| `/parar` | Detiene la reproducción, limpia la cola y desconecta. | `pararCommand`, `playCommand`, `analytics` |
| `/soundpad` | Abre el panel de Soundpad para reproducir clips locales. | `soundpadCommand`, `config`, `analytics` |
| `/vapls` | Pregunta al bot Gemini sin memoria. | `geminiCommand`, `geminiClient`, `analytics` |
| `/indio` | Conversación con memoria corta por guild + memoria de largo plazo (rasgos, anécdotas, chistes internos) destilada por Gemini. | `geminiCommand`, `geminiClient`, `analytics` |
| `/quit` | Desconecta el bot del canal de voz sin tocar la cola. | `bot.py`, `analytics` |
| `/restart` | Reinicia el proceso del bot (dev-only). | `bot.py`, `analytics` |

Notas:
- Los comandos de reproducción disparan saludos cuando el bot entra a voz.
- Si `GEMINI_API_KEY` no está configurado, `/vapls` y `/indio` fallarán con un mensaje de error.
- **Desambiguación de música:** cuando se pide un tema y la búsqueda en YouTube devuelve
  varios resultados, en vez de reproducir el primero a ciegas se ofrecen las opciones
  (numeradas con emojis 1️⃣2️⃣3️⃣). Una URL directa siempre se reproduce sin preguntar.
  - **`/play`:** menú desplegable; elige al instante **quien corrió el comando**.
  - **El Indio (voz/chat):** abre una **votación** que cierra cuando pasan
    `_MUSIC_VOTE_WINDOW_SEC` (5 s por defecto) **sin votos nuevos** — cada voto
    reinicia la cuenta regresiva, así un voto al segundo 4 le da otros 5 s a
    quien quiera reaccionar. **Cualquiera** del canal vota diciendo/escribiendo
    el número; al cerrarse gana la **más votada** (empate → número más bajo; si
    nadie votó → la primera/más relevante).

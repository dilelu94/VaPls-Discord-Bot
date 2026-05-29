# Slash commands

| Command | Behavior | Subsystems touched |
| --- | --- | --- |
| `/play` | Busca o recibe una URL de YouTube, descarga con yt-dlp y reproduce en voz. | `playCommand`, `config`, `analytics`, FFmpeg |
| `/parar` | Detiene la reproducción, limpia la cola y desconecta. | `pararCommand`, `playCommand`, `analytics` |
| `/soundpad` | Abre el panel de Soundpad para reproducir clips locales. | `soundpadCommand`, `config`, `analytics` |
| `/vapls` | Pregunta al bot Gemini sin memoria. | `geminiCommand`, `geminiClient`, `analytics` |
| `/indio` | Conversación con memoria corta por guild + memoria de largo plazo (rasgos, anécdotas, chistes internos) destilada por Gemini. | `geminiCommand`, `geminiClient`, `analytics` |
| `/quit` | Desconecta el bot del canal de voz sin tocar la cola. | `bot.py`, `analytics` |
| `/restart` | Reinicia el proceso del bot (dev-only). | `bot.py`, `analytics` |

Notas:
- Los comandos de reproducción disparan saludos cuando el bot entra a voz.
- Si `GEMINI_API_KEY` no está configurado, `/vapls` y `/indio` fallarán con un mensaje de error.

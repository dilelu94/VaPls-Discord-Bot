# Slash commands

| Command | Behavior | Subsystems touched |
| --- | --- | --- |
| `/play` | Busca o recibe una URL de YouTube, descarga con yt-dlp y reproduce en voz. Si la búsqueda devuelve varios resultados, muestra un menú para que quien pidió elija cuál; una URL se reproduce directo. | `playCommand`, `config`, `analytics`, FFmpeg |
| `/dj` | **Activa el modo DJ en un paso** (requiere haber puesto música antes) y postea el panel de control **en el mismo canal donde se corrió**. El panel tiene botones para vetar la sugerencia, reproducirla ya, o apagar el modo. | `playCommand`, `bot.py`, `config` |
| `/parar` | Detiene la reproducción, limpia la cola y desconecta. | `pararCommand`, `playCommand`, `analytics` |
| `/soundpad` | Abre el panel de Soundpad para reproducir clips locales. | `soundpadCommand`, `config`, `analytics` |
| `/vapls` | Pregunta al bot Gemini sin memoria. | `geminiCommand`, `geminiClient`, `analytics` |
| `/indio` | Conversación con memoria corta por guild + memoria de largo plazo (rasgos, anécdotas, chistes internos) destilada por Gemini. | `geminiCommand`, `geminiClient`, `analytics` |
| `/banana` (En desuso / Inactivo) | Comando desactivado temporalmente debido a bloqueos de seguridad de Google. El código está archivado y comentado en `geminiImage_legacy.py` y `geminiImage.py`. | `geminiImage`, Playwright |
| `/sugerencias` | Manda una idea/feature. Gemini Flash-Lite la categoriza (la agrupa con ideas parecidas o abre un grupo nuevo) y **solo se persiste si logró categorizar**; el usuario recibe feedback de a qué grupo quedó (y cuántos lo pidieron antes). | `suggestionsCommand`, `geminiClient`, `analytics` |
| `/sugerencias-ver` | Lista los grupos de sugerencias existentes, ordenados por las más pedidas. | `suggestionsCommand`, `analytics` |
| `/quit` | Desconecta el bot del canal de voz sin tocar la cola. | `bot.py`, `analytics` |
| `/entraindio` | Hace que el userbot (Indio) entre al canal de voz del invocador. | `bot.py`, userbot relay `/join` |
| `/sensibilidad` `1\|2\|3\|4` | Cambia la sensibilidad del wake-word del Indio. Preset 1 = más sensible: `che indio`, `que indio`, `eh indio` + verbos. Preset 2 = solo `che indio` + verbos (reduce falsos positivos de "que"). Preset 3 = re-habilita `che/que/eh indio` pero usa pool grande de frases señuelo en la gramática VOSK para reducir falsos positivos; editable a mano vía `_PRESET_3_FILLER`. Preset 4 = mismo VOSK que el 2 (`che indio` + verbos, gramática chica), pero agrega una segunda capa: después de que VOSK dispara, corre un pase corto de Whisper sobre el prebuffer y descarta el evento si Whisper no detecta "indio". Estricto por diseño. Es el **default**. El preset es in-memory y se resetea a 4 al reiniciar el userbot. | `bot.py`, userbot relay `/sensibilidad` |
| `/restart` | Reinicia el proceso del bot (dev-only). | `bot.py`, `analytics` |

Notas:
- Los comandos de reproducción disparan saludos cuando el bot entra a voz.
- Si `GEMINI_API_KEY` no está configurado, `/vapls` y `/indio` fallarán con un mensaje de error.
- **Desambiguación de música:** cuando se pide un tema y la búsqueda en YouTube devuelve
  varios resultados, en vez de reproducir el primero a ciegas se ofrecen las opciones
  (numeradas con emojis 1️⃣2️⃣3️⃣). Una URL directa siempre se reproduce sin preguntar.
  - **`/play`:** menú desplegable; elige al instante **quien corrió el comando**.
  - **El Indio (voz/chat):** abre una **votación** que cierra cuando pasan
    `_MUSIC_VOTE_WINDOW_SEC` (30 s por defecto) **sin votos nuevos** — cada voto
    reinicia la cuenta regresiva, así un voto al segundo 29 le da otros 30 s a
    quien quiera votar. **Cualquiera** del canal vota, y se puede votar de
    **tres formas, que se combinan en el mismo conteo**: hablando, escribiendo
    el número, o **reaccionando** con el emoji del número (el bot siembra las
    reacciones 1️⃣2️⃣3️⃣ en el mensaje de opciones). Un voto por persona
    (la reacción y el texto del mismo usuario cuentan una sola vez). Al cerrarse
    gana la **más votada** (empate → número más bajo; si nadie votó → la
    primera/más relevante). El conteo de reacciones requiere que el bot
    principal tenga el intent de reacciones (incluido en `Intents.default()`).

## 🎧 Auto-DJ del Indio

Cuando el Auto-DJ está activo y la cola se vacía, **el Indio elige el próximo
tema** en vez de dejar morir la música. **No gasta tokens de IA extra**: la
música la elige YouTube (Mix/radio del último tema o búsqueda del mismo artista)
y las frases del Indio salen de un banco pre-escrito.

**Cómo activar el modo DJ (dos vías — ambas lo prenden en un solo paso):**
- **`/dj`** — lo activa directo y postea el panel **en el mismo canal donde lo
  corrés**. **No se activa en frío**: necesita un tema sonando o en historial
  (poné algo con `/play` primero).
- **Pedíselo al Indio en el chat de texto** — escribí algo como *"indio hacé de
  DJ"*, *"mode DJ"*, *"ponete a pinchar"*, *"pone música en automático"*, etc.
  El Indio lo detecta vía su sistema de tools de Gemini y lo activa, posteando el
  panel en el canal donde está charlando. **No funciona por voz** — el path de
  voz del Indio no tiene lógica de DJ.

El panel se postea en el canal de invocación (no en un canal fijo). El Auto-DJ
y sus sugerencias quedan ahí. `AUTODJ_MENU_CHANNEL_ID` se usa solo como fallback
si no se puede resolver el canal.

**El panel** tiene tres botones:
- **🚫 Vetar sugerencia** — descarta la sugerencia actual y busca otra del mismo
  artista.
- **▶️ Poner ya** — salta la espera de `AUTODJ_GRACE_SECONDS` y pone la sugerencia
  ahora.
- **⏹️ Cortar DJ** — apaga el modo DJ.

**Sugerencia + veto:** al vaciarse la cola, el Indio muestra el tema que va a
poner y abre una ventana de `AUTODJ_GRACE_SECONDS`. Si nadie hace nada, lo pone.
El botón 🚫 busca otro **del mismo artista**.

**Apagado automático:**
- Tras `AUTODJ_MAX_CHAIN` temas seguidos → se apaga solo.
- Si se va todo el mundo del canal de voz → cancela lo pendiente y sigue el
  camino normal de fin de cola.

**Filtros:** nunca propone temas de más de 10 minutos ni algo que ya sonó en la
sesión. El motor normal es el Mix/radio de YouTube (respeta el mood); el veto
cae a búsqueda del mismo artista.

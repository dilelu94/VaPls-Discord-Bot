"""Slash command logic for /vapls and /indio.

Both commands ask Google Gemini for a reply. /vapls is stateless (no memory).
/indio keeps a short verbatim conversation history per guild PLUS a compressed
"long-term" memory (rasgos por usuario, anécdotas, chistes internos). When the
history grows past a threshold, the oldest turns are distilled into the
long-term notes via a separate Gemini call (fire-and-forget) before being
discarded — so the indio feels like a friend that remembers the group.

Depends on geminiClient and analytics.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import Optional
from urllib.parse import urljoin

import base64 as _b64
import aiohttp
import discord

import analytics
import config
import geminiClient
import gemini_keywords as _kw
import geminiKeys
import imageManager

try:
    from users import USERS as _USERS
except Exception:
    _USERS: dict[int, dict] = {}

try:
    from users import GROUP_LORE as _GROUP_LORE
except Exception:
    _GROUP_LORE: dict[str, list[str]] = {}

try:
    from users import NON_DISCORD_MEMBERS as _NON_DISCORD_MEMBERS
except Exception:
    _NON_DISCORD_MEMBERS: list[dict] = []

logger = logging.getLogger("bot.gemini")

# Strong-ref set for fire-and-forget background tasks. Without this, CPython
# can GC the Task object before it finishes (the event loop holds only a weak
# reference). Symptom seen in the wild: indio dispatches PLAY_MUSIC, the task
# vanishes mid-flight, and no music plays without any error in the logs.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


VAPLS_SYSTEM = """\
Sos el bot del servidor de Discord "VaPls". Tu rol es ayudar a los amigos del \
server con preguntas, traducciones, datos curiosos o lo que necesiten. Sos \
amigable, directo, y respondés en español rioplatense (voseo). Usás emojis con \
moderación: uno o dos por respuesta máximo. Tus respuestas son concisas: por \
defecto no más de 4 párrafos cortos. Si te piden código, lo devolvés bien \
formateado en bloques de Discord (```lang ... ```). No inventás información: \
si no sabés algo, lo decís. No tenés acceso a internet en tiempo real ni al \
estado del servidor. No te hagas pasar por un humano: sos un bot y está bien \
que se note.
"""


def _fmt_trigger(tool: str) -> str:
    return " / ".join(f'"{p}"' for p in _kw.SYSTEM_TRIGGERS[tool])


INDIO_SYSTEM = f"""\
Sos el indio, un amigo más del grupo en este server de Discord: charlatán, \
divertido, con buena onda. Importante: VaPls es un bot, el bot oficial del \
grupo que corre los comandos como /play, /vapls, /indio, etc. NO trates a \
VaPls como persona. \

Sos bastante más grande que el grupo: tenés más de 30 años más que cualquiera \
de tus amigos, sos el viejo veterano de la barra. Eso lo podés referenciar \
con onda cuando viene al caso (sin restregarlo en cada mensaje). \

Cada usuario del grupo tiene un APODO (lo ves antes de cada mensaje, \
ej. "Miles: ...") y, para varios, un NOMBRE REAL distinto (aparece en sus \
rasgos como "nombre real: X"). Algunos tienen además apodos alternativos \
listados como "apodos: X, Y, Z". Llamalos SIEMPRE por el apodo (el principal \
o cualquiera de los alternativos). \
Contexto interno (NO lo expliques en el chat a menos que la charla lleve \
directo ahí): a nadie del grupo le gusta que lo llamen por el nombre real. \
Es algo que tenés en cuenta para no meter la pata, no algo que andás \
contando. \

Algunos rasgos vienen prefijados con "(privado, no mencionar)": son contexto \
para vos, te ayudan a responder coherente, pero NO los digas explícitamente \
en el chat. \

PRINCIPIO GENERAL para toda la info que tenés del grupo (rasgos, anécdotas, \
chistes internos, lo que sea): es para que RAZONES y formules respuestas \
coherentes con quién es cada uno, no para recitarla. No andes diciendo "ah \
vos sos el de Quilmes, el bombero" o "Miles el programador de Independiente" \
cada vez que te hablan — eso es robótico y queda raro. Usá esa info como \
trasfondo que tiñe tus respuestas (vocabulario, referencias, qué chistes \
hacer con quién, qué temas evitar) y mencionalas solo cuando la conversación \
lo pide naturalmente. \

REGLAS ESTRICTAS para tools de música/sonido: NO uses NINGUNA tool a menos \
que el usuario te esté dando una orden DIRECTA de reproducción. Preguntas, \
opiniones, menciones de artistas, o charla general sobre música → respondé \
SOLO con texto, sin llamar tools. \

Únicos casos en que llamás una tool: \
- {_fmt_trigger("play_music")} \
  con verbo de orden + QUÉ reproducir → `play_music` \
- {_fmt_trigger("play_sound")} \
  con verbo de orden + nombre del clip → `play_sound` \
- {_fmt_trigger("skip_music")} → `skip_music` \
- {_fmt_trigger("pause_music")} → `pause_music` \
- {_fmt_trigger("resume_music")} → `resume_music` \
- {_fmt_trigger("stop_music")} → `stop_music` \
- {_fmt_trigger("dj_mode")} → `dj_mode`
- {_fmt_trigger("spacewar_guide")} → `spacewar_guide` \
- {_fmt_trigger("use_image")} → `use_image` \

"play" / "metele play" / "pone play" sin artista → NUNCA es play_music, \
es resume_music. \

Si el usuario te pide música, podés usar la herramienta `play_music` o simplemente escribir el comando `/play <tema>` en tu respuesta; el sistema se encargará de que el bot VaPls lo reproduzca. \n\nUna sola tool por mensaje. Confirmación breve ("tomá", "va", "salteo") \
solo si la vas a llamar — sin chamuyo. Si NO llamás tool, NO digas \
frases de confirmación — respondé charla normal. \
Nunca digas "no puedo" ni "no me anda". \
\
Hablás español rioplatense bien casual (voseo, modismos argentinos, muletillas \
como "che", "boludo" usado con afecto, "posta", "una banda", "de una"). \
No escribas acciones escénicas, acotaciones ni pensamientos entre paréntesis \
o asteriscos. Respondé solo el mensaje que mandarías al chat. \
\
Estás en un chat grupal con varios amigos a la vez. Cada mensaje del grupo te \
llega con el formato "nombre: contenido" donde "nombre" es quién habla. Te \
acordás de quién dijo qué y podés referirte a alguien por su nombre si hace \
falta. NO empieces tus respuestas con tu nombre ni con el nombre de otro (ni \
"indio:", ni "Miles:", ni "[indio]:", ni "[Miles]:", ni nada parecido): \
hablás directo, como el indio. NO repitas ni parafrasees la pregunta que te \
hicieron — contestá directo, sin citar lo que te dijeron entre comillas, sin \
poner "> ..." citando, y sin reformular el pedido antes de responder. Si \
necesitás referenciar algo de lo que dijeron, usá una frase nueva y corta. \
Si te hablan a vos directamente, respondé a esa persona; si te preguntan por \
otra, contestá lo que sepas de la conversación previa. \
\
Si el contenido del mensaje empieza con el marcador "[voz]" (ej. \
"Miles: [voz] indio para la musica"), eso significa que viene de una \
transcripción de voz hecha por un ASR (Whisper) y puede tener errores \
fonéticos, palabras mal entendidas, verbos partidos pegados a "indio" \
("indio de tener a música" = "che indio, detené la música"), nombres propios \
castellanizados ("líneas horarias" = "Indio Solari"), o dígitos que sonaron \
como palabras. Tolerá esos errores: inferí qué intentó decir el hablante en \
español rioplatense natural y respondé en consecuencia, sin pedir que repita \
ni explicar que no entendiste. Si te dan una orden con un dígito ("indio \
tirala 4"), preservá el número — es una respuesta a una votación abierta. \
El marcador "[voz]" es solo una pista para vos: NO lo repitas en la \
respuesta. \
\
Sos opinable pero no agresivo. Si la conversación se pone densa o trolleadora, \
seguís el chiste sin pasarte de mambo. Si te preguntan si sos un bot, te reís — te \
parece una pregunta rarísima, obvio que no sos un robot. Seguís la charla como \
si nada. Mantenés respuestas cortas, como en chat real: 1 a 3 oraciones la \
mayoría de las veces. Solo te extendés si la \
pregunta lo amerita (explicar algo técnico, contar una anécdota). Tirás algún \
emoji cada tanto, como un pibe en un chat real: ni en cada mensaje ni nunca, \
alguno suelto cuando viene al caso (😂, 👀, 🤡, 🙏, 🔥, 💀, etc.). Si el \
server tiene emojis custom, los CONOCÉS — más abajo te paso la lista — y los \
podés usar pegando EXACTAMENTE el código "<:nombre:id>" (o "<a:nombre:id>" si \
es animado) tal cual aparece en esa lista. Discord solo los renderiza si \
copiás el código completo con los "<", ":" e "id" numérico. No inventes ids \
ni uses ":nombre:" pelado, no funciona. Si te preguntan si viste tal o cual \
emoji o "los nuevos emojis del server", mirá la lista de abajo y respondé en \
base a eso — no hagas el bobo si los tenés a mano, tirá uno o dos pegando el \
código y listo.
"""

_INDIO_TOOLS = [
    {
        "name": "play_music",
        "description": (
            "Reproducir un tema NUEVO en el canal de voz. \n"
            "REQUISITO DURO: el mensaje DEBE tener (1) un verbo de orden "
            "explícito — " + ", ".join(_kw.PLAY_MUSIC_VERBS) + " — Y (2) QUÉ "
            "reproducir (artista, canción, o 'tema'). \n"
            "'Sacá' tampoco — sacar/quitar música es stop_music. \n"
            "NO uses esta tool para comandos de control (saltear, pausar, "
            "etc.) ni para clips del soundpad. \n"
            "Si solo dicen 'play' / 'pone play' / 'metele play' "
            "SIN artista, NUNCA es play_music — es resume_music."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Búsqueda en YouTube o URL. Usá lo que dijeron tal "
                        "cual (ej: nombre del artista, canción, o género). "
                        "Si hay varios resultados, el sistema "
                        "le pregunta al que pidió cuál quiere; si es una URL "
                        "la reproduce directo. No elijas vos el tema."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "play_sound",
        "description": (
            "Reproducir un clip corto del soundpad (audio meme/efecto) en "
            "el canal de voz. \n"
            "Hay DOS casos válidos para llamarla:\n"
            "CASO A (orden explícita): el mensaje tiene (1) un verbo "
            "explícito de orden — "
            + ", ".join(_kw.PLAY_SOUND_VERBS)
            + " — Y (2) un nombre/keyword "
            "concreto del clip. Acá el clip es la respuesta principal.\n"
            "CASO B (lo nombran sin pedirlo): alguien dice TEXTUALMENTE el "
            "nombre/keyword de un clip que existe pero sin verbo de orden. "
            "Acá PRIMERO respondé normal a lo que dijeron (tu texto de "
            "siempre) y ADEMÁS llamás play_sound para que el clip salga como "
            "yapa/extra. Nunca reemplaces tu respuesta por el clip en este "
            "caso. \n"
            "FUERA DE ESOS DOS CASOS NO la llames: si no hay verbo de orden y "
            "tampoco nombran un clip que exista, NO inventes un audio para "
            "'comentar' la charla — solo respondé texto. \n"
            "Si falta el verbo de orden, NO uses esta tool aunque mencionen "
            "una palabra que matchee con un clip del soundpad. Que alguien "
            "diga un nombre de clip en medio de una conversación NO "
            "significa que quieran que toques ese audio — están hablando del "
            "tema. Solo cuando hay un imperativo explícito pidiendo "
            "reproducirlo, llamás esta tool. \n"
            "Si falta el nombre concreto del clip (solo dicen 'pone un audio' "
            "sin más), tampoco la uses. \n"
            "Ejemplos VÁLIDOS: 'tirá el de las risas', "
            "'hacé sonar el de aplausos'. \n"
            "Ejemplos INVÁLIDOS (NO llamar play_sound): mencionar un clip "
            "sin pedirlo ('che ese audio del otro día estaba bueno'), "
            "preguntar por sonidos ('cuál es tu meme favorito?'), "
            "o hablar del soundpad sin dar una orden."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {
                    "type": "STRING",
                    "description": (
                        "Nombre o palabra clave del clip (fuzzy match). "
                        "Ej: 'risas', 'aplausos'."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "skip_music",
        "description": (
            "Saltear el tema actual. "
            "Usala cuando piden " + ", ".join(f"'{v}'" for v in _kw.SKIP_VERBS) + "."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "pause_music",
        "description": (
            "Pausar la música. "
            "Usala cuando piden " + ", ".join(f"'{v}'" for v in _kw.PAUSE_VERBS) + "."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "resume_music",
        "description": (
            "Despausar la música. "
            "Usala cuando piden "
            + ", ".join(f"'{v}'" for v in _kw.RESUME_VERBS)
            + " SIN artista."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "stop_music",
        "description": (
            "Parar la música y vaciar la cola. "
            "Usala cuando piden " + ", ".join(f"'{v}'" for v in _kw.STOP_VERBS) + "."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "dj_mode",
        "description": (
            "Activar el modo DJ. "
            "Usala cuando el grupo pide "
            + ", ".join(f"'{v}'" for v in _kw.DJ_VERBS)
            + "."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "generate_image",
        "description": (
            "Generar una imagen a pedido del usuario. "
            "Úsala únicamente cuando el usuario ordene explícitamente generar o hacer una imagen "
            "(ej: 'generá una imagen de...', 'haceme una imagen de...'). "
            "Debes redactar un prompt en español muy descriptivo y detallado para la generación, "
            "incorporando de forma inteligente y detallada los rasgos físicos, descripción o aspecto "
            "de las personas del grupo de amigos si son nombradas en el pedido (por ejemplo, si piden "
            "una imagen de 'Viny' y en tu memoria sabes que es pelado, flaco y de cara graciosa, "
            "describe a un hombre joven con esas características)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {
                    "type": "STRING",
                    "description": (
                        "El prompt detallado en español para generar la imagen. "
                        "Debe incorporar los rasgos físicos y aspecto del usuario/amigo del lore si es mencionado."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "edit_image",
        "description": (
            "Crear una imagen parecida o editar la imagen proporcionada por el usuario según sus indicaciones. "
            "Úsala únicamente cuando el usuario responda a una imagen existente (que tú puedes ver) "
            "y ordene explícitamente editarla, modificarla, o hacer una imagen parecida a esa "
            "(ej: 'haceme una imagen como esta pero con...', 'editame esta foto...'). "
            "Debes redactar un prompt en español enfocado principalmente en los cambios, agregados o el estilo deseado "
            "para la nueva imagen (ej: 'un hombre usando un gorrito de lana'). IMPORTANTE: NO describas de forma detallada "
            "los rasgos físicos del sujeto original o del fondo que no deben cambiar (como si es pelado, su sonrisa, etc.), "
            "ya que el modelo de edición de imagen-a-imagen los deformará si se especifican textualmente."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {
                    "type": "STRING",
                    "description": (
                        "El prompt en español enfocado únicamente en los cambios, agregados o estilo solicitado "
                        "(ej: 'un hombre con un gorrito de lana'). No repitas descripciones físicas de lo que no cambia."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "spacewar_guide",
        "description": (
            "Explicar cómo obtener Spacewar (appid 480, la app de testing de Valve) "
            "gratis en la biblioteca de Steam. "
            "Usala SOLO cuando el usuario pregunte explícitamente cómo instalar, tener, agregar "
            "o conseguir Spacewar en Steam, o mencione steam://run/480. "
            "No la uses para hablar de juegos retro ni del Spacewar original de 1962. "
            "No generes texto adicional — llamá la tool y el sistema se encarga de la guía."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "use_image",
        "description": (
            "Mostrar una imagen de la colección del grupo en el chat. "
            "Usala cuando sea contextualmente relevante (un momento gracioso, "
            "una referencia visual, una imagen que un amigo compartió antes). "
            "Elegí la imagen que mejor matchee con la conversación. "
            "Siempre fijate en [IMÁGENES DISPONIBLES] arriba para saber IDs y descripciones."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "image_id": {
                    "type": "STRING",
                    "description": "ID de la imagen en el catálogo (ej: 'f47ac10b-58cc-4372-a567-0e02b2c3d479')",
                },
                "caption": {
                    "type": "STRING",
                    "description": "Texto opcional para acompañar la imagen (ej: 'mirá esta joyita'). Vacío si no querés texto.",
                },
            },
            "required": ["image_id"],
        },
    },
]


_STORED_MSG_MAX_CHARS = 1500
_HISTORY_TTL_SEC = 6 * 3600
_DISCORD_CHUNK_LIMIT = 1990
_MAX_CHUNKS = 4

# Aviso visible cuando geminiClient rota a otra key tras un 429. Se edita en el
# deferred del invoker para que el usuario sepa que hubo un problema transitorio
# y el sistema sigue intentando. Cuando llega la respuesta final, este texto se
# reemplaza por el primer chunk del reply (no queda pista en el output final).
AVISO_ROTACION_GEMINI = "⏳ Aguantame, estoy cambiando de key…"

# Short-term history bounds (in turns; each /indio call appends 2 turns).
# When history grows past the threshold we kick off a compression task that
# distills the oldest turns into the long-term notes. HARD_CAP is the safety
# slice that bounds RAM if compression keeps failing.
_HISTORY_COMPRESS_THRESHOLD = 30  # ~15 mensajes user + 15 model
_HISTORY_KEEP_AFTER_COMPRESS = 14  # se queda con los ~7 más recientes user+model
_HISTORY_HARD_CAP = 50

# Long-term memory bounds.
_LONG_TERM_MAX_CHARS = 8000  # JSON dumpeado no debe pasar de esto
_LT_TRAITS_PER_USER = 5
_LT_QUESTIONS_PER_USER = 5
_LT_ANECDOTES_PER_USER = 5
_LT_GROUP_EVENTS = 10
_LT_JOKES = 10

_indio_history: dict[str, list[dict]] = {}
_indio_last_seen: dict[str, float] = {}

# How old a turn has to be (in seconds) before we tag it with a "(hace X)"
# prefix when feeding it back to Gemini. Without this, the model has no temporal
# cue and confuses last week's "te pasé esta lista" with the current convo.
# Parens (not brackets) intentional — brackets in the prompt teach the model
# to echo "[Name]:" speaker-tag patterns in its own replies.
_HISTORY_AGE_TAG_THRESHOLD_SEC = 15 * 60  # 15 minutes

# ---------------------------------------------------------------------------
# Image collection via DM
# ---------------------------------------------------------------------------

import time as _time
from dataclasses import dataclass, field
from typing import Optional as _Optional

_IMAGE_SESSION_TIMEOUT = 300  # 5 min sin respuesta → se cancela
_INDIO_IMAGE_ROLE = (
    "Main Characters"  # rol requerido para enviar imágenes al Indio por DM
)


def _can_send_indio_images(
    member: "_Optional[discord.Member]",
    role_name: str = _INDIO_IMAGE_ROLE,
) -> tuple[bool, str]:
    """Check if a guild member can send images to the Indio via DM.

    Returns (True, '') if allowed, or (False, 'mensaje de error') if denied.
    Pure function — no Discord API calls, no side effects. Testable by
    passing MagicMock objects for ``member``.
    """
    if member is None:
        return False, "❌ Solo miembros de la guild pueden mandarme fotos por DM."
    if not discord.utils.get(member.roles, name=role_name):
        return (
            False,
            f"❌ Solo usuarios con el rol @{role_name} pueden mandarme fotos por DM.",
        )
    return True, ""


@dataclass
class _PendingImage:
    attachment: "discord.Attachment"
    original_filename: str


class _ImageDMSession:
    """State machine for collecting images one-by-one via DM."""

    def __init__(self, author_id: int, images: list["discord.Attachment"]):
        self.author_id = author_id
        self.pending = [_PendingImage(a, a.filename) for a in images]
        self.total = len(images)
        self.processed: list[dict] = []
        self.current_index = 0
        # stage: confirm | waiting_desc | confirm_save | done
        self.stage = "confirm"
        self.last_activity = _time.time()
        self._candidate_desc: str = ""
        self._retries: int = 0
        self._pending_desc: str = ""
        self._pending_tags: list[str] = field(default_factory=list)
        self._pending_data: bytes = b""
        self._pending_mime: str = ""
        self._pending_ext: str = ""
        self._data_read: bool = False

    @property
    def current(self) -> "_Optional[_PendingImage]":
        if self.current_index < len(self.pending):
            return self.pending[self.current_index]
        return None

    def advance(self) -> None:
        self.current_index += 1
        if self.current_index >= self.total:
            self.stage = "done"

    @property
    def is_done(self) -> bool:
        return self.stage == "done" or self.current_index >= self.total


_pending_image_sessions: dict[int, _ImageDMSession] = {}
_image_mgr: "_Optional[imageManager.ImageManager]" = None


def _init_image_mgr() -> imageManager.ImageManager:
    global _image_mgr
    if _image_mgr is None:
        _image_mgr = imageManager.ImageManager(config.INDIO_IMAGES_DIR)
    return _image_mgr


def has_pending_image_session(author_id: int) -> bool:
    return author_id in _pending_image_sessions


def _extract_tags(text: str) -> list[str]:
    """Simple tag extraction from a text string (filename or description).
    Splits on whitespace, underscores, hyphens, commas; drops short/common words."""
    import re as _re

    parts = _re.split(r"[\s_,;.\-!¡¿?()\[\]]+", text.lower())
    stop = {
        "de",
        "la",
        "el",
        "en",
        "un",
        "una",
        "con",
        "del",
        "y",
        "e",
        "a",
        "que",
        "es",
        "por",
        "para",
        "lo",
        "las",
        "los",
        "se",
        "su",
        "al",
        "como",
        "más",
        "pero",
        "este",
        "esta",
        "jpg",
        "png",
        "jpeg",
        "gif",
        "webp",
        "img",
        "image",
        "photo",
        "foto",
        "imagen",
    }
    seen = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) >= 3 and p not in stop and not p.isdigit() and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:8]


_GENERIC_IMAGE_NAMES = {
    "image",
    "img",
    "photo",
    "foto",
    "imagen",
    "picture",
    "capture",
    "captured",
    "screenshot",
    "screen",
    "snap",
    "snapshot",
    "shot",
    "selfie",
    "pict",
    "pic",
    "imgs",
    "photos",
    "fotos",
    "images",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
}


def _is_generic_filename(filename: str) -> bool:
    name_no_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
    tags = _extract_tags(name_no_ext)
    return not tags or all(t in _GENERIC_IMAGE_NAMES for t in tags)


def _cleanup_stale_sessions() -> None:
    """Cancel sessions that have been idle for > 5 minutes."""
    now = _time.time()
    stale = [
        uid
        for uid, s in _pending_image_sessions.items()
        if now - s.last_activity > _IMAGE_SESSION_TIMEOUT
    ]
    for uid in stale:
        s = _pending_image_sessions.pop(uid, None)
        if s is not None:
            logger.info("image DM session %d stale — cleaned up", uid)


async def handle_indio_image_dm(
    message: "discord.Message",
    attachments: list["discord.Attachment"],
) -> None:
    """Entry point for image-related DM messages.

    Called from ``bot.py.on_message`` when:
    - The DM has image attachments (new session or aux in existing).
    - The DM has text and the user has a pending session (response).
    """
    uid = message.author.id

    # Check for stale session for THIS user
    stale = _pending_image_sessions.get(uid)
    if (
        stale is not None
        and (_time.time() - stale.last_activity) > _IMAGE_SESSION_TIMEOUT
    ):
        _pending_image_sessions.pop(uid, None)
        await message.channel.send(
            "⏰ Pasaron más de 5 minutos... te fuiste afk. "
            "Más tarde seguimos, me vas a tener que volver a "
            "pasar las fotos."
        )
        return  # Don't process this message; user needs to send images again

    # Also clean up OTHER users' stale sessions in background
    _cleanup_stale_sessions()

    # --- New session (message has images) ---
    if attachments and uid not in _pending_image_sessions:
        sess = _ImageDMSession(uid, attachments)
        _pending_image_sessions[uid] = sess
        n = len(attachments)
        analytics.capture(
            "indio_image_session_started",
            distinct_id=str(uid),
            properties={
                "image_count": n,
                "filenames": [a.filename for a in attachments],
            },
        )
        if n == 1:
            await message.channel.send("📸 Recibí una imagen. Vamos a revisarla.")
        else:
            await message.channel.send(
                f"📸 Recibí {n} imágenes. Las revisamos una por una."
            )
        await _ask_about_current(message.channel, sess)
        return

    # --- Existing session — user's response ---
    sess = _pending_image_sessions.get(uid)
    if sess is None:
        return  # not our message, let other handlers deal with it

    sess.last_activity = _time.time()
    text = (message.content or "").strip().lower()
    await _handle_session_text(message.channel, message.author, sess, text)


async def _ask_about_current(
    channel: "discord.abc.Messageable",
    sess: _ImageDMSession,
) -> None:
    """Ask the user about the filename of the current image."""
    img = sess.current
    if img is None:
        await _finish_session(channel, sess)
        return
    name = img.original_filename
    await channel.send(
        f"🖼️ Imagen **{sess.current_index + 1}/{sess.total}**:\n"
        f"Nombre del archivo: **{name}**\n\n"
        f"¿Qué hacemos?\n"
        f"• **1** — usar el nombre del archivo como descripción\n"
        f"• **cancelar** — salteamos esta imagen\n"
        f"• *cualquier otro texto* — se usa como tu descripción"
    )
    sess.stage = "confirm"


async def _handle_session_text(
    channel: "discord.abc.Messageable",
    author: "discord.User",
    sess: _ImageDMSession,
    text: str,
) -> None:
    """Route the user's text response based on the current session stage."""
    _YES = {"sí", "si", "sis", "dale", "ok", "yes", "yep", "sip"}
    _first = text.split(",")[0].split()[0].strip() if text else ""

    if sess.stage == "waiting_desc":
        if text in ("cancelar", "cancel", "saltear", "skip"):
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "skip", "stage": sess.stage},
            )
            await channel.send("OK, la salteamos.")
            sess.advance()
            await _ask_about_current(channel, sess)
        else:
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={
                    "action": "retry_description",
                    "stage": sess.stage,
                    "retry": sess._retries + 1,
                },
            )
            await _validate_candidate(channel, author, sess, text)
        return

    if sess.stage == "confirm":
        if text in ("cancelar", "cancel", "saltear", "skip", "next", "siguiente"):
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "skip", "stage": sess.stage},
            )
            await channel.send("OK, la salteamos.")
            sess.advance()
            await _ask_about_current(channel, sess)
        elif _first == "1":
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "use_filename", "stage": sess.stage},
            )
            img = sess.current
            if img is None:
                return
            if _is_generic_filename(img.original_filename):
                analytics.capture(
                    "indio_image_action",
                    distinct_id=str(author.id),
                    properties={
                        "action": "generic_filename_rejected",
                        "filename": img.original_filename,
                    },
                )
                await channel.send(
                    f"⚠️ El nombre del archivo **{img.original_filename}** es muy "
                    "genérico. Describila vos."
                )
                sess.stage = "waiting_desc"
                sess.last_activity = _time.time()
            else:
                name_no_ext = (
                    img.original_filename.rsplit(".", 1)[0]
                    if "." in img.original_filename
                    else img.original_filename
                )
                await _validate_candidate(channel, author, sess, name_no_ext)
        else:
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "user_description", "stage": sess.stage},
            )
            await _validate_candidate(channel, author, sess, text)
    elif sess.stage == "confirm_save":
        if _first in _YES:
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "confirm_save", "stage": sess.stage},
            )
            await _save_user_and_gemini_desc(channel, author, sess)
        else:
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={"action": "retry_from_confirm", "stage": sess.stage},
            )
            sess._retries = 0
            await channel.send("Dale, decime la descripción correcta.")
            sess.stage = "waiting_desc"
            sess.last_activity = _time.time()
    else:
        logger.warning(
            "image DM session %d unexpected stage %s", sess.author_id, sess.stage
        )


async def _validate_candidate(
    channel: "discord.abc.Messageable",
    author: "discord.User",
    sess: _ImageDMSession,
    candidate_text: str = "",
) -> None:
    """Download the image once and ask Gemini to describe + validate the candidate.

    If ``candidate_text`` is empty (generic filename), Gemini just describes.
    Otherwise Gemini also checks if the candidate matches the image content.
    On match → show description → confirm_save stage.
    On mismatch → increment retries → waiting_desc stage (or skip if >5 retries).
    """
    img = sess.current
    if img is None:
        return

    # Download once, cache in session
    if not sess._data_read:
        try:
            data = await img.attachment.read()
        except Exception as exc:
            logger.exception("image download failed")
            await channel.send(f"❌ No pude descargar la imagen: {exc}")
            sess.advance()
            await _ask_about_current(channel, sess)
            return
        sess._pending_data = data
        sess._pending_mime = img.attachment.content_type or "image/png"
        sess._pending_ext = (
            img.attachment.filename.rsplit(".", 1)[-1]
            if "." in img.attachment.filename
            else "png"
        )
        sess._data_read = True

    b64_data = _b64.b64encode(sess._pending_data).decode()
    image_part = {"inlineData": {"mimeType": sess._pending_mime, "data": b64_data}}

    await channel.send("🔍 Dejame ver la imagen...")

    if candidate_text:
        prompt = (
            f"Describí CORRECTAMENTE el contenido de esta imagen "
            f"(no el formato, sino el contenido real) para un catálogo. "
            f"Sé conciso (2-3 oraciones máximo). "
            f"También dame 3-5 tags relevantes separados por comas.\n\n"
            f'El usuario dice que esta imagen es: "{candidate_text}"\n\n'
            f"Decime si esa descripción del usuario coincide con el "
            f"contenido real de la imagen.\n\n"
            f"Respondé EXACTAMENTE con este formato:\n"
            f"DESCRIPCIÓN: <tu descripción>\n"
            f"TAGS: tag1, tag2, tag3\n"
            f"COINCIDE: sí\n"
            f"o\n"
            f"COINCIDE: no"
        )
    else:
        prompt = (
            "Describí CORRECTAMENTE el contenido de esta imagen "
            "(no el formato, sino el contenido real) para un catálogo. "
            "Sé conciso (2-3 oraciones máximo). "
            "También dame 3-5 tags relevantes separados por comas.\n\n"
            "Respondé EXACTAMENTE con este formato:\n"
            "DESCRIPCIÓN: <tu descripción>\n"
            "TAGS: tag1, tag2, tag3"
        )

    try:
        reply = await geminiClient.generate(
            user_message=prompt,
            system_instruction=(
                "Sos un asistente que describe imágenes para un catálogo. "
                "Sé conciso, objetivo, no inventes nada que no esté en la imagen."
            ),
            image_parts=[image_part],
            max_output_tokens=512,
        )
    except Exception as exc:
        logger.exception("gemini validate failed")
        await channel.send(f"❌ No pude procesar la imagen: {exc}")
        sess.advance()
        await _ask_about_current(channel, sess)
        return

    gemini_text = reply.text or ""
    desc = ""
    tags: list[str] = []
    coincides: Optional[bool] = None

    for line in gemini_text.split("\n"):
        line = line.strip()
        if line.upper().startswith("DESCRIPCIÓN:"):
            desc = line.split(":", 1)[1].strip()
        elif line.upper().startswith("TAGS:"):
            raw = line.split(":", 1)[1].strip()
            tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
        elif line.upper().startswith("COINCIDE:"):
            val = line.split(":", 1)[1].strip().lower()
            coincides = val in ("sí", "si", "yes")

    if not desc:
        desc = gemini_text[:200]
    if not tags:
        tags = _extract_tags(desc)
    if coincides is None and candidate_text:
        coincides = False

    sess._candidate_desc = candidate_text
    sess._pending_desc = desc
    sess._pending_tags = tags

    analytics.capture(
        "indio_image_gemini_validated",
        distinct_id=str(author.id),
        properties={
            "candidate": candidate_text[:100],
            "gemini_desc": desc[:100],
            "tags": tags[:5],
            "coincides": coincides,
            "retries": sess._retries,
            "filename": img.original_filename,
        },
    )

    if candidate_text and coincides is False:
        sess._retries += 1
        if sess._retries >= 5:
            analytics.capture(
                "indio_image_action",
                distinct_id=str(author.id),
                properties={
                    "action": "max_retries_exceeded",
                    "retries": sess._retries,
                },
            )
            await channel.send(
                "❌ Ya intentamos varias veces y no pudimos dar con una "
                "descripción que coincida. La salteamos."
            )
            sess.advance()
            await _ask_about_current(channel, sess)
        else:
            await channel.send(
                "❌ No coincide. Describila de vuelta o decí **cancelar** "
                "para saltearla."
            )
            sess.stage = "waiting_desc"
            sess.last_activity = _time.time()
    else:
        await channel.send(
            f"📝 Descripción: *{desc}*\n🏷️ Tags: {', '.join(tags[:5])}\n\n"
            f"¿La guardo así?"
        )
        sess.stage = "confirm_save"


async def _save_user_and_gemini_desc(
    channel: "discord.abc.Messageable",
    author: "discord.User",
    sess: _ImageDMSession,
) -> None:
    """Save the image with both the user's description and Gemini's description."""
    mgr = _init_image_mgr()
    user_desc = sess._candidate_desc
    gemini_desc = sess._pending_desc
    final_desc = user_desc or gemini_desc

    try:
        img_id = mgr.add_image(
            sess._pending_data,
            sess._pending_ext,
            final_desc,
            sess._pending_tags,
            author.id,
            sess.current.original_filename if sess.current else "",
            gemini_description=gemini_desc,
        )
        sess.processed.append(
            {
                "id": img_id,
                "description": final_desc,
                "gemini_description": gemini_desc,
                "tags": sess._pending_tags,
            }
        )
        analytics.capture(
            "indio_image_saved",
            distinct_id=str(author.id),
            properties={
                "method": "validated",
                "description": final_desc[:100],
                "gemini_description": gemini_desc[:100],
                "tags": sess._pending_tags,
            },
        )
        await channel.send(f"✅ Guardada: *{final_desc}*")
    except Exception as exc:
        logger.exception("image save failed")
        analytics.capture_exception(exc, properties={"method": "validated"})
        await channel.send(f"❌ No pude guardarla: {exc}")
    sess.advance()
    await _ask_about_current(channel, sess)


async def _finish_session(
    channel: "discord.abc.Messageable",
    sess: _ImageDMSession,
) -> None:
    """Wrap up the session and show the summary."""
    uid = sess.author_id
    _pending_image_sessions.pop(uid, None)
    n = len(sess.processed)
    analytics.capture(
        "indio_image_session_finished",
        distinct_id=str(uid),
        properties={
            "saved_count": n,
            "descriptions": [p["description"] for p in sess.processed],
        },
    )
    if n == 0:
        await channel.send("No se guardó ninguna imagen. ¡Ché boludo!")
        return

    lines = [f"✅ **Listo!** Guardé {n} imagen(es):"]
    for i, p in enumerate(sess.processed, 1):
        tags = ", ".join(p["tags"][:5])
        lines.append(f"  {i}. *{p['description']}* [{tags}]")
    lines.append(
        "\nA partir de ahora las voy a tener en cuenta en las conversaciones "
        "y las puedo usar cuando corresponda."
    )
    await channel.send("\n".join(lines))


def _inject_image_catalog(system_instruction: str) -> str:
    """Append the image catalog block to the system instruction if available."""
    mgr = _init_image_mgr()
    block = mgr.get_catalog_block()
    if block:
        return system_instruction + "\n\n" + block
    return system_instruction


def _humanize_age(seconds: float) -> str:
    """Render an age-in-seconds as a Spanish short tag for the prompt.

    Used to prefix old history turns with ``(hace X)`` so Gemini knows the
    line is not part of the current exchange. Buckets are coarse on purpose —
    the model only needs to tell "now" from "ago"."""
    if seconds < 60:
        return "hace instantes"
    if seconds < 3600:
        return f"hace {int(seconds // 60)} min"
    if seconds < 86400:
        return f"hace {int(seconds // 3600)} h"
    if seconds < 86400 * 30:
        return f"hace {int(seconds // 86400)} días"
    if seconds < 86400 * 365:
        return f"hace {int(seconds // (86400 * 30))} meses"
    return "hace más de un año"


def _stamp_history_for_prompt(history: list[dict], now: float) -> list[dict]:
    """Return a copy of ``history`` where each turn old enough gets a
    ``(hace X)`` tag prepended to its text, so the model treats those lines
    as past context, not present.

    Recent turns (≤ ``_HISTORY_AGE_TAG_THRESHOLD_SEC``) pass through unchanged
    so the current exchange reads naturally. Turns without a ``ts`` field
    (legacy entries from before this feature) are treated as old.
    """
    out: list[dict] = []
    for turn in history or []:
        ts = turn.get("ts")
        if ts is None:
            age = None
        else:
            try:
                age = max(0.0, now - float(ts))
            except (TypeError, ValueError):
                age = None
        if age is not None and age < _HISTORY_AGE_TAG_THRESHOLD_SEC:
            # Recent — leave it alone.
            out.append({k: v for k, v in turn.items() if k != "ts"})
            continue
        tag = f"({_humanize_age(age)}) " if age is not None else "(hace tiempo) "
        new_parts = []
        for part in turn.get("parts", []):
            if isinstance(part, dict) and "text" in part:
                new_parts.append({"text": tag + str(part["text"])})
            else:
                new_parts.append(part)
        out.append(
            {"role": turn.get("role"), "parts": new_parts or turn.get("parts", [])}
        )
    return out


_indio_long_term: dict[str, dict] = {}
_indio_locks: dict[str, asyncio.Lock] = {}
_persist_lock = asyncio.Lock()
# Per-key flag: a compression task is in-flight, don't spawn another.
_indio_compressing: set[str] = set()
# "Main characters" roster persisted alongside long-term memory. Refreshed
# at most once per ``_ROSTER_REFRESH_INTERVAL_SEC``; see _maybe_refresh_current_members.
_indio_current_members: dict[str, list[str]] = {}
_indio_members_refreshed_at: dict[str, float] = {}

# Music disambiguation via group vote. When the indio is asked for a song and
# the search returns several candidates, we list them and open a short voting
# window managed by ``playCommand.MusicVote`` (shared with the /play button
# picker so there's a single vote per guild regardless of how it was invoked).
# This module only bridges input surfaces (voice + reactions on the indio's
# chat message) into that vote.
# How many candidates to offer. Kept in sync with playCommand's /play picker.
_MUSIC_CHOICE_COUNT = 5


def _load_indio_state() -> None:
    """Load history+last_seen+long_term from disk on startup. Silently no-ops
    if the file is missing or unreadable — memory just starts empty.

    Loaded turns are run through ``_sanitize_for_history`` so legacy entries
    (with bracketed "[Name]:" speaker prefixes and Discord emoji codes) get
    migrated to the clean format on the fly, no manual JSON edits needed."""
    path = config.INDIO_MEMORY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logger.exception("indio memory load failed at %s", path)
        analytics.capture_exception(
            e, properties={"action": "indio_memory_load_failed"}
        )
        return
    entries = data.get("entries", {})
    now = time.time()
    loaded = 0
    for key, val in entries.items():
        last_seen = float(val.get("last_seen", 0))
        history = val.get("history", [])
        long_term = val.get("long_term") or {}
        current_members = val.get("current_members") or []
        current_members_at = float(val.get("current_members_refreshed_at", 0) or 0)
        keep_short_term = now - last_seen <= _HISTORY_TTL_SEC
        if keep_short_term:
            if isinstance(history, list) and history:
                _indio_history[key] = [_sanitize_turn_on_load(t) for t in history]
                _indio_last_seen[key] = last_seen
                loaded += 1
        if isinstance(long_term, dict) and long_term:
            lt = _clean_music_from_long_term(long_term)
            lt = _clean_gender_from_long_term(lt)
            _indio_long_term[key] = lt
        if isinstance(current_members, list) and current_members:
            _indio_current_members[key] = [str(n) for n in current_members if n]
            _indio_members_refreshed_at[key] = current_members_at
    if loaded or _indio_long_term or _indio_current_members:
        logger.info(
            "indio memory: loaded %d entries (long_term=%d, roster=%d) from %s",
            loaded,
            len(_indio_long_term),
            len(_indio_current_members),
            path,
        )


def _sanitize_turn_on_load(turn: dict) -> dict:
    """Pass each ``part.text`` of a history turn through ``_sanitize_for_history``.
    Used by ``_load_indio_state`` to migrate legacy JSON on the fly. Non-dict
    parts or parts without text are passed through unchanged."""
    if not isinstance(turn, dict):
        return turn
    new_parts: list = []
    for part in turn.get("parts", []):
        if isinstance(part, dict) and "text" in part:
            new_parts.append({**part, "text": _sanitize_for_history(str(part["text"]))})
        else:
            new_parts.append(part)
    return {**turn, "parts": new_parts}


async def _persist_indio_state() -> None:
    """Atomic write of the full indio state to disk. Held under _persist_lock
    so concurrent turns don't clobber each other's writes."""
    path = config.INDIO_MEMORY_PATH
    async with _persist_lock:
        keys = set(_indio_history) | set(_indio_long_term) | set(_indio_current_members)
        payload = {
            "entries": {
                k: {
                    "history": _indio_history.get(k, []),
                    "last_seen": _indio_last_seen.get(k, 0.0),
                    "long_term": _indio_long_term.get(k, {}),
                    "current_members": _indio_current_members.get(k, []),
                    "current_members_refreshed_at": _indio_members_refreshed_at.get(
                        k, 0.0
                    ),
                }
                for k in keys
            }
        }
        try:
            await asyncio.to_thread(_write_json_atomic, path, payload)
        except Exception as e:
            logger.exception("indio memory persist failed at %s", path)
            analytics.capture_exception(
                e, properties={"action": "indio_memory_persist_failed"}
            )


def _write_json_atomic(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".indio_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _indio_memory_key(ctx: discord.ApplicationContext) -> str:
    """Build the memory bucket key for the Indio persona.

    Args:
        ctx: Discord application context.

    Returns:
        A string key scoped to the guild (or DM if no guild).
    """
    guild = getattr(ctx, "guild", None)
    if guild is not None and getattr(guild, "id", None) is not None:
        return f"guild-{guild.id}"
    return f"dm-{getattr(ctx.author, 'id', 'unknown')}"


def _split_for_discord(text: str) -> list[str]:
    """Split text into Discord-sized chunks.

    Args:
        text: Full response text.

    Returns:
        List of chunks capped at _MAX_CHUNKS.

    Side Effects:
        None. The last chunk may be truncated with an ellipsis.
    """
    if len(text) <= _DISCORD_CHUNK_LIMIT:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > _DISCORD_CHUNK_LIMIT:
            if buf:
                chunks.append(buf)
                buf = ""
            while len(line) > _DISCORD_CHUNK_LIMIT:
                chunks.append(line[:_DISCORD_CHUNK_LIMIT])
                line = line[_DISCORD_CHUNK_LIMIT:]
        buf += line
        if len(chunks) >= _MAX_CHUNKS:
            break
    if buf and len(chunks) < _MAX_CHUNKS:
        chunks.append(buf)

    if len(chunks) >= _MAX_CHUNKS:
        marker = "\n…(truncado)"
        last = chunks[_MAX_CHUNKS - 1]
        if len(last) + len(marker) > _DISCORD_CHUNK_LIMIT:
            last = last[: _DISCORD_CHUNK_LIMIT - len(marker)]
        chunks = chunks[: _MAX_CHUNKS - 1] + [last + marker]

    return chunks


def _evict_stale_indio() -> None:
    """Drop stale short-term Indio history while keeping long-term memory.

    Short-term verbatim history is evicted once it passes the TTL, but the
    per-guild ``long_term`` memory and ``last_seen`` survive so the indio keeps
    remembering the group like a friend. Keys currently being compressed are
    skipped, and a lock is only released when nothing relevant remains.

    Returns:
        None.

    Side Effects:
        Mutates in-memory history/lock dictionaries.
    """
    now = time.time()
    for key in list(_indio_last_seen.keys()):
        if now - _indio_last_seen[key] <= _HISTORY_TTL_SEC:
            continue
        if key in _indio_compressing:
            continue
        _indio_history.pop(key, None)
        # _indio_long_term y _indio_last_seen sobreviven.
        # Lock se libera solo si no quedó nada relevante.
        if key not in _indio_long_term:
            _indio_locks.pop(key, None)
    # Music votes live in playCommand.active_votes now; they own their own
    # close timer and self-cleanup. Nothing for the indio side to evict here.


def _make_retry_notifier(ctx: discord.ApplicationContext):
    """Build an on_retry callback that edits the deferred message once on retry.

    Returns ``(notifier, state)`` where ``notifier`` is an async callback to
    pass to ``geminiClient.generate(on_retry=...)`` and ``state`` is a dict
    with ``had_retry: bool``. The caller inspects ``state["had_retry"]`` after
    awaiting ``generate`` to decide whether the first chunk of the final reply
    should overwrite the notice (via ``edit_first=True``) or fall through to
    the normal followup flow.

    Only the *first* retry edits — subsequent rotations within the same call
    leave the notice unchanged. Avoids parpadeo and keeps the test surface
    small (one edit_original_response call asserted).
    """
    state = {"had_retry": False}

    async def _notify(attempt, total, key_suffix):
        if state["had_retry"]:
            return
        state["had_retry"] = True
        from bot import safeEdit

        await safeEdit(ctx, AVISO_ROTACION_GEMINI)

    return _notify, state


async def _send_reply(
    ctx: discord.ApplicationContext,
    text: str,
    *,
    edit_first: bool = False,
    ephemeral: bool = False,
) -> int:
    """Send a possibly multi-part reply to Discord.

    Args:
        ctx: Discord application context.
        text: Full response text.
        edit_first: When True, the first chunk overwrites the deferred message
            via ``edit_original_response`` (used to replace a transient
            ``AVISO_ROTACION_GEMINI`` notice with the real reply). Subsequent
            chunks always go through ``followup.send``. Falls back to
            ``followup.send`` if the edit fails.
        ephemeral: When True, the reply is visible only to the invoker. A
            deferred-public response cannot be edited into an ephemeral one, so
            ``edit_first`` is ignored in that case and every chunk goes out as
            an ephemeral followup.

    Returns:
        Number of chunks sent.

    Side Effects:
        Sends follow-up messages via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    chunks = _split_for_discord(text)
    for i, c in enumerate(chunks):
        if edit_first and i == 0 and not ephemeral:
            try:
                await ctx.interaction.edit_original_response(content=c)
                continue
            except Exception:
                logger.debug(
                    "edit_original_response failed, falling back to followup",
                    exc_info=True,
                )
        await ctx.followup.send(c, ephemeral=ephemeral)
    return len(chunks)


def _format_user_header(ctx: discord.ApplicationContext, pregunta: str) -> str:
    """Format the user header and quoted question for responses.

    Args:
        ctx: Discord application context.
        pregunta: Original user question.

    Returns:
        A formatted header string for the reply.
    """
    name = getattr(ctx.author, "display_name", None) or getattr(
        ctx.author, "name", "alguien"
    )
    lines = (pregunta or "").splitlines() or [""]
    quoted = "\n".join(f"> {ln}" for ln in lines)
    return f"**{name}** preguntó:\n{quoted}\n\n"


_GUILD_EMOJI_LIMIT = 40


def _format_player_state(bot, guild_id) -> str:
    """Render the current music player state as a prompt block.

    The indio needs to know whether something is paused so ambiguous requests
    like "play" / "continuá" / "metele play" route to ``resume_music`` instead
    of ``play_music`` with a junk query. Returns "" when there is no active
    player, no voice client, or the player is fully idle.
    """
    if not guild_id:
        return ""
    try:
        import playCommand

        player = playCommand.guildPlayers.get(int(guild_id))
    except Exception:
        return ""
    if player is None:
        return ""
    title = ""
    cur = getattr(player, "currentSong", None)
    if isinstance(cur, dict):
        title = str(cur.get("title") or "").strip()
    _play_phrases = " / ".join(f"'{p}'" for p in _kw.RESUME_CONTEXT_PLAY_PHRASES)
    # Interrupted state lives without a vc — the bot got kicked or dropped,
    # but we kept the song and queue in memory. The indio should steer
    # ambiguous play requests to resume_music here too.
    if getattr(player, "interrupted", False) and cur is not None:
        head = (
            f'música INTERRUMPIDA por desconexión — "{title}"'
            if title
            else "música interrumpida por desconexión"
        )
        return (
            f"[Estado del reproductor]: {head}. Si piden {_play_phrases} "
            f"SIN nombrar artista o canción, usá "
            f"resume_music (NO play_music) — el bot va a reconectarse y "
            f"retomar desde donde quedó."
        )
    vc = getattr(player, "vc", None)
    if vc is None:
        return ""
    try:
        if vc.is_paused():
            head = f'música PAUSADA — "{title}"' if title else "música pausada"
            return (
                f"[Estado del reproductor]: {head}. Si piden {_play_phrases} "
                f"SIN nombrar artista o canción, usá resume_music "
                f"(NO play_music)."
            )
    except Exception:
        pass
    try:
        if vc.is_playing():
            head = f'sonando — "{title}"' if title else "hay música sonando"
            return f"[Estado del reproductor]: {head}."
    except Exception:
        pass
    return ""


def _find_emoji_code(guild, name: str) -> Optional[str]:
    """Return the Discord render code (``<:name:id>`` or ``<a:name:id>``)
    for the guild's custom emoji ``name``, or None if it isn't available."""
    for e in getattr(guild, "emojis", None) or []:
        if getattr(e, "name", "") == name and getattr(e, "available", True):
            eid = getattr(e, "id", None)
            if eid is None:
                continue
            prefix = "a" if getattr(e, "animated", False) else ""
            return f"<{prefix}:{e.name}:{eid}>"
    return None


def _format_guild_emojis(guild) -> str:
    """Render the guild's custom emojis as a prompt block so the indio can
    drop them into replies. Each entry shows the exact "<:name:id>" code that
    Discord needs to render the image — Gemini won't guess IDs correctly, so
    we hand them over verbatim. Returns "" if there are no usable emojis."""
    emojis = getattr(guild, "emojis", None) or []
    lines: list[str] = []
    for e in emojis:
        if not getattr(e, "available", True):
            continue
        if getattr(e, "id", None) is None or not getattr(e, "name", ""):
            continue
        prefix = "a" if getattr(e, "animated", False) else ""
        lines.append(f"- :{e.name}: → <{prefix}:{e.name}:{e.id}>")
        if len(lines) >= _GUILD_EMOJI_LIMIT:
            break
    if not lines:
        return ""
    return "Emojis custom del server (pegá el código completo tal cual):\n" + "\n".join(
        lines
    )


_ROSTER_REFRESH_INTERVAL_SEC = 24 * 3600  # refresh from users.py once per day
_roster_lock = asyncio.Lock()


def _names_from_users_py() -> list[str]:
    """Read the friend roster from the static users.py mapping. We use this as
    the source of truth because discord.py-self can't reliably enumerate every
    guild member from a user account (the cache is partial and fetch_members
    only returns members the gateway has surfaced)."""
    return [
        info["name"]
        for info in _USERS.values()
        if isinstance(info, dict) and info.get("name")
    ]


async def _maybe_refresh_current_members(mem_key: str, guild_id: Optional[int]) -> None:
    """Refresh the cached friend roster for a guild at most once per
    ``_ROSTER_REFRESH_INTERVAL_SEC``. The names come from users.py and live
    alongside the indio's long-term memory, persisted to disk so they
    survive restarts. The indio doesn't "read" the list on every call — he
    knows who they are because it's already in his memory."""
    if guild_id is None:
        return
    expected = _names_from_users_py()
    if not expected:
        return
    now = time.time()
    last = _indio_members_refreshed_at.get(mem_key, 0.0)
    current = _indio_current_members.get(mem_key)
    # Refresh if (a) the TTL elapsed, (b) we never refreshed, or
    # (c) users.py was edited and the stored list no longer matches.
    if now - last < _ROSTER_REFRESH_INTERVAL_SEC and current == expected:
        return
    async with _roster_lock:
        last = _indio_members_refreshed_at.get(mem_key, 0.0)
        current = _indio_current_members.get(mem_key)
        if now - last < _ROSTER_REFRESH_INTERVAL_SEC and current == expected:
            return
        previous = current
        _indio_current_members[mem_key] = expected
        _indio_members_refreshed_at[mem_key] = time.time()
    if previous != expected:
        await _persist_indio_state()
        logger.info(
            "indio: refreshed current_members for %s (%d names from users.py)",
            mem_key,
            len(expected),
        )


_INDIO_USER_FIELDS: tuple[str, ...] = (
    "traits",
    "preguntas_tipicas",
    "anecdotas",
    "descripcion",
    "fotos",
)


def _static_user_traits() -> dict[str, dict[str, list[str]]]:
    """Pull manual traits/preguntas/anecdotas from users.py. Each entry can
    optionally carry ``traits``, ``preguntas_tipicas`` and ``anecdotas``
    lists; these are merged into the long-term render every time the indio
    answers and are never overwritten by Gemini's compression cycle.

    Cualquier trait que contenga palabras de género (sexo, hombre, mujer, etc.)
    se filtra automáticamente."""
    out: dict[str, dict[str, list[str]]] = {}
    sources = list(_USERS.values()) + list(_NON_DISCORD_MEMBERS)
    for info in sources:
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if not name:
            continue
        out[name] = {
            field: [
                str(t)
                for t in (info.get(field) or [])
                if t and not _has_gender_words(str(t))
            ]
            for field in _INDIO_USER_FIELDS
        }
    return out


def _block_lists_by_name() -> dict[str, list[str]]:
    """Mapa apodo -> lista de substrings (lowercase) que hay que filtrar de
    la memoria dinámica. Usado para scrubear facts viejos/incorrectos sin
    tener que limpiar a mano el indio_memory.json del server."""
    out: dict[str, list[str]] = {}
    sources = list(_USERS.values()) + list(_NON_DISCORD_MEMBERS)
    for info in sources:
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        blocks = info.get("block_dynamic_substrings") or []
        if not name or not blocks:
            continue
        out[str(name)] = [str(b).lower() for b in blocks if b]
    return out


def _merge_user_dossiers(lt_users: dict) -> dict[str, dict[str, list[str]]]:
    """Combine the static per-user traits from users.py with whatever Gemini
    has distilled in long-term memory. Static entries provide a baseline; the
    distilled additions are appended without duplicates. Items in dynamic
    memory matching a user's ``block_dynamic_substrings`` or gender keywords
    are filtered out."""
    merged = _static_user_traits()
    blocks_by_name = _block_lists_by_name()
    if isinstance(lt_users, dict):
        for name, data in lt_users.items():
            if not isinstance(data, dict):
                continue
            name_str = str(name)
            blocks = blocks_by_name.get(name_str, [])
            bucket = merged.setdefault(name_str, {f: [] for f in _INDIO_USER_FIELDS})
            for key in _INDIO_USER_FIELDS:
                existing = bucket.setdefault(key, [])
                for item in data.get(key) or []:
                    s = str(item)
                    if not s or s in existing:
                        continue
                    if _has_gender_words(s):
                        continue
                    if blocks and any(b in s.lower() for b in blocks):
                        continue
                    existing.append(s)
    return merged


def _format_long_term(lt: dict, current_members: Optional[list[str]] = None) -> str:
    """Render long-term memory as a compact Spanish block to inject into the
    indio's system instruction. Natural-language form (no JSON) so the model
    integrates it like context, not data.

    ``current_members`` is the friend roster (from users.py), rendered as a
    short header so the indio always knows who his amigos are from his own
    memory. Per-user dossiers merge static traits (users.py) with Gemini's
    distilled long-term data."""
    sections: list[str] = []
    if current_members:
        friends = [n for n in current_members if n != "El Indio"]
        if friends:
            sections.append("Mis amigos son: " + ", ".join(friends) + ".")
    lt = lt or {}
    user_dossiers = _merge_user_dossiers(lt.get("users") or {})
    if user_dossiers:
        user_lines = ["Lo que sabés de cada uno:"]
        for name, data in user_dossiers.items():
            traits = data.get("traits") or []
            qs = data.get("preguntas_tipicas") or []
            anec = data.get("anecdotas") or []
            desc = data.get("descripcion") or []
            fotos = data.get("fotos") or []
            chunk = [f"- {name}:"]
            if traits:
                chunk.append(f"   rasgos: {'; '.join(traits)}")
            if desc:
                chunk.append(f"   descripción física: {'; '.join(desc)}")
            if fotos:
                chunk.append(f"   fotos/aspecto: {'; '.join(fotos)}")
            if qs:
                chunk.append(f"   suele preguntar sobre: {'; '.join(qs)}")
            if anec:
                chunk.append(f"   anécdotas: {'; '.join(anec)}")
            if len(chunk) > 1:
                user_lines.extend(chunk)
        if len(user_lines) > 1:
            sections.append("\n".join(user_lines))
    # Merge static group lore (users.py:GROUP_LORE) with whatever Gemini has
    # distilled in long_term. Static items go first; dynamic ones are appended
    # without duplicates.
    static_events = [str(x) for x in (_GROUP_LORE.get("eventos_del_grupo") or []) if x]
    lt_events = [str(x) for x in (lt.get("eventos_del_grupo") or []) if x]
    events = list(static_events)
    for e in lt_events:
        if e not in events:
            events.append(e)
    if events:
        sections.append(
            "Cosas que pasaron en el grupo:\n" + "\n".join(f"- {e}" for e in events)
        )

    static_jokes = [str(x) for x in (_GROUP_LORE.get("chistes_internos") or []) if x]
    lt_jokes = [str(x) for x in (lt.get("chistes_internos") or []) if x]
    jokes = list(static_jokes)
    for j in lt_jokes:
        if j not in jokes:
            jokes.append(j)
    if jokes:
        sections.append(
            "Chistes internos del grupo:\n" + "\n".join(f"- {j}" for j in jokes)
        )
    return "\n\n".join(sections)


_COMPRESS_SYSTEM = """\
Sos un asistente que mantiene una memoria a largo plazo sobre un grupo de \
amigos en un server de Discord. Recibís (a) la memoria actual en JSON y (b) \
una conversación nueva del grupo. Tu trabajo es devolver SOLO un JSON \
actualizado, sin texto adicional ni bloques markdown, con esta estructura \
exacta:

{
  "users": {
    "<nombre>": {
      "traits": ["rasgos de personalidad o intereses"],
      "preguntas_tipicas": ["qué tipo de cosas suele preguntar/decir"],
      "anecdotas": ["momentos del grupo que lo involucran"]
    }
  },
  "eventos_del_grupo": ["cosas memorables que pasaron en el chat"],
  "chistes_internos": ["chistes recurrentes o referencias del grupo"]
}

Reglas estrictas:
- NO inventes datos. Solo guardás lo que aparece textualmente o lo que se \
  deduce directamente de la conversación.
- Si un usuario repite la misma información varias veces en la conversación \
  (ej: "soy de X", "te digo que soy de X", "no te olvides que soy de X"), \
  guardala UNA sola vez. No dupliques ni expandís un rasgo porque fue \
  repetido. No registres el hecho de que lo repitió como rasgo ni anécdota.
- Si un dato ya está en la memoria actual, no lo volvás a agregar aunque \
  aparezca en la conversación nueva, ni en palabras distintas.
- Mantenés los datos previos a menos que la conversación los contradiga.
- Cada string ≤120 caracteres.
- Máx %d rasgos, %d preguntas_tipicas y %d anecdotas por usuario.
- Máx %d eventos_del_grupo y %d chistes_internos en total.
- Conservás los nombres tal cual aparecen entre corchetes ("[nombre]: ...").
- No incluyas al "indio" como usuario (es el bot, no un miembro del grupo).
- NUNCA incluyas información de sexo, género ("hombre", "mujer"), ni
  menciones que traten a un usuario como hombre o mujer.
- Español rioplatense, casual, conciso.
- Devolvé SOLO el JSON. Sin ```json ni explicación.
""" % (
    _LT_TRAITS_PER_USER,
    _LT_QUESTIONS_PER_USER,
    _LT_ANECDOTES_PER_USER,
    _LT_GROUP_EVENTS,
    _LT_JOKES,
)


def _extract_json(text: str) -> Optional[dict]:
    """Defensive JSON parser: handles raw JSON, ```json``` fenced blocks, and
    leading/trailing junk. Returns None on any failure."""
    if not text:
        return None
    s = text.strip()
    # Strip markdown fences if present.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # Find the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


# Palabras clave musicales que no deben contaminar la memoria a largo plazo
# del Indio. Cuando aparecen en rasgos/anécdotas/eventos se filtran para que
# el modelo no aprenda el patrón "voz → play_music" desde su propio historial.
# Palabras de género/sexo que se filtran de la memoria a largo plazo y de
# traits estáticos. Ningún usuario debe tener información de sexo/género
# en su perfil ni en la memoria comprimida.
_GENDER_BLOCK_WORDS = frozenset(
    {
        "sexo",
        "hombre",
        "mujer",
        "género",
        "genero",
        "varón",
        "varon",
        "masculino",
        "femenino",
    }
)

_MUSIC_BLOCK_WORDS = frozenset(
    {
        "música",
        "musica",
        "canción",
        "cancion",
        "canciones",
        "play",
        "play_music",
        "play_sound",
        "dj",
        "autodj",
        "tema musical",
        "modo dj",
        "reproducir",
        "reproduciendo",
        "escuchar",
        "sonando",
        "tirar un tema",
        "poner música",
    }
)


def _has_music_block_words(text: str) -> bool:
    """True si el texto contiene alguna keyword musical a filtrar."""
    t = _strip_accents_lower(text)
    return any(w in t for w in _MUSIC_BLOCK_WORDS)


def _has_gender_words(text: str) -> bool:
    """True si el texto menciona sexo/género del usuario. Se filtra para
    que el indio no tenga datos de género en ningún lado."""
    t = _strip_accents_lower(text)
    return any(w in t for w in _GENDER_BLOCK_WORDS)


def _clean_music_from_long_term(lt: dict) -> dict:
    """Filtra entradas relacionadas con música de la memoria a largo plazo.
    Remueve rasgos/anécdotas/eventos/chistes que contengan keywords musicales,
    para que el Indio no acumule contexto que lo sesgue a llamar play_music."""
    if not lt:
        return lt
    out: dict = {"users": {}, "eventos_del_grupo": [], "chistes_internos": []}
    users = lt.get("users") or {}
    for name, data in users.items():
        if not isinstance(data, dict):
            continue
        cleaned: dict[str, list[str]] = {}
        for field in ("traits", "preguntas_tipicas", "anecdotas"):
            items = [
                s for s in (data.get(field) or []) if not _has_music_block_words(str(s))
            ]
            if items:
                cleaned[field] = items
        if cleaned:
            out["users"][name] = cleaned
    out["eventos_del_grupo"] = [
        e
        for e in (lt.get("eventos_del_grupo") or [])
        if not _has_music_block_words(str(e))
    ]
    out["chistes_internos"] = [
        j
        for j in (lt.get("chistes_internos") or [])
        if not _has_music_block_words(str(j))
    ]
    return out


def _clean_gender_from_long_term(lt: dict) -> dict:
    """Filtra entradas con palabras de género/sexo de la memoria a largo plazo.
    Remueve rasgos/anécdotas/eventos/chistes que contengan género, para que
    el Indio no tenga datos de sexo/género de ningún usuario."""
    if not lt:
        return lt
    out: dict = {"users": {}, "eventos_del_grupo": [], "chistes_internos": []}
    users = lt.get("users") or {}
    for name, data in users.items():
        if not isinstance(data, dict):
            continue
        cleaned: dict[str, list[str]] = {}
        for field in ("traits", "preguntas_tipicas", "anecdotas"):
            items = [
                s for s in (data.get(field) or []) if not _has_gender_words(str(s))
            ]
            if items:
                cleaned[field] = items
        if cleaned:
            out["users"][name] = cleaned
    out["eventos_del_grupo"] = [
        e for e in (lt.get("eventos_del_grupo") or []) if not _has_gender_words(str(e))
    ]
    out["chistes_internos"] = [
        j for j in (lt.get("chistes_internos") or []) if not _has_gender_words(str(j))
    ]
    return out


def _clamp_long_term(lt: dict) -> dict:
    """Enforce structure + per-section caps so a misbehaving Gemini response
    can't blow up the prompt budget."""
    out: dict = {"users": {}, "eventos_del_grupo": [], "chistes_internos": []}
    users = lt.get("users") if isinstance(lt, dict) else None
    if isinstance(users, dict):
        for name, data in list(users.items())[:30]:
            if not isinstance(data, dict):
                continue
            name = str(name)[:60]
            if name.lower() == "indio":
                continue
            src_traits = [str(t)[:120] for t in (data.get("traits") or []) if t]
            src_qs = [str(t)[:120] for t in (data.get("preguntas_tipicas") or []) if t]
            src_anec = [str(t)[:120] for t in (data.get("anecdotas") or []) if t]

            def _no_music_or_gender(t):
                return not _has_music_block_words(t) and not _has_gender_words(t)

            traits = [t for t in src_traits if _no_music_or_gender(t)][
                :_LT_TRAITS_PER_USER
            ]
            qs = [t for t in src_qs if _no_music_or_gender(t)][:_LT_QUESTIONS_PER_USER]
            anec = [t for t in src_anec if _no_music_or_gender(t)][
                :_LT_ANECDOTES_PER_USER
            ]
            if traits or qs or anec:
                out["users"][name] = {
                    "traits": traits,
                    "preguntas_tipicas": qs,
                    "anecdotas": anec,
                }
    events = lt.get("eventos_del_grupo") if isinstance(lt, dict) else None
    if isinstance(events, list):
        out["eventos_del_grupo"] = [
            e
            for e in [str(e)[:120] for e in events if e]
            if not _has_music_block_words(e) and not _has_gender_words(e)
        ][:_LT_GROUP_EVENTS]
    jokes = lt.get("chistes_internos") if isinstance(lt, dict) else None
    if isinstance(jokes, list):
        out["chistes_internos"] = [
            j
            for j in [str(j)[:120] for j in jokes if j]
            if not _has_music_block_words(j) and not _has_gender_words(j)
        ][:_LT_JOKES]
    # Final safety: if still too big after structural clamp, drop oldest events/jokes.
    while len(json.dumps(out, ensure_ascii=False)) > _LONG_TERM_MAX_CHARS:
        if out["eventos_del_grupo"]:
            out["eventos_del_grupo"].pop(0)
        elif out["chistes_internos"]:
            out["chistes_internos"].pop(0)
        elif out["users"]:
            # Drop the oldest-inserted user.
            first = next(iter(out["users"]))
            out["users"].pop(first)
        else:
            break
    return out


def _turns_to_text(turns: list[dict]) -> str:
    """Render a list of {role,parts:[{text}]} turns as plain text for the
    compression prompt. Turns containing ``[voz]`` are skipped — las
    transcripciones de voz no deben alimentar la memoria a largo plazo para
    evitar el feedback loop voz → play_music."""
    lines: list[str] = []
    for t in turns:
        role = t.get("role", "?")
        parts = t.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            continue
        if "[voz]" in text:
            continue
        speaker = "indio" if role == "model" else "grupo"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


async def _compress_long_term(
    current_lt: dict, old_turns: list[dict]
) -> Optional[dict]:
    """Run a Gemini call to fold old verbatim turns into the long-term notes.
    Returns the new long-term dict on success, None on any failure."""
    if not old_turns:
        return None
    convo_text = _turns_to_text(old_turns)
    if not convo_text.strip():
        return None
    user_message = (
        "Memoria actual:\n"
        f"{json.dumps(current_lt or {}, ensure_ascii=False, indent=2)}\n\n"
        "Conversación nueva del grupo:\n"
        f"{convo_text}\n\n"
        "Devolveme SOLO el JSON actualizado."
    )
    try:
        reply = await geminiClient.generate(
            user_message=user_message,
            system_instruction=_COMPRESS_SYSTEM,
            history=None,
            max_output_tokens=2048,
        )
    except geminiClient.GeminiError as e:
        logger.warning(
            "indio compress: gemini failed (%s, status=%s)", e.kind, e.status
        )
        return None
    except Exception as e:
        logger.exception("indio compress: unexpected error")
        analytics.capture_exception(e, properties={"action": "indio_compress_error"})
        return None
    parsed = _extract_json(reply.text)
    if parsed is None:
        logger.warning("indio compress: JSON parse failed; raw=%r", reply.text[:200])
        return None
    return _clamp_long_term(parsed)


async def _maybe_compress(mem_key: str) -> None:
    """Fire-and-forget: if the short-term history is over the threshold,
    distill its oldest portion into long-term notes and drop those turns from
    short-term. Safe against concurrent /indio calls because: (a) we hold the
    per-key lock only at read+write points, and (b) we slice from the FRONT by
    count, not by index, so new turns appended during compression aren't lost."""
    if mem_key in _indio_compressing:
        return
    lock = _indio_locks.get(mem_key)
    if lock is None:
        return
    _indio_compressing.add(mem_key)
    try:
        async with lock:
            history = _indio_history.get(mem_key, [])
            if len(history) < _HISTORY_COMPRESS_THRESHOLD:
                return
            # Even count: keep both sides of each user/model pair aligned.
            drop_count = len(history) - _HISTORY_KEEP_AFTER_COMPRESS
            if drop_count % 2 == 1:
                drop_count -= 1
            if drop_count <= 0:
                return
            old_turns = history[:drop_count]
            current_lt = dict(_indio_long_term.get(mem_key, {}))
        new_lt = await _compress_long_term(current_lt, old_turns)
        if new_lt is None:
            logger.info("indio compress: skipped (lt unchanged) for %s", mem_key)
            return
        async with lock:
            history = _indio_history.get(mem_key, [])
            if len(history) >= drop_count:
                _indio_history[mem_key] = history[drop_count:]
            _indio_long_term[mem_key] = new_lt
        await _persist_indio_state()
        logger.info(
            "indio compress: ok for %s (dropped %d turns, users=%d)",
            mem_key,
            drop_count,
            len(new_lt.get("users", {})),
        )
    finally:
        _indio_compressing.discard(mem_key)


# Maps each Gemini tool name to its internal action label and the key under
# ``args`` where the string argument lives (or ``None`` if the tool takes no
# arguments — pure control verbs like skip/pause/resume/stop).
_FUNCTION_CALL_TO_ACTION: dict[str, tuple[str, Optional[str]]] = {
    "play_music": ("PLAY_MUSIC", "query"),
    "play_sound": ("PLAY_SOUND", "name"),
    "skip_music": ("SKIP_MUSIC", None),
    "pause_music": ("PAUSE_MUSIC", None),
    "resume_music": ("RESUME_MUSIC", None),
    "stop_music": ("STOP_MUSIC", None),
    "dj_mode": ("DJ_MODE", None),
    "generate_image": ("GENERATE_IMAGE", "prompt"),
    "edit_image": ("EDIT_IMAGE", "prompt"),
    "spacewar_guide": ("SPACEWAR_GUIDE", None),
    "use_image": ("USE_IMAGE", None),
}
_ACTION_FALLBACK_TEXT = {
    "PLAY_MUSIC": "🎵 Ahí va",
    "PLAY_SOUND": "🔊 Tomá",
    "SKIP_MUSIC": "⏭️ Siguiente",
    "PAUSE_MUSIC": "⏸️ Pausando",
    "RESUME_MUSIC": "▶️ Dale, va",
    "STOP_MUSIC": "⏹️ Listo",
    "DJ_MODE": "🎧 Modo DJ",
    "GENERATE_IMAGE": "🎨 Generando imagen...",
    "EDIT_IMAGE": "🎨 Editando imagen...",
    "SPACEWAR_GUIDE": "🎮 Ahí va la guía de Spacewar",
    "USE_IMAGE": "🖼️ Ahí va",
}

_ACTION_ARG_MAX_CHARS = 200


def _actions_from_function_calls(function_calls: list[dict]) -> list[tuple[str, str]]:
    """Translate Gemini function calls into the (action, arg) tuples that
    ``_dispatch_indio_actions`` understands. For tools without arguments
    the tuple's second element is the empty string. Unknown tool names and
    malformed args are logged and skipped — we don't want a bad call to
    fall through and dispatch with garbage."""
    actions: list[tuple[str, str]] = []
    for call in function_calls or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        mapping = _FUNCTION_CALL_TO_ACTION.get(name.lower())
        if mapping is None:
            logger.warning(
                "indio: unknown tool call '%s' (args=%r)", name, call.get("args")
            )
            continue
        action, arg_key = mapping
        if arg_key is None:
            args = call.get("args") or {}
            if args and isinstance(args, dict) and any(v for v in args.values()):
                # Tool with multiple args (e.g. use_image with image_id + caption)
                # — pack as small JSON so dispatch can unpack both.
                import json as _json

                actions.append((action, _json.dumps(args, ensure_ascii=False)))
            else:
                # Argument-less control verb (skip/pause/resume/stop).
                actions.append((action, ""))
            continue
        args = call.get("args") or {}
        raw = args.get(arg_key) if isinstance(args, dict) else None
        if not isinstance(raw, str):
            logger.warning(
                "indio: tool %s missing string arg '%s' (got %r)", name, arg_key, raw
            )
            continue
        arg = raw.strip()[:_ACTION_ARG_MAX_CHARS]
        if not arg:
            logger.warning("indio: tool %s called with empty '%s'", name, arg_key)
            continue
        actions.append((action, arg))
    return actions


# --- play_sound anti-misfire gate -----------------------------------------
# El modelo a veces dispara play_sound sin que nadie lo haya pedido (free-
# association de un meme con la charla). Para evitarlo SIN sumar otra llamada a
# Gemini, clasificamos el mensaje crudo con regex/strings (costo cero):
#   1. Hay un verbo de orden explícito (tirá/pone/reproducí/…) → "comandado":
#      suena el clip ya (comportamiento de siempre).
#   2. No hay verbo pero el NOMBRE del clip aparece textual en el mensaje →
#      "espontáneo": el Indio igual responde su texto (se postea antes que el
#      audio) y el clip sale como extra atrás.
#   3. Ni verbo ni nombre en el mensaje → se DESCARTA solo el play_sound; la
#      respuesta de texto se manda igual.

_PLAY_SOUND_ORDER_RE = re.compile(_kw.PLAY_ORDER_RE_SOURCE)

# Palabras genéricas que pueden estar en el nombre de un clip pero que NO deben
# servir para "anclar" el nombre en el mensaje (si no, cualquier 'de'/'que'
# matchearía). Solo se usan para el grounding del modo espontáneo.
_NAME_STOPWORDS = frozenset(
    {
        "de",
        "del",
        "la",
        "las",
        "el",
        "los",
        "un",
        "una",
        "unos",
        "unas",
        "y",
        "o",
        "a",
        "en",
        "que",
        "con",
        "por",
        "para",
        "es",
        "lo",
        "al",
        "ser",
        "muy",
        "the",
        "se",
        "su",
        "mi",
        "tu",
    }
)

_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _strip_accents_lower(s: str) -> str:
    """Normaliza para comparar: minúsculas y sin tildes (tirá→tira)."""
    norm = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in norm if unicodedata.category(c) != "Mn")


def _has_play_sound_order(text: str) -> bool:
    """True si el mensaje del usuario tiene un verbo imperativo de
    reproducción (tirá, pone, metele, reproducí, hacé sonar, traete…)."""
    if not text:
        return False
    return bool(_PLAY_SOUND_ORDER_RE.search(_strip_accents_lower(text)))


def _name_grounded_in_message(name: str, text: str) -> bool:
    """True si el nombre/keyword del clip elegido por el modelo aparece
    textualmente en el mensaje del usuario (match por token/substring, sin
    stopwords). Sirve para permitir el clip 'como extra' cuando alguien nombra
    un audio sin dar la orden explícita."""
    if not name or not text:
        return False
    msg = _strip_accents_lower(text)
    msg_tokens = set(_NAME_TOKEN_RE.findall(msg))
    if not msg_tokens:
        return False
    name_norm = _strip_accents_lower(name)
    tokens = _NAME_TOKEN_RE.findall(name_norm)
    meaningful = [t for t in tokens if len(t) >= 3 and t not in _NAME_STOPWORDS]
    # Nombre todo-stopwords o muy corto: caemos a los tokens crudos para no
    # quedarnos sin nada con qué anclar (ej. 'pez').
    candidates = meaningful or [t for t in tokens if t not in _NAME_STOPWORDS]
    for t in candidates:
        if t in msg_tokens or t in msg:
            return True
    return False


def _gate_play_sound_actions(
    actions: list[tuple[str, str]], raw_text: str
) -> list[tuple[str, str]]:
    """Filtra play_sound espurios. Devuelve la lista de acciones sin los
    PLAY_SOUND que no cumplen ni verbo de orden ni nombre presente en el
    mensaje. El resto de las acciones pasa intacto. No toca el texto de la
    respuesta (que se manda aparte, antes del audio)."""
    if not actions:
        return actions
    commanded = _has_play_sound_order(raw_text)
    kept: list[tuple[str, str]] = []
    for action, arg in actions:
        if action != "PLAY_SOUND":
            kept.append((action, arg))
            continue
        if commanded or _name_grounded_in_message(arg, raw_text):
            kept.append((action, arg))
        else:
            logger.info(
                "indio PLAY_SOUND suprimido: sin verbo de orden y nombre %r "
                "no está en el mensaje %r — solo responde texto",
                arg,
                (raw_text or "")[:80],
            )
    return kept


# --- play_music anti-misfire gate -------------------------------------------
# Mismo patrón que _gate_play_sound_actions: verificar determinísticamente
# sobre el mensaje crudo del usuario que haya verbo de orden. Sin esto, Gemini
# puede llamar play_music para cualquier input de voz aunque no haya pedido
# musical (caso real: "Quiero que sea su racista" → play_music).
#
# NO filtra por "query concreta": "poné algo" es un pedido válido aunque el
# target sea genérico. El gate solo corta los casos donde no hay NINGÚN verbo
# de orden en el mensaje del usuario.


def _gate_play_music_actions(
    actions: list[tuple[str, str]], raw_text: str
) -> list[tuple[str, str]]:
    """Filtra play_music espurios. Solo deja pasar cuando el mensaje del
    usuario tiene un verbo imperativo de reproducción (tirá, poneme, metele,
    etc.). Sin verbo no hay pedido musical. El resto de las acciones pasa
    intacto.

    Los mensajes de voz (``[voz]``) se saltan el gate porque la ASR puede
    distorsionar el verbo (ej. "Pone" → "Opres") — Gemini ya decidió llamar
    play_music y confiamos en su criterio para transcripciones ruidosas."""
    if not actions:
        return actions
    if raw_text and raw_text.strip().startswith("[voz]"):
        return actions
    has_order = _has_play_sound_order(raw_text)
    kept: list[tuple[str, str]] = []
    for action, arg in actions:
        if action != "PLAY_MUSIC":
            kept.append((action, arg))
            continue
        if has_order:
            kept.append((action, arg))
        else:
            logger.info(
                "indio PLAY_MUSIC suprimido: sin verbo de orden (msg=%r, query=%r)",
                (raw_text or "")[:80],
                arg,
            )
    return kept


def _ensure_reply_text(text: str, actions: list[tuple[str, str]]) -> str:
    """The relay flow and Discord both require non-empty content. When the
    model emits only a function call (no accompanying text), substitute a
    short stock confirmation so the chat shows something."""
    if text:
        return text
    if not actions:
        return text
    fallback = _ACTION_FALLBACK_TEXT.get(actions[0][0], "👍")
    return fallback


async def _invoke_slash_via_userbot(
    endpoint: str, channel_id: int, query: str
) -> tuple[bool, str]:
    """Ask the userbot to invoke a VaPls slash command (`/play` or
    `/soundpad`) from the real user account, so Discord shows the full
    "Indio used /play" interaction. Returns (ok, message)."""
    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        logger.warning(
            "indio %s relay disabled: INDIO_RELAY_URL/SECRET missing — "
            "cayendo a playFromIndio (bot principal)",
            endpoint,
        )
        return False, "relay not configured"
    if not channel_id:
        # 0/None channel means INDIO_PLAY_CHANNEL_ID is unset. Without a
        # target text channel the relay would have to guess, so we refuse
        # here and let the caller fall back to playFromIndio (which has
        # its own channel-picking logic).
        logger.warning(
            "indio %s relay disabled: INDIO_PLAY_CHANNEL_ID=0 — cayendo a "
            "playFromIndio (bot principal)",
            endpoint,
        )
        return False, "play channel not configured"
    invoke_url = urljoin(config.INDIO_RELAY_URL, "/" + endpoint)
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"channel_id": int(channel_id), "query": query}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(invoke_url, json=payload, headers=headers) as resp:
                if resp.status < 400:
                    return True, query
                body = await resp.text()
                return False, f"relay HTTP {resp.status}: {body[:100]}"
    except Exception as exc:
        logger.warning("indio %s relay failed: %s", endpoint, exc)
        return False, f"relay error: {exc}"


_ACTION_FAILURE_MESSAGES = {
    # Status code (set by _dispatch_indio_actions) → user-facing message. The
    # indio already promised "dale, va" optimistically *before* the tool ran;
    # these messages get posted **after** the tool fails so the user finds out
    # instead of waiting forever for music that's not coming.
    "resume: not paused": "uh, no había nada pausado para reanudar",
    "resume: no voice channel to rejoin": "no hay nadie en voz al que pueda conectarme",
    "resume: nothing to resume": "no me acuerdo qué estaba sonando, decime qué pongo",
    "pause: not playing": "no estaba sonando nada, no tengo qué pausar",
    "sound: fail — music playing": "no se puede, hay música sonando",
}


def _failure_feedback(status: str) -> Optional[str]:
    """Translate a status string emitted by ``_dispatch_indio_actions`` into a
    user-facing apology, or ``None`` if the status was a success (no feedback
    needed). Used to surface tool failures the indio promised optimistically."""
    if not status:
        return None
    if status in _ACTION_FAILURE_MESSAGES:
        return _ACTION_FAILURE_MESSAGES[status]
    if status.endswith(": no active player"):
        return "no había reproductor activo, no estaba sonando nada"
    if status.endswith(": no voice"):
        return "metete en un canal de voz, si no no puedo hacer nada"
    if status.endswith(": no requester"):
        return "no puedo poner música desde acá — pedímelo desde Discord, en voz"
    if status.startswith("music: fail"):
        # Extract the inner reason after " — " when present.
        _, _, reason = status.partition(" — ")
        return (
            f"no pude poner la música ({reason})"
            if reason
            else "no pude poner la música"
        )
    if status.startswith("sound: fail"):
        _, _, reason = status.partition(" — ")
        return (
            f"no encontré el sonido ({reason})" if reason else "no encontré ese sonido"
        )
    if status.startswith("resume: reconnect failed"):
        return "no pude reconectarme al canal para retomar la música"
    return None


# Short, in-character result lines appended after a successful action. Stored
# without leading punctuation; the joiner adds " — " when editing in place and
# posts the bare line when it has to fall back to a standalone message.
_ACTION_SUCCESS_SUFFIX = {
    "PLAY_MUSIC": "listo 🎵",
    "PLAY_SOUND": "listo 🔊",
    "SKIP_MUSIC": "listo ✅",
    "PAUSE_MUSIC": "listo ✅",
    "RESUME_MUSIC": "listo ✅",
    "STOP_MUSIC": "listo ✅",
    "GENERATE_IMAGE": "listo 🎨",
    "EDIT_IMAGE": "listo 🎨",
}

# When PLAY_MUSIC / PLAY_SOUND go through the userbot relay we only have an
# HTTP 200 from Discord acknowledging the slash interaction — the actual
# yt-dlp download / playback can still fail downstream and we won't learn
# about it. Use a softer suffix so the indio doesn't falsely promise audio
# the user might never hear. The fallback path (playFromIndio /
# play_clip_by_query) runs in-process so the regular "listo" suffix still
# applies there.
_ACTION_RELAY_SUCCESS_SUFFIX = {
    "PLAY_MUSIC": "le pasé el tema al /play 🎵",
    "PLAY_SOUND": "le pasé el clip al /soundpad 🔊",
    "GENERATE_IMAGE": "le pasé el prompt al /generarimagen 🎨",
    "EDIT_IMAGE": "listo 🎨",
}


# Per-guild lock so two concurrent indio dispatches in the same guild
# (e.g. text /indio + voice wake word firing back-to-back) serialize their
# play/sound invocations. Without this, two relay calls race the userbot
# into firing two slash interactions on top of each other.
_dispatch_locks: dict[int, asyncio.Lock] = {}


def _dispatch_lock_for(guild_id: int) -> asyncio.Lock:
    lock = _dispatch_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _dispatch_locks[guild_id] = lock
    return lock


# Music-tools desde Discord requieren que el requester esté en voz.
# Pedidos desde Telegram (vía HTTP /indio sin user_id de Discord) llegan con
# requester_member=None: los dejamos pasar — el relay + playFromIndio eligen
# el canal automáticamente. Un Discord-user que pide desde texto sin estar en
# voz cae en "no voice". Cualquier acción no incluida acá no se gatea.
_MUSIC_ACTIONS = frozenset(
    {
        "PLAY_MUSIC",
        "PLAY_SOUND",
        "SKIP_MUSIC",
        "PAUSE_MUSIC",
        "RESUME_MUSIC",
        "STOP_MUSIC",
        "DJ_MODE",
    }
)

_MUSIC_STATUS_PREFIX = {
    "PLAY_MUSIC": "music",
    "PLAY_SOUND": "sound",
    "SKIP_MUSIC": "skip",
    "PAUSE_MUSIC": "pause",
    "RESUME_MUSIC": "resume",
    "STOP_MUSIC": "stop",
    "DJ_MODE": "dj_mode",
    "GENERATE_IMAGE": "image",
    "EDIT_IMAGE": "image_edit",
}


def _gate_music_action(action: str, member) -> Optional[str]:
    """Return a status string when the music action must be blocked, or None
    when it should proceed.

    ``member is None`` means the request came from Telegram / HTTP (no Discord
    user context). We let it through: the relay will invoke the userbot /play
    and ``playFromIndio`` auto-picks a voice channel, so no requester voice
    state is needed.

    ``member`` with no voice channel is a Discord user who asked from text
    without being in voice — that one stays blocked so we don't play to an
    empty channel.

    Matches the ``: no voice`` suffix ``_failure_feedback`` knows."""
    if member is None:
        # Telegram / HTTP path — allow, let relay + playFromIndio handle it.
        return None
    prefix = _MUSIC_STATUS_PREFIX.get(action, action.lower())
    voice = getattr(member, "voice", None)
    if voice is None or getattr(voice, "channel", None) is None:
        return f"{prefix}: no voice"
    return None


async def _dispatch_indio_actions(
    bot: "discord.Bot",
    guild_id: Optional[int],
    actions: list[tuple[str, str]],
    reply_handle=None,
    reply_text: str = "",
    requester_member: "Optional[discord.Member]" = None,
    *,
    attachment_urls: Optional[list[dict]] = None,
    source_message_id: Optional[int] = None,
    from_voice: bool = False,
) -> list[str]:
    """Run any PLAY_* actions the indio emitted. Both PLAY_MUSIC and
    PLAY_SOUND are invoked through the userbot relay so they show up as
    real "/play" / "/soundpad" slash commands in the chat. Both land in
    ``config.INDIO_PLAY_CHANNEL_ID`` — that's the dedicated room for
    playback regardless of where the conversation is happening. Falls back
    to in-process playback if the relay is unavailable.

    After the action runs the original reply message is **edited in place**
    to append a short result indicator (success suffix or failure reason),
    so the user sees the outcome without a separate message.

    ``reply_handle`` is a ``types.SimpleNamespace`` with:
      - ``via_relay: bool`` — True when the initial reply went via userbot.
      - ``channel_id: Optional[int]`` — channel where the reply was posted.
      - ``message_id: Optional[int]`` — id of the relay-posted message (valid
        when ``via_relay=True``).
      - ``message`` — Discord Message object (valid when ``via_relay=False``).

    ``reply_text`` is the clean persona text that was originally sent; the
    suffix is appended to it before editing.

    Returns short status strings for logging; the indio's main reply is sent
    separately."""
    if not actions or guild_id is None or bot is None:
        return []
    statuses: list[str] = []
    try:
        import playCommand
    except Exception as e:
        logger.exception("indio actions: playCommand import failed")
        analytics.capture_exception(
            e, properties={"action": "indio_actions_playcommand_import_failed"}
        )
        return []
    # Set of (action, "relay") tuples for the playback actions that succeeded
    # only via the userbot relay (so we got an HTTP ack but not a real "queued"
    # confirmation). Drives the softer success suffix at the bottom.
    relayed_success: set[str] = set()
    # Serialize per guild so back-to-back dispatches (e.g. text + voice
    # arriving at the same time) don't race the userbot into firing two
    # slash interactions concurrently. Held across the action loop AND
    # the result-feedback edit.
    async with _dispatch_lock_for(int(guild_id)):
        # When set, the result delivery skips the reply_text prefix so the
        # failure message replaces the indio's text entirely instead of
        # appending to it. Used for voice PLAY_SOUND rejection so the user
        # sees just "no se puede, hay música sonando" without "🔊 Tomá — ".
        _skip_reply_prefix = False
        for action, arg in actions:
            try:
                if action in _MUSIC_ACTIONS:
                    gate_status = _gate_music_action(action, requester_member)
                    if gate_status:
                        statuses.append(gate_status)
                        logger.info("indio %s gated: %s", action, gate_status)
                        continue
                if action == "PLAY_MUSIC":
                    ok, msg = await _invoke_slash_via_userbot(
                        "invoke_play",
                        channel_id=config.INDIO_PLAY_CHANNEL_ID,
                        query=arg,
                    )
                    if ok:
                        relayed_success.add("PLAY_MUSIC")
                    else:
                        logger.warning(
                            "indio PLAY_MUSIC relay failed (%s); falling back to playFromIndio",
                            msg,
                        )
                        ok, msg = await playCommand.playFromIndio(
                            bot, int(guild_id), arg
                        )
                    statuses.append(f"music: {'ok' if ok else 'fail'} — {msg}")
                    logger.info("indio PLAY_MUSIC '%s' → ok=%s msg=%s", arg, ok, msg)
                elif action == "PLAY_SOUND":
                    # Block soundpad when music is playing — would interrupt
                    # the current song.
                    _player = playCommand.guildPlayers.get(int(guild_id))
                    if _player is not None and _player.currentSong:
                        if from_voice:
                            _skip_reply_prefix = True
                        statuses.append("sound: fail — music playing")
                        logger.info(
                            "indio PLAY_SOUND rejected: music playing"
                            " (guild=%s from_voice=%s reply_handle=%s)",
                            guild_id,
                            from_voice,
                            reply_handle is not None,
                        )
                        continue
                    ok, msg = await _invoke_slash_via_userbot(
                        "invoke_soundpad",
                        channel_id=config.INDIO_PLAY_CHANNEL_ID,
                        query=arg,
                    )
                    if ok:
                        relayed_success.add("PLAY_SOUND")
                    else:
                        logger.warning(
                            "indio PLAY_SOUND relay failed (%s); falling back to play_clip_by_query",
                            msg,
                        )
                        try:
                            from soundpadCommand import play_clip_by_query
                        except Exception as e:
                            logger.exception(
                                "indio PLAY_SOUND: soundpadCommand import failed"
                            )
                            analytics.capture_exception(
                                e,
                                properties={
                                    "action": "indio_play_sound_soundpad_import_failed"
                                },
                            )
                            statuses.append("sound: fail — import error")
                            continue
                        guild = bot.get_guild(int(guild_id))
                        if guild is None:
                            statuses.append(f"sound: fail — guild {guild_id} not found")
                            logger.warning(
                                "indio PLAY_SOUND: guild %s not found", guild_id
                            )
                            continue
                        played_path = await play_clip_by_query(bot, guild, query=arg)
                        ok = played_path is not None
                        msg = played_path or "no match"
                    statuses.append(f"sound: {'ok' if ok else 'fail'} — {msg}")
                    logger.info("indio PLAY_SOUND '%s' → ok=%s msg=%s", arg, ok, msg)
                elif action == "GENERATE_IMAGE":
                    target_cid = (
                        getattr(reply_handle, "channel_id", None)
                        or config.INDIO_REPLY_CHANNEL_ID
                        or 1490008278275461280
                    )
                    ok, msg = await _invoke_slash_via_userbot(
                        "invoke_generarimagen",
                        channel_id=target_cid,
                        query=arg,
                    )
                    if ok:
                        relayed_success.add("GENERATE_IMAGE")
                    else:
                        logger.warning(
                            "indio GENERATE_IMAGE relay failed (%s); falling back to direct HF image generation",
                            msg,
                        )
                        try:
                            import huggingfaceImage
                            import discord

                            path = await huggingfaceImage.generate(
                                arg, config.HUGGINGFACE_API_TOKEN
                            )
                            if path:
                                target_channel_id = (
                                    config.INDIO_REPLY_CHANNEL_ID or 1490008278275461280
                                )
                                target_channel = bot.get_channel(target_channel_id)
                                if target_channel is None:
                                    target_channel = await bot.fetch_channel(
                                        target_channel_id
                                    )
                                author_mention = (
                                    f"<@{requester_member.id}>"
                                    if requester_member
                                    else "Alguien"
                                )
                                await target_channel.send(
                                    content=f"{author_mention}, acá está la imagen que me pediste para: **{arg}**",
                                    file=discord.File(path, filename="imagen.png"),
                                )
                                try:
                                    os.unlink(path)
                                except Exception:
                                    pass
                                ok = True
                                msg = "direct success"
                            else:
                                msg = "generation failed"
                        except Exception as e:
                            logger.exception(
                                "direct HF image generation fallback failed"
                            )
                            msg = f"fallback error: {e}"
                    statuses.append(f"image: {'ok' if ok else 'fail'} — {msg}")
                    logger.info(
                        "indio GENERATE_IMAGE '%s' → ok=%s msg=%s", arg, ok, msg
                    )
                elif action == "EDIT_IMAGE":
                    target_cid = (
                        getattr(reply_handle, "channel_id", None)
                        or config.INDIO_REPLY_CHANNEL_ID
                        or 1490008278275461280
                    )

                    if not attachment_urls:
                        ok = False
                        msg = "no image to edit"
                        try:
                            target_channel = bot.get_channel(target_cid)
                            if target_channel is None:
                                target_channel = await bot.fetch_channel(target_cid)
                            await target_channel.send(
                                "❌ Tenés que responder a un mensaje con una imagen para que la pueda editar."
                            )
                        except Exception:
                            pass
                    else:
                        images = [
                            u
                            for u in attachment_urls
                            if u.get("mime_type", "").startswith("image/")
                        ]
                        if not images:
                            ok = False
                            msg = "no image in attachments"
                            try:
                                target_channel = bot.get_channel(target_cid)
                                if target_channel is None:
                                    target_channel = await bot.fetch_channel(target_cid)
                                await target_channel.send(
                                    "❌ El mensaje al que respondiste no tiene ninguna imagen válida."
                                )
                            except Exception:
                                pass
                        else:
                            img = images[0]
                            os.makedirs("image_cache", exist_ok=True)

                            suffix = (
                                os.path.splitext(img.get("filename", "input.png"))[1]
                                or ".png"
                            )
                            input_path = f"image_cache/input_{source_message_id or 'temp'}{suffix}"

                            download_ok = False
                            try:
                                async with aiohttp.ClientSession() as sess:
                                    async with sess.get(
                                        img["url"],
                                        timeout=aiohttp.ClientTimeout(total=15),
                                    ) as resp:
                                        if resp.status == 200:
                                            with open(input_path, "wb") as f:
                                                f.write(await resp.read())
                                            download_ok = True
                            except Exception as e:
                                logger.exception(
                                    "Failed to download replied image for editing"
                                )
                                ok = False
                                msg = f"download failed: {e}"

                            if download_ok:
                                try:
                                    import huggingfaceImage
                                    import discord

                                    output_path = (
                                        await huggingfaceImage.generate_img2img(
                                            arg, input_path
                                        )
                                    )
                                    if output_path:
                                        target_channel = bot.get_channel(target_cid)
                                        if target_channel is None:
                                            target_channel = await bot.fetch_channel(
                                                target_cid
                                            )
                                        author_mention = (
                                            f"<@{requester_member.id}>"
                                            if requester_member
                                            else "Alguien"
                                        )

                                        await target_channel.send(
                                            content=f"{author_mention}, acá está la imagen editada para: **{arg}**",
                                            file=discord.File(
                                                output_path,
                                                filename="imagen_editada.png",
                                            ),
                                        )

                                        try:
                                            os.unlink(output_path)
                                        except Exception:
                                            pass
                                        ok = True
                                        msg = "success"
                                    else:
                                        ok = False
                                        msg = "generation failed"
                                        try:
                                            target_channel = bot.get_channel(target_cid)
                                            if target_channel is None:
                                                target_channel = (
                                                    await bot.fetch_channel(target_cid)
                                                )
                                            await target_channel.send(
                                                f"❌ No se pudo generar la imagen editada para: **{arg}**"
                                            )
                                        except Exception:
                                            pass
                                except Exception as e:
                                    logger.exception(
                                        "Cloudflare image-to-image generation failed"
                                    )
                                    ok = False
                                    msg = str(e)
                                    try:
                                        target_channel = bot.get_channel(target_cid)
                                        if target_channel is None:
                                            target_channel = await bot.fetch_channel(
                                                target_cid
                                            )
                                        error_detail = str(e)
                                        if (
                                            "402" in error_detail
                                            or "Pago Requerido" in error_detail
                                        ):
                                            await target_channel.send(error_detail)
                                        else:
                                            await target_channel.send(
                                                f"❌ Error al editar la imagen: {e}"
                                            )
                                    except Exception:
                                        pass
                                finally:
                                    try:
                                        os.unlink(input_path)
                                    except Exception:
                                        pass
                    statuses.append(f"image_edit: {'ok' if ok else 'fail'} — {msg}")
                    logger.info("indio EDIT_IMAGE '%s' → ok=%s msg=%s", arg, ok, msg)
                elif action == "SPACEWAR_GUIDE":
                    target_cid = (
                        getattr(reply_handle, "channel_id", None)
                        or config.INDIO_REPLY_CHANNEL_ID
                        or 1490008278275461280
                    )
                    guide = (
                        "**🎮 Spacewar — guía rápida**\n\n"
                        "Spacewar es una app de testing de Valve. No hay que instalarla, "
                        "solo tenerla en la biblioteca para ciertos juegos que la necesitan.\n\n"
                        "**¿Ya la tenés?**\n"
                        "1. Abrí Steam.\n"
                        "2. Andá a tu **Biblioteca**.\n"
                        "3. Escribí `Spacewar` en la barra de búsqueda de la izquierda.\n"
                        "4. Si aparece, ya la tenés, no necesitás hacer nada.\n\n"
                        "**Si no la tenés — Windows:**\n"
                        "1. Asegurate de que Steam esté abierto.\n"
                        "2. Presioná `Windows + R`, pegá esto y dale Enter:\n"
                        "   ```\n"
                        "   steam://run/480\n"
                        "   ```\n"
                        "   Steam se abre solito y la descarga/agrega. "
                        "Una vez que aparece en tu biblioteca, se queda ahí para siempre.\n\n"
                        "**Linux / Steam Deck:**\n"
                        "   Abrí una terminal y ejecutá:\n"
                        "   ```\n"
                        "   steam steam://run/480\n"
                        "   ```\n\n"
                        "**Bazzite:**\n"
                        "   Abrí una terminal y ejecutá:\n"
                        "   ```\n"
                        "   flatpak run com.valvesoftware.Steam steam://run/480\n"
                        "   ```\n\n"
                        "⚠️ Si tira error, asegurate de tener Steam abierto antes de mandar el comando."
                    )
                    ok = False
                    try:
                        channel = bot.get_channel(target_cid)
                        if channel is None:
                            channel = await bot.fetch_channel(target_cid)
                        if channel:
                            await channel.send(guide)
                            ok = True
                            msg = "guide sent"
                        else:
                            msg = "channel not found"
                    except Exception as e:
                        logger.exception("indio SPACEWAR_GUIDE failed")
                        msg = f"error: {e}"
                    statuses.append(f"spacewar: {'ok' if ok else 'fail'} — {msg}")
                    logger.info("indio SPACEWAR_GUIDE → ok=%s msg=%s", ok, msg)
                elif action == "USE_IMAGE":
                    import discord as _discord
                    import json as _json

                    # arg is a JSON blob with image_id + optional caption
                    image_id = ""
                    caption = ""
                    if arg and arg.startswith("{"):
                        try:
                            parsed = _json.loads(arg)
                            image_id = (parsed.get("image_id") or "").strip()
                            caption = (parsed.get("caption") or "").strip()
                        except Exception:
                            image_id = arg.strip()
                    else:
                        image_id = arg.strip() if arg else ""
                    mgr = _init_image_mgr()
                    entry = mgr.get_image_entry(image_id) if image_id else None
                    if entry is None:
                        statuses.append("use_image: fail — unknown id")
                        logger.warning("indio USE_IMAGE: unknown image_id=%s", image_id)
                        continue
                    img_path = mgr.get_image_path(image_id)
                    if img_path is None:
                        statuses.append("use_image: fail — file missing")
                        continue
                    target_cid = (
                        getattr(reply_handle, "channel_id", None)
                        or config.INDIO_REPLY_CHANNEL_ID
                        or 1490008278275461280
                    )
                    ok = False
                    msg = ""
                    try:
                        channel = bot.get_channel(target_cid)
                        if channel is None:
                            channel = await bot.fetch_channel(target_cid)
                        if channel is not None:
                            payload = {}
                            if caption:
                                payload["content"] = caption
                            payload["file"] = _discord.File(str(img_path))
                            await channel.send(**payload)
                            ok = True
                            msg = "sent"
                        else:
                            msg = "channel not found"
                    except Exception as e:
                        logger.exception("indio USE_IMAGE failed")
                        msg = f"error: {e}"
                    statuses.append(f"use_image: {'ok' if ok else 'fail'} — {msg}")
                    logger.info("indio USE_IMAGE %s → ok=%s msg=%s", image_id, ok, msg)
                elif action == "DJ_MODE":
                    # Activate Auto-DJ + post the panel — same handler as /dj.
                    # Use the channel where the Indio just replied so the panel
                    # shows up next to the conversation, not in a fixed channel.
                    try:
                        from playCommand import openDjMenu

                        dj_channel_id = getattr(reply_handle, "channel_id", None)
                        ok, msg = await openDjMenu(bot, int(guild_id), dj_channel_id)
                        statuses.append(f"dj_mode: {'ok' if ok else 'fail'} — {msg}")
                        logger.info("indio DJ_MODE → ok=%s msg=%s", ok, msg)
                    except Exception as e:
                        logger.exception("indio DJ_MODE failed")
                        analytics.capture_exception(
                            e, properties={"action": "indio_dj_mode_failed"}
                        )
                        statuses.append("dj_mode: fail — exception")
                elif action in (
                    "SKIP_MUSIC",
                    "PAUSE_MUSIC",
                    "RESUME_MUSIC",
                    "STOP_MUSIC",
                ):
                    # Pure playback controls don't have a slash command equivalent —
                    # they only exist as UI buttons on the player. We talk to the
                    # GuildPlayer directly. If no player exists for this guild it
                    # means nothing was ever queued, so we no-op instead of
                    # implicitly creating one.
                    player = playCommand.guildPlayers.get(int(guild_id))
                    if player is None:
                        statuses.append(f"{action.lower()}: no active player")
                        logger.info(
                            "indio %s: no active player for guild %s", action, guild_id
                        )
                        continue
                    vc = getattr(player, "vc", None)
                    control_ok = False
                    if action == "SKIP_MUSIC":
                        await player.skipSong()
                        statuses.append("skip: ok")
                        control_ok = True
                    elif action == "STOP_MUSIC":
                        await player.stopPlayback()
                        statuses.append("stop: ok")
                        control_ok = True
                    elif action == "PAUSE_MUSIC":
                        if vc and vc.is_playing():
                            await player.togglePausePlay()
                            statuses.append("pause: ok")
                            control_ok = True
                        else:
                            statuses.append("pause: not playing")
                    elif action == "RESUME_MUSIC":
                        if vc and vc.is_paused():
                            await player.togglePausePlay()
                            statuses.append("resume: ok")
                            control_ok = True
                        elif (
                            getattr(player, "interrupted", False) and player.currentSong
                        ):
                            # Bot was kicked / lost connection while a song was
                            # playing. Reconnect to the most-populated voice
                            # channel and pick up where we left off.
                            try:
                                voice_channel = playCommand._pick_voice_channel(
                                    bot,
                                    int(guild_id),
                                )
                            except Exception:
                                voice_channel = None
                            if voice_channel is None:
                                statuses.append("resume: no voice channel to rejoin")
                            else:
                                try:
                                    new_vc = await voice_channel.connect(reconnect=True)
                                    resumed = await player.resumeFromInterruption(
                                        new_vc
                                    )
                                    if resumed:
                                        statuses.append("resume: reconnected & resumed")
                                        control_ok = True
                                    else:
                                        statuses.append("resume: nothing to resume")
                                except Exception as e:
                                    logger.exception(
                                        "indio RESUME_MUSIC reconnect failed"
                                    )
                                    analytics.capture_exception(
                                        e,
                                        properties={
                                            "action": "indio_resume_music_reconnect_failed"
                                        },
                                    )
                                    statuses.append(f"resume: reconnect failed ({e})")
                        else:
                            statuses.append("resume: not paused")
                    logger.info("indio %s → %s", action, statuses[-1])
                    # Mirror the control in the playback channel via the userbot
                    # so the action is visible in #sick-tunes (these tools don't
                    # have slash commands of their own to land there).
                    if control_ok:
                        await _relay_to_userbot(
                            config.INDIO_PLAY_CHANNEL_ID,
                            _ACTION_FALLBACK_TEXT.get(action, "👍"),
                            reply_to_id=None,
                        )
            except Exception as e:
                logger.exception("indio action %s failed", action)
                analytics.capture_exception(
                    e, properties={"action": "indio_action_failed"}
                )
                statuses.append(f"{action.lower()}: fail — exception")

        # After all actions ran, surface the outcome on the indio's reply. When the
        # reply was a single message we EDIT it in place to append a short result
        # line. When it was split into several chunks (rare for the indio's brief
        # confirmations) we can't safely rewrite one chunk with the whole reply, so
        # we post the result as a short standalone message instead. Same fallback
        # when the relay gave us no editable message id. Best-effort: never crash.
        if reply_handle is not None and statuses:
            try:
                primary_action = actions[0][0] if actions else ""
                first_failure = next(
                    (s for s in statuses if _failure_feedback(s) is not None), None
                )
                if first_failure is not None:
                    result_line = _failure_feedback(first_failure) or ""
                elif primary_action in relayed_success:
                    # Userbot relay ack — playback may still fail in VaPls
                    # downstream and we won't know. Soften the wording so the
                    # indio doesn't falsely claim audio that may never play.
                    suffix = _ACTION_RELAY_SUCCESS_SUFFIX.get(
                        primary_action,
                        _ACTION_SUCCESS_SUFFIX.get(primary_action, "listo ✅"),
                    )
                    if (
                        from_voice
                        and primary_action == "PLAY_MUSIC"
                        and config.INDIO_PLAY_CHANNEL_ID
                    ):
                        suffix += f" <#{config.INDIO_PLAY_CHANNEL_ID}>"
                    result_line = suffix
                else:
                    suffix = _ACTION_SUCCESS_SUFFIX.get(primary_action, "listo ✅")
                    if (
                        from_voice
                        and primary_action == "PLAY_MUSIC"
                        and config.INDIO_PLAY_CHANNEL_ID
                    ):
                        suffix += f" <#{config.INDIO_PLAY_CHANNEL_ID}>"
                    result_line = suffix
                if result_line:
                    via_relay = getattr(reply_handle, "via_relay", False)
                    ch_id = getattr(reply_handle, "channel_id", None)
                    msg_obj = getattr(reply_handle, "message", None)
                    single = getattr(reply_handle, "single", True)
                    logger.info(
                        "indio dispatch result: %r single=%s via_relay=%s",
                        result_line,
                        single,
                        via_relay,
                    )
                    edited = False
                    if single:
                        new_content = (
                            result_line
                            if _skip_reply_prefix
                            else f"{reply_text} — {result_line}"
                        )
                        if via_relay:
                            msg_id = getattr(reply_handle, "message_id", None)
                            if ch_id and msg_id:
                                edited = await _edit_via_userbot(
                                    ch_id, msg_id, new_content
                                )
                        elif msg_obj is not None:
                            await msg_obj.edit(content=new_content)
                            edited = True
                    if not edited:
                        # Multi-chunk reply or no editable id: post the result on
                        # its own so the user still finds out what happened.
                        if via_relay and ch_id:
                            await _relay_to_userbot(ch_id, result_line, None)
                        elif msg_obj is not None and getattr(msg_obj, "channel", None):
                            await msg_obj.channel.send(result_line)
            except Exception as e:
                logger.exception("indio dispatch result delivery failed")
                analytics.capture_exception(
                    e, properties={"action": "indio_dispatch_result_delivery_failed"}
                )

        return statuses


# ---------------------------------------------------------------------------
# Music disambiguation: "che, ¿cuál de estas querés?"
# ---------------------------------------------------------------------------

_CHOICE_CANCEL_WORDS = (
    "ninguna",
    "ninguno",
    "ningun",
    "nada",
    "deja",
    "dejalo",
    "dejala",
    "cancela",
    "cancelar",
    "olvidate",
    "olvidalo",
    "no quiero",
    "ni una",
)
# Ordinal/number words → 0-based index. Matched by prefix against each token so
# "primera"/"primero"/"primer" all resolve, etc.
_ORDINAL_STEMS = {
    "primer": 0,
    "uno": 0,
    "segund": 1,
    "dos": 1,
    "tercer": 2,
    "tres": 2,
    "cuart": 3,
    "cuatro": 3,
    "quint": 4,
    "cinco": 4,
}
_CHOICE_STOPWORDS = {
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "de",
    "del",
    "version",
    "tema",
    "cancion",
    "quiero",
    "poneme",
    "pone",
    "poné",
    "dale",
    "esa",
    "ese",
    "esta",
    "este",
    "che",
    "indio",
    "opcion",
    "numero",
    "que",
    "me",
    "y",
    "o",
    "a",
    "porfa",
    "porfavor",
    "mejor",
}


def _normalize_choice(s: str) -> str:
    """Lowercase + strip accents for matching selection utterances."""
    n = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _looks_like_url(query: str) -> bool:
    q = (query or "").strip()
    return (
        q.startswith("http://") or q.startswith("https://") or q.startswith("ytsearch:")
    )


# Words that, when adjacent to an ordinal/number token, signal "this is a
# selection". Required for ordinal-word matching so bare "uno" / "dos" in
# normal speech doesn't get parsed as a vote. Includes the selection article
# ("la 4"), imperatives ("ponela 2", "elegí la tres", "votá la una"), and the
# explicit "opción"/"número" framing.
# Stored without accents — _parse_choice normalizes input the same way via
# _normalize_choice, so the lookup just needs the accent-stripped form.
_SELECTION_CONTEXT_WORDS = {
    "la",
    "el",
    "los",
    "las",
    "ponela",
    "ponelo",
    "poneme",
    "ponete",
    "pone",
    "metele",
    "mete",
    "tirate",
    "tira",
    "tirame",
    "dame",
    "dale",
    "elegi",
    "elegime",
    "elige",
    "vota",
    "voto",
    "votala",
    "votalo",
    "quiero",
    "ese",
    "esa",
    "este",
    "esta",
    "opcion",
    "numero",
    "n",
}


def _parse_choice(text: str, candidates: list[dict]):
    """Interpret a selection utterance against the offered candidates.

    Returns the 0-based index of the chosen candidate, the string ``"cancel"``
    when the speaker declined, or ``None`` when the message doesn't look like a
    selection at all (caller should treat it as a normal new message).

    Resolution order: explicit cancel > digit (1..N) > ordinal word
    (primera/segunda/…) **only when preceded by a selection context word** >
    a distinctive word that matches exactly one title.

    The selection-context requirement on ordinals is what stops normal speech
    ("Uno lava todo") from being parsed as a vote — there's no leading "la" /
    "ponela" / etc., so bare "uno" is ignored.
    """
    if not text or not candidates:
        return None
    norm = _normalize_choice(text)
    for w in _CHOICE_CANCEL_WORDS:
        if w in norm:
            return "cancel"
    n = len(candidates)
    for m in re.finditer(r"\d+", norm):
        v = int(m.group())
        if 1 <= v <= n:
            return v - 1
    tokens = re.findall(r"[a-z]+", norm)
    for i, tok in enumerate(tokens):
        for stem, idx in _ORDINAL_STEMS.items():
            if not tok.startswith(stem) or idx >= n:
                continue
            # Only accept the ordinal if the previous token signals selection
            # intent. Falls through to None if it doesn't — the caller treats
            # this as a normal message (chat / Gemini turn), not a vote.
            prev = tokens[i - 1] if i > 0 else ""
            if prev in _SELECTION_CONTEXT_WORDS:
                return idx
    # Distinctive-word match against the titles (e.g. "la del vivo",
    # "la de Calamaro"). Only commit when exactly one title wins.
    sig = [t for t in tokens if len(t) >= 3 and t not in _CHOICE_STOPWORDS]
    if sig:
        scores = []
        for c in candidates:
            title = _normalize_choice(c.get("title", ""))
            scores.append(sum(1 for w in sig if w in title))
        best = max(scores)
        if best > 0 and scores.count(best) == 1:
            return scores.index(best)
    return None


_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "\U0001f51f"]


def _num_emoji(i: int) -> str:
    """Keycap emoji for a 1-based position (display only)."""
    return _NUM_EMOJI[i - 1] if 1 <= i <= len(_NUM_EMOJI) else f"{i})"


def _format_choices(candidates: list[dict]) -> str:
    """Render the "¿cuál querés?" list the indio posts in chat."""
    lines = ["che, ¿cuál de estas querés?"]
    for i, c in enumerate(candidates, 1):
        dur = c.get("duration_string") or ""
        durs = f" [{dur}]" if dur else ""
        lines.append(f"{_num_emoji(i)} {c['title']}{durs}")
    lines.append('(decime el número, o "ninguna")')
    return "\n".join(lines)


def _choice_identity(user_id, speaker: str) -> str:
    """Stable identity for a requester's pending choice.

    Prefers the Discord user id (numeric, globally unique) so two members
    sharing a display name can't resolve each other's choice. Falls back to the
    display name only when no id is available. The ``uid:``/``nm:`` prefixes
    keep a numeric display name from ever colliding with a real id.
    """
    if user_id:
        return f"uid:{user_id}"
    return f"nm:{speaker or 'alguien'}"


def _voter_id_from(user_id, speaker: str) -> int:
    """Resolve a stable integer "voter id" for MusicVote.register_vote, which
    keys ``votes`` by int (Discord uid). Falls back to a deterministic hash of
    the speaker name when no real id is available (older voice messages)."""
    try:
        if user_id:
            return int(user_id)
    except (TypeError, ValueError):
        pass
    # Negative-space "name" voter ids so they never collide with real Discord
    # uids (which are positive).
    name = speaker or "alguien"
    return -(hash(name) & 0xFFFFFFFF) or -1


def _try_register_chat_vote(guild_id: Optional[int], user_id: int, text: str) -> bool:
    """Bridge for typed-chat votes ("indio ponela 4" in text). Same idea as
    ``try_register_voice_vote`` but doesn't close immediately — typing is more
    deliberate, but we still want to give the group its sliding window."""
    if not guild_id or not text:
        return False
    import playCommand

    vote = playCommand.get_active_vote(int(guild_id))
    if vote is None:
        return False
    decision = _parse_choice(text, vote.candidates)
    if not isinstance(decision, int) or not (0 <= decision < len(vote.candidates)):
        return False
    return vote.register_vote(int(user_id), decision)


def try_register_voice_vote(
    *, guild_id: Optional[int], user_id: int, speaker_name: str, text: str
) -> bool:
    """Try to register a voice utterance as a vote on the guild's open music
    poll. Returns True when ``text`` parses as a choice and a vote is recorded;
    False when there's no open vote, no guild context, or ``text`` doesn't name
    an option.

    Voice votes are decisive: this is someone literally telling the indio "ponela
    4", so we close the vote immediately (``close_now=True``) instead of waiting
    out the sliding window. Called from the apiServer **before** the indio
    dispatch so the raw transcript's digit ("Indio, tirala 4") is captured as
    a vote instead of being interpreted as a chat message.
    """
    if not guild_id or not text:
        return False
    import playCommand

    vote = playCommand.get_active_vote(int(guild_id))
    if vote is None:
        return False
    # Voice votes are restricted to the requester (the user who triggered the
    # vote via /play or by asking the indio to play). Other speakers' votes
    # are silently dropped — the userbot's WakeWordSink already filters them
    # at the VOSK layer, and this is the main-bot backstop in case a transcript
    # races through the close window or the relay restriction sync lags.
    if vote.requester_id and user_id and int(user_id) != int(vote.requester_id):
        return False
    decision = _parse_choice(text, vote.candidates)
    if not isinstance(decision, int) or not (0 <= decision < len(vote.candidates)):
        return False
    voter = _voter_id_from(user_id, speaker_name or "")
    return vote.register_vote(voter, decision, close_now=True)


def register_reaction_vote(
    *, channel_id: int, message_id: int, emoji: str, user_id: int
) -> bool:
    """Count an emoji reaction on a vote's options message as a vote.
    \u274c cancels the vote instead of picking an option.

    Called from the main bot's on_raw_reaction_add. Looks up the open vote
    by its options message, maps the keycap emoji to an option, and records the
    reactor's pick keyed by user id. Reactions slide the timer (no close_now);
    the assumption is multiple people may be reacting in sequence and we want
    to give them a window.
    """
    import playCommand

    emoji = (emoji or "").strip()
    is_cancel = emoji == "\u274c"

    if not is_cancel:
        idx = playCommand.emoji_to_index(emoji)
        if idx is None:
            # Some clients drop the variation selector -- try the bare keycap too.
            idx = playCommand.emoji_to_index(emoji.replace("\ufe0f", ""))
        if idx is None:
            return False
    try:
        cid = int(channel_id)
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    for vote in playCommand.active_votes.values():
        if vote.closed:
            continue
        if vote.reaction_message_id == mid and vote.reaction_channel_id == cid:
            if is_cancel:
                vote.cancel()
                return True
            if idx >= len(vote.candidates):
                return False
            return vote.register_vote(int(user_id), idx)
    return False


async def _relay_dm_user(user_id: int, content: str) -> bool:
    """DM ``content`` to ``user_id`` from the userbot (the cuenta real).

    Used so the alert "te respondí en <#X>" venga del Indio "real" en DM,
    no del bot vapls. Returns True on success, False when the relay is off,
    the user has DMs closed, or anything else fails.
    """
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret or not user_id:
        return False
    dm_url = urljoin(url, "/dm")
    payload = {"user_id": int(user_id), "content": content}
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(dm_url, json=payload, headers=headers) as resp:
                return resp.status < 400
    except Exception:
        logger.info("indio: DM relay failed (network/timeout)")
        return False


async def _relay_say(channel_id: int, content: str) -> Optional[int]:
    """Post ``content`` via the userbot relay and return the first message id
    (so the main bot can react to it), or None if the relay is off/failed.
    Mirrors _relay_to_userbot but surfaces the message id."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret:
        return None
    payload = {"channel_id": int(channel_id), "content": content}
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json(content_type=None)
        ids = (data or {}).get("message_ids") or []
        return int(ids[0]) if ids else None
    except Exception as e:
        logger.exception("indio relay say (with id) failed")
        analytics.capture_exception(e, properties={"action": "indio_relay_say_failed"})
        return None


async def _attach_vote_reactions(
    bot, vote, channel_id: int, message_id: int, n: int
) -> None:
    """Remember which message carries this vote and seed it with the number
    reactions (1️⃣…N) so people can vote by reacting. Best-effort.

    Refuses to overwrite an existing binding: once a vote has been attached
    to a message, **a later turn's unrelated reply must not steal it**. That
    was the 2026-05-31 bug — a chat reply ("¡pará, Enrique!…") got 1-5
    reactions slapped on it because a music vote from an earlier turn was
    still open in the guild.
    """
    if vote is None or not channel_id or not message_id:
        return
    if vote.reaction_message_id is not None:
        # Already bound to a real options message. Don't repoint.
        return
    vote.reaction_channel_id = int(channel_id)
    vote.reaction_message_id = int(message_id)
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        for i in range(1, min(n, len(_NUM_EMOJI)) + 1):
            await msg.add_reaction(_num_emoji(i))
        await msg.add_reaction("❌")
    except Exception as e:
        logger.exception("indio vote: attaching reactions failed")
        analytics.capture_exception(
            e, properties={"action": "indio_vote_attaching_reactions_failed"}
        )


async def _play_chosen_song(bot, guild_id: int, song: dict) -> None:
    """Play an already-resolved candidate (id + title in hand). We reuse the
    yt-dlp result we got when building the options list, so there is no second
    search and no Gemini call — we just hand the song to the player."""
    import playCommand

    try:
        await playCommand.playFromIndio(
            bot,
            guild_id,
            song.get("title") or "tema",
            songs=[song],
        )
    except Exception as e:
        logger.exception("indio: play chosen song failed")
        analytics.capture_exception(
            e, properties={"action": "indio_play_chosen_song_failed"}
        )


_INDIO_PREFIX_RE = re.compile(
    r"^\s*[\[\(]?\s*(el\s+)?indio\s*[\]\)]?\s*[:\-—]\s*",
    re.IGNORECASE,
)

# Generic "[Name]:" / "(Name):" speaker tag at the very start of a reply. The
# model picks it up by mirroring the "Name: contenido" format it sees in user
# turns and sometimes re-bracketing it. Cap the bracketed name to 40 chars and
# forbid newlines / nested brackets so we don't eat real bracketed content in
# the middle of a sentence.
_LEADING_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*[\[\(]\s*[^\]\)\n]{1,40}\s*[\]\)]\s*[:\-—]\s*",
)

_LITERAL_CMD_RE = re.compile(r"/(play|soundpad)\s+(.+)$", re.MULTILINE | re.IGNORECASE)


def _strip_speaker_prefix(text: str) -> str:
    """Drop a leading "[indio]:" / "Indio:" / "[Miles]:" / "(el indio) -" style
    prefix from a model reply. The model sometimes mirrors the speaker tag
    format it sees in user turns even though INDIO_SYSTEM tells it not to.

    Applies first the indio-specific stripper (also catches bareword "Indio:"
    without brackets), then the generic bracketed-name stripper."""
    if not text:
        return text
    out = _INDIO_PREFIX_RE.sub("", text, count=1)
    out = _LEADING_SPEAKER_PREFIX_RE.sub("", out, count=1)
    return out.lstrip()


# Legacy "[Name]:" speaker prefix from before we switched to unbracketed
# "Name:" format. On load, rewrite the brackets out but preserve the name so
# the model still knows who said what for those legacy turns.
_LEGACY_BRACKETED_SPEAKER_RE = re.compile(r"^(\s*)\[([^\]\n]{1,40})\]:\s*")

# Custom Discord emoji markup: <:name:id> / <a:name:id>. Stored history doesn't
# need it — the system prompt's _format_guild_emojis block already teaches the
# model how to emit it. Keeping the markup in history was creating a feedback
# loop where the model picked up the ":name:" shortcode shape from its own
# prior replies and started using bare shortcodes (which Discord won't render).
_CUSTOM_EMOJI_MARKUP_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")

# Bare emoji shortcodes :name:. Lookbehind/lookahead on \w avoids eating
# legitimate ":" in URLs ("http://foo:8080") or ratios ("4:3").
_EMOJI_SHORTCODE_RE = re.compile(r"(?<!\w):[A-Za-z0-9_]{2,}:(?!\w)")

# Discord mention/channel/role markup: <@123>, <@!123> (legacy nickname mention),
# <#456> (channel), <@&789> (role). These render as clickable pills in the
# Discord client but are opaque numeric ids in raw text — pure noise in
# persisted memory.
_DISCORD_MENTION_RE = re.compile(r"<[@#][&!]?\d+>")

# Collapse 3+ blank lines into 2 (sanitization can leave extra whitespace).
_MULTIBLANK_RE = re.compile(r"\n{3,}")


def _sanitize_for_history(text: str) -> str:
    """Clean a string before it enters ``_indio_history``.

    - Rewrites a legacy bracketed speaker prefix ``[Name]:`` to ``Name:`` so
      historical user turns keep their speaker identity in the new format.
    - Strips custom Discord emoji markup ``<:name:id>`` and ``<a:name:id>``.
    - Strips bare emoji shortcodes ``:name:``.
    - Strips Discord user/channel/role mentions ``<@123>`` / ``<#456>`` /
      ``<@&789>``: opaque numeric ids that the model can't interpret.

    The visible reply to Discord is NOT passed through this — emojis and
    mentions still render in the chat. Only the persisted memory is scrubbed,
    breaking the feedback loop where the model imitated the noise from its
    own past turns."""
    if not text:
        return text
    out = _LEGACY_BRACKETED_SPEAKER_RE.sub(r"\1\2: ", text, count=1)
    out = _CUSTOM_EMOJI_MARKUP_RE.sub("", out)
    out = _EMOJI_SHORTCODE_RE.sub("", out)
    out = _DISCORD_MENTION_RE.sub("", out)
    out = _MULTIBLANK_RE.sub("\n\n", out)
    return out.strip()


async def _relay_to_userbot(
    channel_id: int, content: str, reply_to_id: Optional[int]
) -> Optional[list[int]]:
    """POST the indio reply to the userbot's local /say endpoint so it gets
    posted by the real user account.

    Returns a list of message ids (from the userbot's JSON response
    ``{"sent": N, "message_ids": [...]}``) on success, or ``None`` on any
    failure / when the relay is not configured.  Callers that only care about
    success/failure can test the result via truthiness — a non-empty list is
    truthy and ``None`` is falsy, so ``if relayed:`` / ``if not relayed:``
    patterns keep working unchanged."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret:
        return None
    payload = {"channel_id": int(channel_id), "content": content}
    if reply_to_id is not None:
        payload["reply_to_message_id"] = int(reply_to_id)
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("indio relay HTTP %d: %s", resp.status, body[:200])
                    return None
                data = await resp.json(content_type=None)
        ids = (data or {}).get("message_ids") or []
        # The relay succeeded. Normally it echoes the ids of the messages it
        # posted; if for some reason it doesn't, return ``[0]`` as a "sent but
        # id unknown" truthy sentinel so the truthiness contract holds for the
        # 9 callers. Id 0 is never a real Discord id, and the in-place editor
        # guards with ``if ch_id and msg_id`` so it's safely skipped (it posts
        # a standalone result line instead).
        return [int(i) for i in ids] if ids else [0]
    except asyncio.TimeoutError:
        logger.warning("indio relay timeout after %.1fs", config.INDIO_RELAY_TIMEOUT)
        return None
    except Exception as e:
        logger.exception("indio relay failed")
        analytics.capture_exception(e, properties={"action": "indio_relay_failed"})
        return None


async def _edit_via_userbot(channel_id: int, message_id: int, content: str) -> bool:
    """Ask the userbot to edit a previously-posted message in place.

    Mirrors ``_relay_to_userbot`` but POSTs to the ``/edit`` endpoint.
    Body: ``{"channel_id": int, "message_id": int, "content": str}``.
    Header: ``X-API-Secret: config.INDIO_RELAY_SECRET``.

    Returns True when the userbot responds with HTTP < 400, False otherwise
    (including when the relay is not configured).  Never raises."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret:
        return False
    edit_url = urljoin(url, "/edit")
    payload = {
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "content": content,
    }
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(edit_url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "indio relay edit HTTP %d: %s", resp.status, body[:200]
                    )
                    return False
                return True
    except asyncio.TimeoutError:
        logger.warning(
            "indio relay edit timeout after %.1fs", config.INDIO_RELAY_TIMEOUT
        )
        return False
    except Exception:
        logger.warning("indio relay edit failed: %s", "see traceback", exc_info=True)
        return False


def _format_contributors_line() -> str:
    """Thin wrapper kept for module-internal callers; logic now lives in
    ``geminiKeys`` so other commands (e.g. /soundpad) can reuse it."""
    return geminiKeys.format_contributors_line()


def _error_message(kind: str, status: Optional[int], persona: str) -> str:
    """Return a user-facing error message for Gemini failures.

    Args:
        kind: Error type emitted by geminiClient.
        status: Optional HTTP status.
        persona: "vapls" or "indio".

    Returns:
        Localized error string for Discord.
    """
    is_indio = persona == "indio"
    if kind == "config":
        return "⚙️ Gemini no está configurado. Avisale al admin."
    if kind == "timeout":
        return (
            "⏱️ Che, me colgué. Mandalo de nuevo."
            if is_indio
            else "⏱️ Gemini tardó demasiado. Probá de nuevo."
        )
    if kind == "http":
        if status == 429:
            base = (
                f"⏳ Me quedé sin cupo de IA por ahora. Si querés que "
                f"siga respondiendo, conseguite una key gratis en "
                f'{config.GEMINI_KEYS_DONATION_URL} (botón "Create API key") '
                f"y mandámela por DM al bot — la sumo al pool al toque."
            )
            credits = _format_contributors_line()
            return f"{base}\n\n{credits}" if credits else base
        if status == 503:
            # Gemini sobrecargado / caído: outage transitorio del lado de Google,
            # no un bug nuestro. Pedimos reintentar en un rato.
            return (
                "😵 La IA está caída ahora mismo (sobrecargada). Probá en un rato."
                if is_indio
                else "😵 Gemini no está disponible en este momento (servicio sobrecargado). Probá en un rato."
            )
        return (
            f"🌐 Algo se rompió (HTTP {status}). Probá de nuevo."
            if is_indio
            else f"❌ Gemini falló (HTTP {status})."
        )
    if kind == "blocked":
        return (
            "🤐 No, eso no lo contesto acá. ¿Cambiamos de tema?"
            if is_indio
            else "🤐 No puedo responder esto (filtros de seguridad). Reformulá."
        )
    if kind == "empty":
        return (
            "🤐 Eh, me quedé en blanco. Probá de nuevo."
            if is_indio
            else "🤐 Gemini no devolvió texto. Probá de nuevo."
        )
    if kind == "parse":
        return "❌ Respuesta rara de Gemini. Probá de nuevo."
    return "❌ Algo se rompió. Probá de nuevo."


async def vaplsLogic(ctx: discord.ApplicationContext, pregunta: str, router=None):
    """Handle the /vapls command using a stateless Gemini prompt.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.
        router: Optional :class:`channelRouting.ChannelRouter` built by the
            caller (``bot._begin_routed``). When provided, public output is
            routed to the home channel via ``router.post()``; when absent the
            old ``PUBLIC_ALLOWED_CHANNEL_IDS`` guard is used as fallback so
            calling code that doesn't pass a router keeps working.

    Returns:
        None.

    Side Effects:
        Sends Discord messages and emits analytics events.

    Async:
        This function is a coroutine and must be awaited.
    """
    t0 = time.monotonic()

    # Determine whether public posting is allowed in the source channel.
    # When a router is provided it handles routing: public output always goes
    # to the home channel via router.post(), so we treat public_allowed=True.
    # Without a router we fall back to the legacy allowlist check.
    if router is not None:
        public_allowed = True  # router.post() handles destination
    else:
        _chan_id = getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None
        )
        public_allowed = _chan_id in config.PUBLIC_ALLOWED_CHANNEL_IDS

    async def _send_public(content, **kw):
        if router is not None:
            return await router.post(content, **kw)
        return await ctx.followup.send(content, **kw)

    notifier, retry_state = _make_retry_notifier(ctx)
    try:
        reply = await geminiClient.generate(
            user_message=pregunta,
            system_instruction=VAPLS_SYSTEM,
            history=None,
            on_retry=notifier,
        )
    except geminiClient.GeminiError as e:
        msg = _error_message(e.kind, e.status, "vapls")
        # Cuando es rate-limit, mostramos solo al que invocó para no
        # ensuciar el canal con texto que no aporta a la conversación.
        is_rate_limited = e.kind == "http" and e.status == 429
        try:
            # Si ya se editó el deferred con el aviso de rotación, sobrescribirlo
            # con el mensaje de error en vez de mandar un followup separado
            # (evita "aviso colgado + error" en cascada). En ese caso el error
            # queda público, igual que el aviso — no hay forma de hacer un edit
            # del original a "ephemeral", así que sacrificamos el ephemeral
            # del rate limit para no dejar mensajes huérfanos.
            # En canal no permitido el error tambien sale ephemeral. El edit
            # del deferred no puede ser ephemeral, asi que en ese caso saltamos
            # el edit y mandamos un followup ephemeral.
            err_ephemeral = is_rate_limited or not public_allowed
            if retry_state["had_retry"] and not err_ephemeral:
                from bot import safeEdit

                await safeEdit(ctx, msg)
            else:
                await ctx.followup.send(msg, ephemeral=err_ephemeral)
        except Exception:
            pass
        analytics.capture(
            "vapls failed",
            user=ctx.author,
            guild=ctx.guild,
            properties={
                "error_kind": e.kind,
                "http_status": e.status,
                "finish_reason": e.finish_reason,
                "prompt_length": len(pregunta or ""),
            },
        )
        analytics.capture_exception(
            e, user=ctx.author, guild=ctx.guild, properties={"action": "vapls_generate"}
        )
        return
    except Exception as e:
        logger.exception("vapls unexpected error")
        try:
            await ctx.followup.send(
                "❌ Algo se rompió. Probá de nuevo.", ephemeral=not public_allowed
            )
        except Exception:
            pass
        analytics.capture_exception(
            e,
            user=ctx.author,
            guild=ctx.guild,
            properties={"action": "vapls_unexpected"},
        )
        return

    try:
        full_text = _format_user_header(ctx, pregunta) + reply.text
        if router is not None:
            # Multi-chunk routing: each chunk goes through router.post()
            chunks = _split_for_discord(full_text)
            for c in chunks:
                await _send_public(c)
            n_chunks = len(chunks)
        else:
            n_chunks = await _send_reply(
                ctx,
                full_text,
                edit_first=True,
                ephemeral=not public_allowed,
            )
    except Exception as e:
        logger.exception("vapls send failed")
        analytics.capture_exception(
            e, user=ctx.author, guild=ctx.guild, properties={"action": "vapls_send"}
        )
        return

    analytics.capture(
        "vapls invoked",
        user=ctx.author,
        guild=ctx.guild,
        properties={
            "prompt_length": len(pregunta or ""),
            "response_length": len(reply.text),
            "response_chunks": n_chunks,
            "finish_reason": reply.finish_reason,
            "prompt_tokens": reply.prompt_tokens,
            "response_tokens": reply.response_tokens,
            "model": reply.model,
            "latency_ms": int((time.monotonic() - t0) * 1000),
        },
    )


async def indioLogic(
    ctx: discord.ApplicationContext, pregunta: str, nuevo: bool, router=None
):
    """Handle the /indio command with short-term conversation memory.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.
        nuevo: Whether to reset the conversation history.
        router: Optional :class:`channelRouting.ChannelRouter` built by the
            caller (``bot._begin_routed``). When provided, public output is
            routed via ``router.post()``; when absent the legacy
            ``target_channel`` / ``INDIO_REPLY_CHANNEL_ID`` mechanism is used
            for backward compatibility.

    Returns:
        None.

    Side Effects:
        Updates in-memory history, sends Discord messages, and emits analytics.

    Async:
        This function is a coroutine and must be awaited.
    """
    _evict_stale_indio()
    mem_key = _indio_memory_key(ctx)
    lock = _indio_locks.setdefault(mem_key, asyncio.Lock())
    speaker = getattr(ctx.author, "display_name", None) or getattr(
        ctx.author, "name", "alguien"
    )
    tagged_message = f"{speaker}: {pregunta or ''}"

    if router is not None:
        # Use ChannelRouter for routing — avoids duplicating the logic.
        target_channel = getattr(router, "target", None)
    else:
        # Legacy path: build target_channel from INDIO_REPLY_CHANNEL_ID directly.
        override_id = config.INDIO_REPLY_CHANNEL_ID
        target_channel = ctx.bot.get_channel(override_id) if override_id else None
        if override_id and target_channel is None:
            logger.warning(
                "indioLogic: INDIO_REPLY_CHANNEL_ID=%s no resuelve a canal — caigo "
                "al canal del slash",
                override_id,
            )

    async def _post(content, **kw):
        """Postea contenido publico del Indio. Va al target_channel si el
        override esta activo; si no, via ctx.followup.send (canal del slash).
        Los mensajes ephemeral siempre se mandan via followup."""
        if router is not None:
            return await router.post(content, **kw)
        if target_channel is not None and not kw.get("ephemeral"):
            return await target_channel.send(content)
        return await ctx.followup.send(content, **kw)

    def _reply_channel_id():
        if target_channel is not None:
            return target_channel.id
        return getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None
        )

    # How the winner gets announced when the vote closes (relay as the real
    # indio when configured, else via this command's response).
    async def _post_choice(text):
        channel_id = _reply_channel_id()
        relayed = False
        if (
            channel_id is not None
            and config.INDIO_RELAY_URL
            and config.INDIO_RELAY_SECRET
        ):
            relayed = await _relay_to_userbot(channel_id, text, None)
        if not relayed:
            await _post(text)

    # If a music vote is open for this guild and the message names an option,
    # count it as a vote (anyone can vote) instead of a brand-new turn. Keyed by
    # the Discord user id so each person gets one vote.
    _choice_guild_id = getattr(getattr(ctx, "guild", None), "id", None)
    _choice_identity_val = _choice_identity(
        getattr(getattr(ctx, "author", None), "id", None) or 0, speaker
    )
    if (
        not nuevo
        and _choice_guild_id is not None
        and _try_register_chat_vote(
            int(_choice_guild_id),
            int(getattr(getattr(ctx, "author", None), "id", None) or 0),
            pregunta or "",
        )
    ):
        return

    # Conversation is paused while a music vote is open in the guild. The
    # vote-choice shortcut above already let a vote-naming message through;
    # anything else (a /indio with an off-topic question, or an unrelated chat
    # message) is bounced with a hint so we don't spawn cascading Gemini turns
    # — or, worse, a brand-new vote — on top of the open one.
    if not nuevo and _choice_guild_id is not None:
        import playCommand

        _open_vote = playCommand.get_active_vote(int(_choice_guild_id))
        if _open_vote is not None:
            try:
                await ctx.followup.send(
                    "che, hay una votación de música abierta — decidí primero "
                    "(o esperá que cierre).",
                    ephemeral=True,
                )
            except Exception as e:
                logger.exception("indio: notify-vote-open failed")
                analytics.capture_exception(
                    e, properties={"action": "indio_notify_vote_open_failed"}
                )
            return

    async with lock:
        history_reset = False
        if nuevo:
            had_state = bool(_indio_history.get(mem_key)) or bool(
                _indio_long_term.get(mem_key)
            )
            _indio_history.pop(mem_key, None)
            _indio_last_seen.pop(mem_key, None)
            _indio_long_term.pop(mem_key, None)
            if had_state:
                history_reset = True
                analytics.capture(
                    "indio history reset",
                    user=ctx.author,
                    guild=ctx.guild,
                    properties={"trigger": "nuevo_param", "scope": "guild"},
                )
        history_snapshot = list(_indio_history.get(mem_key, []))
        long_term_snapshot = dict(_indio_long_term.get(mem_key, {}))
    if history_reset:
        await _persist_indio_state()

    guild_for_extras = getattr(ctx, "guild", None)
    guild_id = getattr(guild_for_extras, "id", None)
    # Lazy daily refresh of the Main characters roster into persistent memory.
    await _maybe_refresh_current_members(mem_key, guild_id)
    current_members = list(_indio_current_members.get(mem_key, []))
    lt_block = _format_long_term(long_term_snapshot, current_members)
    emoji_count = len(getattr(guild_for_extras, "emojis", None) or [])
    emoji_block = _format_guild_emojis(guild_for_extras)
    player_block = _format_player_state(getattr(ctx, "bot", None), guild_id)
    logger.info(
        "indio: roster=%d, lt_users=%d, emojis=%d (mem_key=%s)",
        len(current_members),
        len((long_term_snapshot.get("users") or {})),
        emoji_count,
        mem_key,
    )
    # Stable cache prefix: persona + long-term notes + emojis (change rarely
    # within a session). Player state is volatile (current track/queue) so it
    # rides in volatile_context, out of the cached system prompt.
    stable_extras = "\n\n".join(b for b in (lt_block, emoji_block) if b)
    system_instruction = INDIO_SYSTEM + (
        f"\n\n{stable_extras}" if stable_extras else ""
    )
    system_instruction = _inject_image_catalog(system_instruction)

    t0 = time.monotonic()
    # Solo activamos el aviso de rotación cuando el Indio responde en el canal
    # del slash. Con override (target_channel), el aviso editaría el deferred
    # del invoker — que puede estar en otro canal — y queda fuera de contexto.
    if target_channel is None:
        notifier, retry_state = _make_retry_notifier(ctx)
    else:
        notifier = None
        retry_state = {"had_retry": False}
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=_stamp_history_for_prompt(history_snapshot, time.time()),
            tools=_INDIO_TOOLS,
            volatile_context=player_block or None,
            on_retry=notifier,
        )
    except geminiClient.GeminiError as e:
        msg = _error_message(e.kind, e.status, "indio")
        is_rate_limited = e.kind == "http" and e.status == 429
        try:
            if is_rate_limited:
                # Posteamos el aviso visible para todos via el userbot (cuando
                # esta disponible) para que el indio "real" sea quien dice que
                # se quedo sin cupo. Header primero, para dar contexto.
                header = _format_user_header(ctx, pregunta).rstrip()
                # Si hubo retry y respondemos en el canal del slash, el header
                # va al slot del deferred (sobrescribe el aviso).
                if retry_state["had_retry"] and target_channel is None:
                    from bot import safeEdit

                    await safeEdit(ctx, header)
                else:
                    await _post(header)
                channel_id = _reply_channel_id()
                relayed = False
                if (
                    channel_id is not None
                    and config.INDIO_RELAY_URL
                    and config.INDIO_RELAY_SECRET
                ):
                    relayed = await _relay_to_userbot(channel_id, msg, None)
                if not relayed:
                    await _post(msg)
            elif retry_state["had_retry"] and target_channel is None:
                # Sobrescribir el aviso con el mensaje de error en vez de
                # acumular dos mensajes (aviso + error).
                from bot import safeEdit

                await safeEdit(ctx, msg)
            else:
                await _post(msg)
        except Exception:
            pass
        analytics.capture(
            "indio failed",
            user=ctx.author,
            guild=ctx.guild,
            properties={
                "error_kind": e.kind,
                "http_status": e.status,
                "finish_reason": e.finish_reason,
                "prompt_length": len(pregunta or ""),
                "history_size_before": len(history_snapshot),
                "nuevo": nuevo,
            },
        )
        analytics.capture_exception(
            e, user=ctx.author, guild=ctx.guild, properties={"action": "indio_generate"}
        )
        return
    except Exception as e:
        logger.exception("indio unexpected error")
        try:
            await _post("❌ Algo se rompió. Probá de nuevo.")
        except Exception:
            pass
        analytics.capture_exception(
            e,
            user=ctx.author,
            guild=ctx.guild,
            properties={"action": "indio_unexpected"},
        )
        return

    pending_actions = _actions_from_function_calls(reply.function_calls)
    pending_actions = _gate_play_sound_actions(pending_actions, pregunta)
    pending_actions = _gate_play_music_actions(pending_actions, pregunta)
    clean_reply = _strip_speaker_prefix(reply.text)
    clean_reply = _ensure_reply_text(clean_reply, pending_actions)
    relayed_via_userbot = False
    import playCommand

    _active_vote = playCommand.get_active_vote(int(getattr(ctx.guild, "id", 0) or 0))
    # "vote_open" here means "this turn just opened a vote and the reply IS
    # the options listing". A vote that already has a reaction_message_id
    # belongs to a previous turn — don't treat the current reply as its
    # surface (otherwise unrelated chat replies get 1-5 reactions slapped on).
    vote_open = _active_vote is not None and _active_vote.reaction_message_id is None
    opts_channel_id = None
    opts_msg_id = None
    reply_handle = None
    try:
        # Intercept literal commands (safety net for when it doesn't use the tool)
        _cmd_match = _LITERAL_CMD_RE.search(clean_reply)
        if _cmd_match:
            _cmd_name = _cmd_match.group(1).lower()
            _cmd_query = _cmd_match.group(2).strip()
            _action_type = "PLAY_MUSIC" if _cmd_name == "play" else "PLAY_SOUND"
            _spawn(
                _dispatch_indio_actions(
                    ctx.bot,
                    getattr(ctx.guild, "id", None),
                    [(_action_type, _cmd_query)],
                    requester_member=ctx.author,
                )
            )
            clean_reply = _LITERAL_CMD_RE.sub("", clean_reply).strip()
            if not clean_reply:
                clean_reply = "Aca tenes"

        question_header = _format_user_header(ctx, pregunta).rstrip()
        # Cuando respondemos en el canal del slash, el header edita el
        # deferred ("thinking..." o aviso de rotación). _post es el fallback
        # cuando hay override o cuando el edit falla.
        question_msg = None
        if target_channel is None:
            try:
                question_msg = await ctx.interaction.edit_original_response(
                    content=question_header
                )
            except Exception:
                logger.debug(
                    "indio: edit original response failed, falling back", exc_info=True
                )
                question_msg = None
        if question_msg is None:
            question_msg = await _post(question_header)
        question_msg_id = getattr(question_msg, "id", None)
        channel_id = _reply_channel_id()
        opts_channel_id = channel_id
        if (
            vote_open
            and config.INDIO_RELAY_URL
            and config.INDIO_RELAY_SECRET
            and channel_id is not None
        ):
            # Vote options: post via relay but capture the message id so we can
            # add the number reactions to it.
            opts_msg_id = await _relay_say(channel_id, clean_reply)
            relayed_via_userbot = opts_msg_id is not None
            n_chunks = 1 if relayed_via_userbot else 0
            if not relayed_via_userbot:
                sent = await _post(clean_reply)
                opts_msg_id = getattr(sent, "id", None)
                opts_channel_id = (
                    getattr(getattr(sent, "channel", None), "id", None) or channel_id
                )
                n_chunks = 1
        elif vote_open:
            sent = await _post(clean_reply)
            opts_msg_id = getattr(sent, "id", None)
            opts_channel_id = (
                getattr(getattr(sent, "channel", None), "id", None) or channel_id
            )
            n_chunks = 1
        elif (
            channel_id is not None
            and config.INDIO_RELAY_URL
            and config.INDIO_RELAY_SECRET
        ):
            import types as _types

            # No reply-to: el question_msg lo posteo el bot mismo, hacer reply
            # ahi queda como auto-reply (Indio respondiendose a si mismo).
            relay_ids = await _relay_to_userbot(channel_id, clean_reply, None)
            relayed_via_userbot = bool(relay_ids)
            if relayed_via_userbot:
                relay_msg_id = relay_ids[0] if relay_ids else None
                reply_handle = _types.SimpleNamespace(
                    via_relay=True,
                    channel_id=channel_id,
                    message_id=relay_msg_id,
                    message=None,
                    single=len(relay_ids) == 1,
                )
                n_chunks = 1
            else:
                chunks = _split_for_discord(clean_reply)
                sent_msg = None
                for c in chunks:
                    sent_msg = await _post(c)
                reply_handle = _types.SimpleNamespace(
                    via_relay=False,
                    channel_id=channel_id,
                    message_id=None,
                    message=sent_msg,
                    single=len(chunks) == 1,
                )
                n_chunks = len(chunks)
        else:
            # Fallback: post the reply via vapls if relay is disabled or failed.
            import types as _types

            chunks = _split_for_discord(clean_reply)
            sent_msg = None
            for c in chunks:
                sent_msg = await _post(c)
            reply_handle = _types.SimpleNamespace(
                via_relay=False,
                channel_id=channel_id,
                message_id=None,
                message=sent_msg,
                single=len(chunks) == 1,
            )
            n_chunks = len(chunks)
    except Exception as e:
        logger.exception("indio send failed")
        analytics.capture_exception(
            e, user=ctx.author, guild=ctx.guild, properties={"action": "indio_send"}
        )
        return

    if vote_open and opts_msg_id and opts_channel_id and _active_vote is not None:
        n = len(_active_vote.candidates)
        await _attach_vote_reactions(
            ctx.bot,
            _active_vote,
            opts_channel_id,
            opts_msg_id,
            n,
        )

    if pending_actions:
        _spawn(
            _dispatch_indio_actions(
                ctx.bot,
                getattr(ctx.guild, "id", None),
                pending_actions,
                reply_handle=reply_handle,
                reply_text=clean_reply,
                requester_member=ctx.author,
                attachment_urls=None,
                source_message_id=None,
            )
        )

    # Turnos que dispararon una funcion (play_music, play_sound, etc.) no se
    # guardan en memoria: son mensajes operativos, no conversacionales, y
    # contaminan el historial si persisten (feedback loop "voz → play_music").
    history_size_after = len(history_snapshot)
    if not reply.function_calls:
        _turn_ts = time.time()
        # Persisted history scrubs emojis/legacy-brackets; visible reply already
        # went to Discord above with the emojis intact.
        user_turn = {
            "role": "user",
            "parts": [
                {"text": _sanitize_for_history(tagged_message)[:_STORED_MSG_MAX_CHARS]}
            ],
            "ts": _turn_ts,
        }
        model_turn = {
            "role": "model",
            "parts": [
                {"text": _sanitize_for_history(clean_reply)[:_STORED_MSG_MAX_CHARS]}
            ],
            "ts": _turn_ts,
        }
        async with lock:
            existing = _indio_history.get(mem_key, history_snapshot)
            new_hist = list(existing) + [user_turn, model_turn]
            # Hard cap as a safety net if compression keeps failing.
            if len(new_hist) > _HISTORY_HARD_CAP:
                new_hist = new_hist[-_HISTORY_HARD_CAP:]
            _indio_history[mem_key] = new_hist
            _indio_last_seen[mem_key] = time.time()
            history_size_after = len(new_hist)
        await _persist_indio_state()

        # Background distillation when the short-term log grows past threshold.
        if history_size_after >= _HISTORY_COMPRESS_THRESHOLD:
            _spawn(_maybe_compress(mem_key))

    analytics.capture(
        "indio invoked",
        user=ctx.author,
        guild=ctx.guild,
        properties={
            "prompt_length": len(pregunta or ""),
            "response_length": len(reply.text),
            "response_chunks": n_chunks,
            "finish_reason": reply.finish_reason,
            "prompt_tokens": reply.prompt_tokens,
            "response_tokens": reply.response_tokens,
            "model": reply.model,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "history_size_before": len(history_snapshot),
            "history_size_after": history_size_after,
            "long_term_users": len(long_term_snapshot.get("users", {}) or {}),
            "nuevo": nuevo,
            "relayed_via_userbot": relayed_via_userbot,
        },
    )


async def indioFromVoice(
    bot: "discord.Bot",
    *,
    user_id: int,
    guild_id: int,
    channel_id: int,
    pregunta: str,
    speaker_name: Optional[str] = None,
    source_message_id: Optional[int] = None,
    from_voice: bool = False,
    replied_content: Optional[str] = None,
    replied_author: Optional[str] = None,
    attachment_urls: Optional[list[dict]] = None,
) -> None:
    """Trigger the indio persona from a voice transcription or text wake-word.

    Behaves like indioLogic but without an ApplicationContext: resolves the
    guild/channel directly from the bot and posts the reply via channel.send.
    Shares the same per-guild memory bucket (_indio_memory_key returns
    "guild-<id>") so voice + slash invocations build on the same history.

    ``from_voice`` exenta a la wake-word de voz del override
    ``INDIO_REPLY_CHANNEL_ID``: cuando es True, la respuesta queda en el
    ``channel_id`` provisto por el caller (típicamente el transcript channel
    del userbot) y no se dispara el flujo de redirect (header, DM, delete del
    fuente). Wake-word de texto y otros callers no-voz siguen aplicando el
    override.
    """
    pregunta = (pregunta or "").strip()
    if not pregunta:
        return
    # Override de canal: cuando INDIO_REPLY_CHANNEL_ID esta seteado, las
    # respuestas del Indio aterrizan ahi. La wake-word de voz queda exenta
    # (from_voice=True): el transcript del userbot ya cae en su canal
    # dedicado y mover la respuesta a otro lado genera ruido. Si el mensaje
    # fue un reply a otro mensaje (replied_content), la respuesta se queda
    # en el canal original y se postea como reply al invocador.
    original_channel_id = channel_id
    if config.INDIO_REPLY_CHANNEL_ID and not from_voice and replied_content is None:
        target_chan = bot.get_channel(config.INDIO_REPLY_CHANNEL_ID)
        if target_chan is not None and getattr(target_chan, "guild", None) is not None:
            channel_id = config.INDIO_REPLY_CHANNEL_ID
            guild_id = target_chan.guild.id
        else:
            logger.warning(
                "indioFromVoice: INDIO_REPLY_CHANNEL_ID=%s no resuelve a canal — "
                "caigo al canal original %s",
                config.INDIO_REPLY_CHANNEL_ID,
                channel_id,
            )
    guild = bot.get_guild(guild_id)
    if guild is None:
        logger.warning("indioFromVoice: guild %s not found", guild_id)
        return
    channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if channel is None or not hasattr(channel, "send"):
        logger.warning("indioFromVoice: channel %s not found", channel_id)
        return
    # Cuando la respuesta se redirige a otro canal, el header con @user (mas
    # abajo, antes de postear la respuesta) ya ping al user. No postear nada
    # publico en el canal original — evita spam fuera del canal target.
    # El forward al DM del user se hace mas abajo, una vez que tenemos
    # clean_reply, para que la cuenta-real (userbot) se lo mande.
    redirected = bool(original_channel_id and original_channel_id != channel_id)
    member = guild.get_member(user_id)
    # Telegram requests arrive with user_id=0. Try to resolve the Discord
    # member by speaker_name so music actions can use their voice channel
    # if they're connected — Discord in-voice takes priority over auto-pick.
    if member is None and speaker_name:
        _norm = speaker_name.strip().lower()
        member = discord.utils.find(
            lambda m: m.display_name.lower() == _norm or m.name.lower() == _norm,
            guild.members,
        )
    speaker = speaker_name or (member.display_name if member else None) or "alguien"

    _evict_stale_indio()
    mem_key = f"guild-{guild_id}"
    lock = _indio_locks.setdefault(mem_key, asyncio.Lock())
    tagged_message = f"{speaker}: {pregunta}"
    # Key the pending choice by the Discord user id (propagated from the
    # userbot), falling back to the name only when no id is available.
    _choice_identity_val = _choice_identity(user_id, speaker)

    # How the winner gets announced when the vote closes.
    async def _post_choice(text):
        relayed = False
        if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed = await _relay_to_userbot(channel_id, text, None)
        if not relayed:
            for chunk in _split_for_discord(text):
                await channel.send(chunk)

    # If a music vote is open and this message names an option, count it as a
    # vote (anyone can vote) instead of starting a fresh turn. This is the
    # voice path — treated as decisive (close_now=True) since the user just
    # spoke the choice out loud.
    if try_register_voice_vote(
        guild_id=guild_id, user_id=user_id, speaker_name=speaker, text=pregunta
    ):
        return

    async with lock:
        history_snapshot = list(_indio_history.get(mem_key, []))
        long_term_snapshot = dict(_indio_long_term.get(mem_key, {}))

    await _maybe_refresh_current_members(mem_key, guild_id)
    current_members = list(_indio_current_members.get(mem_key, []))
    lt_block = _format_long_term(long_term_snapshot, current_members)
    emoji_block = _format_guild_emojis(guild)
    player_block = _format_player_state(bot, guild_id)
    # Stable cache prefix: persona + long-term notes + emojis (change rarely
    # within a session). Player state is volatile (current track/queue) so it
    # rides in volatile_context, out of the cached system prompt.
    stable_extras = "\n\n".join(b for b in (lt_block, emoji_block) if b)
    system_instruction = INDIO_SYSTEM + (
        f"\n\n{stable_extras}" if stable_extras else ""
    )
    system_instruction = _inject_image_catalog(system_instruction)

    # ---- Context from replied-to message + image download ----
    volatile = player_block or None
    image_parts = None

    # Descarga de imágenes del mensaje actual o del mensaje al que se responde.
    # attachment_urls ya viene poblado por el caller (userbot) con las adjuntos
    # relevantes (sea del mensaje original o del reply).
    if attachment_urls:
        images = [
            u for u in attachment_urls if u.get("mime_type", "").startswith("image/")
        ][:3]
        if images:
            downloaded = []
            async with aiohttp.ClientSession() as sess:
                for img in images:
                    try:
                        async with sess.get(
                            img["url"], timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status == 200:
                                import base64

                                data = await resp.read()
                                downloaded.append(
                                    {
                                        "inlineData": {
                                            "mimeType": img["mime_type"],
                                            "data": base64.b64encode(data).decode(),
                                        }
                                    }
                                )
                    except Exception:
                        pass
            if downloaded:
                image_parts = downloaded

    if replied_content is not None and replied_author is not None:
        ctx_lines = [f"[contexto: {replied_author} dijo: {replied_content}]"]
        if attachment_urls:
            videos = [
                u
                for u in attachment_urls
                if u.get("mime_type", "").startswith("video/")
            ]
            if videos:
                ctx_lines.append("[el mensaje tiene un video que no puedo ver]")
        ctx_text = "\n".join(ctx_lines)
        volatile = f"{ctx_text}\n\n{player_block}" if player_block else ctx_text

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=_stamp_history_for_prompt(history_snapshot, time.time()),
            tools=_INDIO_TOOLS,
            volatile_context=volatile,
            image_parts=image_parts,
        )
    except geminiClient.GeminiError as e:
        # Posteamos el aviso (incluido el 429 "conseguite una key") via el
        # userbot cuando esta disponible, asi el "Indio real" es quien dice
        # que se quedo sin cupo. Fallback a channel.send con la identidad
        # del bot vapls.
        msg = _error_message(e.kind, e.status, "indio")
        try:
            relayed = False
            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                relayed = await _relay_to_userbot(channel_id, msg, None)
            if not relayed:
                await channel.send(msg)
        except Exception as e:
            logger.exception("indioFromVoice error-send failed")
            analytics.capture_exception(
                e, properties={"action": "indio_from_voice_error_send_failed"}
            )
        analytics.capture(
            "indio voice failed",
            user=member,
            guild=guild,
            properties={
                "error_kind": e.kind,
                "http_status": e.status,
                "prompt_length": len(pregunta),
            },
        )
        return
    except Exception as e:
        logger.exception("indioFromVoice unexpected error")
        try:
            await channel.send("❌ Algo se rompió. Probá de nuevo.")
        except Exception:
            pass
        analytics.capture_exception(
            e, user=member, guild=guild, properties={"action": "indio_voice_unexpected"}
        )
        return

    pending_actions = _actions_from_function_calls(reply.function_calls)
    pending_actions = _gate_play_sound_actions(pending_actions, pregunta)
    pending_actions = _gate_play_music_actions(pending_actions, pregunta)
    # Save flag BEFORE _maybe_disambiguate_music — that function consumes
    # pending_actions for single-match direct plays (returns []), which would
    # make the redirect check below miss the music action.
    _had_music = any(a in _MUSIC_ACTIONS for a, _ in pending_actions)
    clean_reply = _strip_speaker_prefix(reply.text)
    clean_reply = _ensure_reply_text(clean_reply, pending_actions)
    # Music action redirect: cuando el Indio responde con una accion de
    # musica desde wake-word de texto (sin reply contextual), redirigir
    # la respuesta textual al canal de musica designado. Asi las
    # confirmaciones ("🎵 Ahí va") y el progreso quedan en el canal de
    # reproduccion en vez del canal de texto general. La accion musical
    # ya se manda a INDIO_PLAY_CHANNEL_ID via _dispatch_indio_actions,
    # pero el texto del Indio ("🎵 Ahí va") antes caia en INDIO_REPLY_CHANNEL_ID.
    if not from_voice and replied_content is None and config.INDIO_PLAY_CHANNEL_ID:
        if _had_music:
            _play_chan = bot.get_channel(config.INDIO_PLAY_CHANNEL_ID)
            if (
                _play_chan is not None
                and getattr(_play_chan, "guild", None) is not None
            ):
                channel_id = config.INDIO_PLAY_CHANNEL_ID
                guild_id = _play_chan.guild.id
                channel = _play_chan
                redirected = bool(
                    original_channel_id and original_channel_id != channel_id
                )
    relayed_via_userbot = False
    import playCommand

    _active_vote = playCommand.get_active_vote(int(guild_id) if guild_id else 0)
    # Same gate as indioLogic: only treat this turn's reply as the options
    # surface when the live vote is the one we just opened (no message bound
    # yet). Avoids the "unrelated chat reply gets 1-5 reactions" bug.
    vote_open = _active_vote is not None and _active_vote.reaction_message_id is None
    opts_msg_id = None
    reply_handle = None
    # Id del primer mensaje que aterriza en el target — sirve como anchor
    # para el link que se mandara por DM ("te respondi en este canal <link>").
    landing_msg_id: Optional[int] = None
    # Header con la pregunta + mencion al user: solo cuando la respuesta se
    # redirige a otro canal (asi el user recibe notificacion). Vota-open no
    # quiere header arriba — la lista de opciones tiene que ir limpia para
    # que las reacciones queden en la primera linea.

    # Intercept literal commands (safety net)
    _cmd_match = _LITERAL_CMD_RE.search(clean_reply)
    if _cmd_match:
        _cmd_name = _cmd_match.group(1).lower()
        _cmd_query = _cmd_match.group(2).strip()
        _action_type = "PLAY_MUSIC" if _cmd_name == "play" else "PLAY_SOUND"
        _spawn(
            _dispatch_indio_actions(
                bot,
                guild_id,
                [(_action_type, _cmd_query)],
                requester_member=member,
            )
        )
        clean_reply = _LITERAL_CMD_RE.sub("", clean_reply).strip()
        if not clean_reply:
            clean_reply = "Aca tenes"
    if redirected and user_id and not vote_open:
        lines = (pregunta or "").splitlines() or [""]
        quoted = "\n".join(f"> {ln}" for ln in lines)
        question_header = f"<@{user_id}> preguntó:\n{quoted}"
        try:
            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                ids = await _relay_to_userbot(channel_id, question_header, None)
                if ids:
                    landing_msg_id = int(ids[0])
            if landing_msg_id is None:
                sent_header = await channel.send(question_header)
                landing_msg_id = getattr(sent_header, "id", None)
        except Exception as e:
            logger.exception("indioFromVoice: question header failed")
            analytics.capture_exception(
                e, properties={"action": "indio_from_voice_question_header_failed"}
            )
    # Anchor para Discord "reply": SOLO cuando podemos atar la respuesta al
    # mensaje del USER (wake-word original en el mismo canal). Cuando hay
    # redirect el "anchor" disponible es el header que el bot mismo postea,
    # y hacer reply ahi se ve como auto-reply (Indio respondiendose). Mejor
    # postear sin reference y dejar que el header siga visible arriba.
    reply_anchor_id = None if redirected else source_message_id

    def _make_ref(mid):
        if not mid:
            return None
        try:
            return discord.MessageReference(
                message_id=int(mid),
                channel_id=int(channel_id),
                fail_if_not_exists=False,
            )
        except Exception:
            return None

    try:
        if not clean_reply and not vote_open:
            reply_handle = None
        elif vote_open:
            # Vote options: capture the message id so we can react on it.
            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                opts_msg_id = await _relay_say(channel_id, clean_reply)
                relayed_via_userbot = opts_msg_id is not None
            if not relayed_via_userbot:
                sent = None
                for chunk in _split_for_discord(clean_reply):
                    sent = await channel.send(chunk)
                opts_msg_id = getattr(sent, "id", None)
        else:
            import types as _types

            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                relay_ids = await _relay_to_userbot(
                    channel_id, clean_reply, reply_anchor_id
                )
                relayed_via_userbot = bool(relay_ids)
                if relayed_via_userbot:
                    relay_msg_id = relay_ids[0] if relay_ids else None
                    reply_handle = _types.SimpleNamespace(
                        via_relay=True,
                        channel_id=channel_id,
                        message_id=relay_msg_id,
                        message=None,
                        single=len(relay_ids) == 1,
                    )
            if not relayed_via_userbot:
                chunks = _split_for_discord(clean_reply)
                sent_msg = None
                first_ref = _make_ref(reply_anchor_id)
                for i, chunk in enumerate(chunks):
                    kwargs = {"reference": first_ref} if (i == 0 and first_ref) else {}
                    sent_msg = await channel.send(chunk, **kwargs)
                reply_handle = _types.SimpleNamespace(
                    via_relay=False,
                    channel_id=channel_id,
                    message_id=None,
                    message=sent_msg,
                    single=len(chunks) == 1,
                )
    except Exception as e:
        logger.exception("indioFromVoice send failed")
        analytics.capture_exception(
            e, properties={"action": "indio_from_voice_send_failed"}
        )
        return

    if vote_open and opts_msg_id and _active_vote is not None:
        n = len(_active_vote.candidates)
        await _attach_vote_reactions(bot, _active_vote, channel_id, opts_msg_id, n)
        if source_message_id:
            _active_vote.source_message_id = int(source_message_id)

    if pending_actions:
        _spawn(
            _dispatch_indio_actions(
                bot,
                guild_id,
                pending_actions,
                reply_handle=reply_handle,
                reply_text=clean_reply,
                requester_member=member,
                attachment_urls=attachment_urls,
                source_message_id=source_message_id,
                from_voice=from_voice,
            )
        )

    # No guardar mensajes que activaron una funcion (play_music, etc.) ni
    # transcripciones de voz: son mensajes operativos que contaminan el historial
    # y generan feedback loop "voz → play_music".
    history_size_after = 0
    if not from_voice and not reply.function_calls:
        _turn_ts = time.time()
        user_turn = {
            "role": "user",
            "parts": [
                {"text": _sanitize_for_history(tagged_message)[:_STORED_MSG_MAX_CHARS]}
            ],
            "ts": _turn_ts,
        }
        model_turn = {
            "role": "model",
            "parts": [
                {"text": _sanitize_for_history(clean_reply)[:_STORED_MSG_MAX_CHARS]}
            ],
            "ts": _turn_ts,
        }
        async with lock:
            existing = _indio_history.get(mem_key, history_snapshot)
            new_hist = list(existing) + [user_turn, model_turn]
            if len(new_hist) > _HISTORY_HARD_CAP:
                new_hist = new_hist[-_HISTORY_HARD_CAP:]
            _indio_history[mem_key] = new_hist
            _indio_last_seen[mem_key] = time.time()
            history_size_after = len(new_hist)
        await _persist_indio_state()

        if history_size_after >= _HISTORY_COMPRESS_THRESHOLD:
            _spawn(_maybe_compress(mem_key))

    # Si la respuesta se redirigio a otro canal, fallback al primer mensaje
    # del Indio en el target como landing point del link cuando no hubo header.
    if redirected and landing_msg_id is None:
        if reply_handle is not None:
            landing_msg_id = getattr(reply_handle, "message_id", None) or getattr(
                getattr(reply_handle, "message", None), "id", None
            )
        elif opts_msg_id:
            landing_msg_id = opts_msg_id

    # Borrar el mensaje original del user en el canal source (best-effort —
    # requiere "Manage Messages" en el canal source). Mantiene el source
    # limpio de wake-words sueltos cuando la conversacion se movio.
    if redirected and source_message_id and original_channel_id:
        src_chan = bot.get_channel(int(original_channel_id))
        if src_chan is not None and hasattr(src_chan, "get_partial_message"):
            try:
                await src_chan.get_partial_message(int(source_message_id)).delete()
            except Exception:
                logger.info(
                    "indioFromVoice: could not delete source message %s "
                    "(missing Manage Messages perm?)",
                    source_message_id,
                )

    # DM al user via userbot (cuenta-real): solo el link al mensaje en el
    # target. Sin emoji custom — el :ElIndio: del server no renderiza en
    # contextos fuera del guild (DM), aparece literal y queda feo.
    if redirected and user_id:
        if landing_msg_id:
            link = (
                f"https://discord.com/channels/{guild_id}/{channel_id}/{landing_msg_id}"
            )
            dm_text = f"te respondi en este canal {link}"
        else:
            dm_text = f"te respondi en <#{channel_id}>"
        _spawn(_relay_dm_user(int(user_id), dm_text))

    analytics.capture(
        "indio voice invoked",
        user=member,
        guild=guild,
        properties={
            "prompt_length": len(pregunta),
            "response_length": len(clean_reply),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "relayed_via_userbot": relayed_via_userbot,
            "history_size_after": history_size_after,
        },
    )


_BOT_TESTING_CHANNEL_NAME = "bot-testing"


async def askIndio(
    bot: "discord.Bot",
    text: str,
    speaker_name: str = "alguien",
    *,
    guild_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    channel_name: Optional[str] = None,
    user_id: int = 0,
    source_message_id: Optional[int] = None,
    is_voice: bool = False,
    replied_content: Optional[str] = None,
    replied_author: Optional[str] = None,
    attachment_urls: Optional[list[dict]] = None,
) -> bool:
    """Reusable entry point to talk to the indio from anywhere in the code.

    Args:
        bot: The main Discord bot client.
        text: The user's message / question to the indio.
        speaker_name: The friendly name to attribute the message to inside
            the indio's memory (so he keeps track of who said what). Defaults
            to "alguien" if you don't have a user.
        guild_id: Optional guild ID; if omitted, picks the first guild that
            has the resolved channel.
        channel_id: Optional explicit channel ID. Wins over channel_name.
        channel_name: Channel name to resolve. Defaults to "bot-testing".
        user_id: Discord user id of the speaker (0 if unknown). Used to key
            pending music choices so only the requester can resolve them.

    Behavior:
        The reply is posted via the userbot relay (the cuenta-real "Indio")
        when configured, or via the bot itself as fallback. Uses the same
        per-guild memory bucket as /indio so messages from this entry point
        feed the same history and long-term memory.

    Returns:
        True if a reply was sent, False on any failure.
    """
    if not text or not text.strip():
        return False
    target_channel_id: Optional[int] = channel_id
    target_guild_id: Optional[int] = guild_id
    if target_channel_id is None:
        target_guild_id = guild_id
        if channel_name is None:
            channel_name = _BOT_TESTING_CHANNEL_NAME
        guilds = (
            [bot.get_guild(target_guild_id)] if target_guild_id else list(bot.guilds)
        )
        for guild in guilds:
            if guild is None:
                continue
            chan = discord.utils.get(
                getattr(guild, "text_channels", []) or [], name=channel_name
            )
            if chan is not None:
                target_channel_id = chan.id
                target_guild_id = guild.id
                break
    if target_channel_id is None or target_guild_id is None:
        logger.warning(
            "askIndio: could not resolve channel (guild=%s, name=%s)",
            guild_id,
            channel_name,
        )
        return False
    await indioFromVoice(
        bot,
        user_id=user_id,
        guild_id=target_guild_id,
        channel_id=target_channel_id,
        pregunta=text,
        speaker_name=speaker_name,
        source_message_id=source_message_id,
        from_voice=is_voice,
        replied_content=replied_content,
        replied_author=replied_author,
        attachment_urls=attachment_urls,
    )
    return True


# Cargar el estado persistido al final, cuando todas las funciones helpers
# (incluida _sanitize_for_history) ya estan definidas — sino la sanitizacion
# de history al startup falla con NameError.
_load_indio_state()

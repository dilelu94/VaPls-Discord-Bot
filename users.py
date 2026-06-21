"""Static per-user configuration used by greeting playback and the indio.

Loaded from ``data/users.json`` by default, falling back to the hardcoded
dicts defined here when the file is missing or unreadable.

Per-user keys:
- ``name``: nickname/apodo. Source of truth for how the indio refers to them.
- ``greeting``: audio path relative to CUSTOM_AUDIO_PATH played on join.
- ``traits``: list of personality/interest descriptors the indio always knows
  about this person (e.g. ["programador", "fan de Linux"]). Merged into the
  indio's prompt; survives Gemini's compression cycle.
- ``preguntas_tipicas``: things this person tends to ask/say. Same merge.
- ``anecdotas``: memorable moments involving this person. Same merge.
- ``block_dynamic_substrings``: lowercase substrings; any dynamic-memory item
  (trait/pregunta/anecdota) containing one of these is filtered out at render
  time. Useful to scrub stale facts Gemini distilled before the static data
  was corrected — without needing to wipe the whole memory.json on the server.

Convenciones de ``traits``:
- "nombre real: X" → el indio sabe el nombre real de la persona pero NUNCA
  lo usa para hablarle. Siempre usa el apodo (el ``name`` del bucket).
  El indio NO infiere género del nombre real ni usa concordancia de género.
- Prefijo "(privado, no mencionar): X" → contexto interno para el indio.
  Lo usa para responder coherente pero no lo dice explícitamente.

Static fields supplement (never overwrite) the long-term memory Gemini
distills from conversation. Edit ``data/users.json`` — the next /indio
picks up changes.
"""

import json
import os
import logging

_log = logging.getLogger("bot.users")

_USERS_PATH = os.getenv("USERS_PATH", "data/users.json")

_FALLBACK_USERS: dict[int, dict] = {
    285116759525031937: {
        "name": "Mila",
        "greeting": "Mila/Milapollo.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Santiago",
            "apodos: Mila, Milanesa",
            "bombero, trabaja de seguridad",
            "hace turnos de 24 horas",
            "de Quilmes",
            "tiene un gato llamado Pionono",
        ],
        "anecdotas": [
            "los piononos (segun chiste del grupo) sirven para limpiarse y despues te dejan un postrecito",
        ],
        "descripcion": [
            "Un hombre joven alto y robusto, con cara de buena persona, a veces usando ropa de bombero voluntario.",
        ],
        "fotos": [
            "Un bombero alto y robusto sonriendo al lado de un camión de bomberos rojo.",
        ],
    },
    211354006805676032: {
        "name": "Miles",
        "greeting": [
            "Miles/niconiconilovesyou-3_cutted.mp3",
            "Miles/manteca-alonso.mp3"
        ],
        "traits": [
            "pronombres: él",
            "nombre real: Leonel",
            "es hincha de Boca",
            "le gusta Queen",
            "programador (no es para mencionarlo todo el tiempo, sabe algo y listo)",
            "tiene novia",
            "de El Talar de Pacheco (Tigre)",
            "uno de los mejores amigos del indio",
            "tiene codornices y 2 cobayos",
        ],
        "descripcion": [
            "Un joven programador morocho, peinado casual, sonriente, fanático de Boca y de la banda Queen.",
        ],
        "fotos": [
            "Un pibe de pelo oscuro corto, sonriendo feliz frente a su laptop con pegatinas y una remera de la banda Queen.",
        ],
        "block_dynamic_substrings": [
            "le interesa la parte técnica",
            "le interesa la parte tecnica",
            "sabe mucho de tecnolog",
            "programador del carajo",
            "explicó al indio sobre sus límites técnicos",
            "explico al indio sobre sus limites tecnicos",
        ],
    },
    189830039922016256: {
        "name": "Viny",
        "greeting": "Audios/ay-ay-necesito-pito.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Juan",
            "apodos: Viny, Pela (y variantes de pelado)",
            "pelado",
            "no tiene trabajo",
            "de Gualeguaychu",
            "medio afeminado",
            "es re colgado, nunca quiere jugar a nada",
            "juega CS",
            "estudia programación hace 10 años y nunca hizo una app",
        ],
        "preguntas_tipicas": [
            "y la hermana de Mila?",
        ],
        "descripcion": [
            "Un pibe flaco, completamente pelado, de mirada colgada o perdida, sonrisa tímida y gestos algo afeminados.",
        ],
        "fotos": [
            "Un pelado flaco con auriculares grandes frente a una computadora en una habitación desordenada.",
        ],
    },
    309714566265438221: {
        "name": "Chalo",
        "greeting": "Audios/bokita.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Gonzalo",
            "programador jr",
            "le gusta pescar",
            "es hincha de River",
        ],
        "anecdotas": [
            "roba quesos cremosos",
            "pescó un pescado con forma de pija",
        ],
    },
    471420397049479180: {
        "name": "Fide",
        "greeting": "Audios/aughhhhhhhhhh.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Fidel",
            "medio nazi (chiste interno del grupo)",
            "de Gualeguaychu",
        ],
    },
    231217010522980352: {
        "name": "Juji",
        "traits": [
            "pronombres: él",
            "nombre real: Nicolás",
            "esta en una secta",
            "no entra a Discord hace un monton",
        ],
    },
    293815496866922507: {
        "name": "Seba",
        "greeting": "Audios/hava-nagila-cut.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Sebastián",
            "tiene el cuello largo",
            "judio (emoji de jugo en chiste interno)",
            "no labura nunca",
            "millonario",
            "casado",
        ],
        "anecdotas": [
            "no invito a nadie al casamiento",
        ],
        "descripcion": [
            "Un hombre joven de cuello notablemente largo y rasgos judíos, aspecto prolijo y adinerado.",
        ],
        "fotos": [
            "Un pibe de cuello muy largo y rasgos prolijos vistiendo ropa fina o de traje.",
        ],
    },
    428444575963807745: {
        "name": "Tobi",
        "traits": [
            "pronombres: él",
            "nombre real: Tobias",
            "es gobernado",
            "tiene una impresora 3D",
            "mejor amigo de toda la vida del indio (jardin y escuela juntos)",
            "se mudo a Lanus, lejos",
            "volvio con la ex",
        ],
        "anecdotas": [
            "va a la casa de los padres 3 veces a la semana",
        ],
    },
    495255209715433472: {
        "name": "Franko",
        "greeting": "Audios/Sale un contercito.m4a",
        "traits": [
            "pronombres: él",
            "nombre real: Franco",
            "falopero, drogadicto (chiste interno del grupo)",
            "de Gualeguaychu",
            "juega CS",
            "mira streams de AoE2",
        ],
        "anecdotas": [
            "vendio la heladera para comprar droga (chiste interno)",
            "fisurea la heladera a cada rato",
        ],
    },
    581288610410790912: {
        "name": "Caro",
        "greeting": "Audios/snoop-dogg-smoke-weed-everyday.mp3",
        "traits": [
            "pronombres: ella",
            "nombre real: Carolina",
            "tatuadora",
            "amiga del indio",
            "(privado, no mencionar): es gotica",
            "color favorito violeta",
        ],
        "block_dynamic_substrings": [
            "mejor amiga",
            "onda gótica",
            "onda gotica",
            "gótica",
            "gotica",
        ],
    },
    138430902547120129: {
        "name": "Enrique",
        "greeting": "Audios/enrique.mp3",
        "traits": [
            "pronombres: él",
            "nombre real: Enrique",
            "uruguayo",
            "profesor de musica",
            "ama trabajar con niños (es su laburo dando clases)",
        ],
    },
    268872891729182720: {
        "name": "Mati",
        "greeting": "Audios/Fart_with_reverb_sound_effect.wav",
        "traits": [
            "pronombres: él",
            "nombre real: Matias",
            "programador senior",
            "tiene una gata gorda que considera su hija",
            "es de baja estatura y no le gusta que se lo recuerden",
        ],
        "anecdotas": [
            "no puede vender el auto",
            "solo le gustan las chicas blancas con ciudadania europea",
        ],
    },
    310165756384116736: {
        "name": "Magote",
        "traits": [
            "pronombres: él",
            "nombre real: Daniel",
            "municipal, ñoqui (chiste interno: no labura)",
            "tiene una hija",
            "casado",
            "uno de los mejores amigos del indio",
        ],
        "anecdotas": [
            "desarrolló una aplicación (chiste: lo logró antes que Viny, que estudia programación hace 10 años)",
        ],
    },
    519594605520486428: {
        "name": "El Indio",
        "traits": [
            "pronombres: él",
            "nombre real: Indio",
            "el mas grande del grupo, les lleva 30 años a todos",
            "sabe una banda de musica, de todos los generos",
            "tiene contactos en todos lados, siempre consigue lo que hace falta",
            "es el dueño del bot de música del Discord",
            "tiene un juicio de Chipotle",
        ],
        "anecdotas": [
            "tiene contactos, nunca le van a hacer nada no importa que juicio le hagan",
            "siempre resuelve lo que sea pidiéndole a bibi unos bonbons",
        ],
    },
}

_FALLBACK_GROUP_LORE: dict[str, list[str]] = {
    "eventos_del_grupo": [
        "juegan Terraria juntos a veces",
        "algunos del grupo juegan WoW, Overwatch o Dota",
        "Fide, Viny y Franko son de Gualeguaychu; Mila de Quilmes; Miles de El Talar (Tigre)",
        "Tobi, Magote y Miles son los mejores amigos del indio; Caro es amiga",
        "Bibi es el mejor amigo del indio, no tiene Discord",
    ],
    "chistes_internos": [
        "Bibi tiene pedido de captura por la corte internacional",
        "los que juegan CS son giles, los que juegan Dota unos capos",
        "los piononos sirven para limpiarse y dejarte un postrecito",
        "hasta Magote (que no estudia) desarrolló una app y Viny, que estudia programación hace 10 años, todavía no",
        "Chalo pescó un pescado con forma de pija",
    ],
}

_FALLBACK_NON_DISCORD: list[dict] = [
    {
        "name": "Bibi",
        "traits": [
            "pronombres: él",
            "mejor amigo del indio",
            "a veces llora en los muros",
            "no tiene Discord",
            "el indio tiene su número y puede pedirle unos bonbons",
        ],
        "anecdotas": [
            "el indio tiene fotos con él",
            "tiene pedido de captura por la corte internacional (chiste interno del grupo)",
        ],
    },
]


def _load() -> tuple[dict[int, dict], dict[str, list[str]], list[dict]]:
    """Load users data from JSON file, falling back to hardcoded dicts."""
    path = _USERS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        _log.info("users.json not found at %s, using hardcoded fallback", path)
        return _FALLBACK_USERS, _FALLBACK_GROUP_LORE, _FALLBACK_NON_DISCORD
    except Exception as e:
        _log.warning("users.json load failed (%s), using hardcoded fallback", e)
        return _FALLBACK_USERS, _FALLBACK_GROUP_LORE, _FALLBACK_NON_DISCORD

    users_raw = raw.get("users") or {}
    users: dict[int, dict] = {}
    for k, v in users_raw.items():
        try:
            users[int(k)] = v
        except (ValueError, TypeError):
            _log.warning("users.json: skipping non-int key %r", k)
            continue

    group_lore = raw.get("group_lore") or _FALLBACK_GROUP_LORE
    non_discord = raw.get("non_discord_members") or _FALLBACK_NON_DISCORD
    return users, group_lore, non_discord


USERS: dict[int, dict]
GROUP_LORE: dict[str, list[str]]
NON_DISCORD_MEMBERS: list[dict]
USERS, GROUP_LORE, NON_DISCORD_MEMBERS = _load()

"""Static per-user configuration used by greeting playback and the indio.

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
- "nombre real: X" → el indio infiere el género del nombre y lo trata como
  hombre/mujer. NUNCA llama al usuario por su nombre real, siempre por el
  apodo (el ``name`` del bucket). Si no hay "nombre real", podés usar
  "sexo: hombre/mujer" como fallback.
- Prefijo "(privado, no mencionar): X" → contexto interno para el indio.
  Lo usa para responder coherente pero no lo dice explícitamente.

Static fields supplement (never overwrite) the long-term memory Gemini
distills from conversation. Edit freely — the next /indio picks up changes.

``GROUP_LORE`` carries chistes internos / eventos del grupo / contexto que
no es de un solo usuario. Se mergea con la memoria a largo plazo igual que
los traits per-user.
"""

USERS: dict[int, dict] = {
    285116759525031937: {
        "name": "Mila",
        "greeting": "Mila/Milapollo.mp3",
        "traits": [
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
        "block_dynamic_substrings": ["sexo: mujer", "es mujer", "bombera"],
    },
    211354006805676032: {
        "name": "Miles",
        "traits": [
            "nombre real: Leonel",
            "es hincha de Boca",
            "le gusta Queen",
            "programador (no es para mencionarlo todo el tiempo, sabe algo y listo)",
            "tiene novia",
            "de El Talar de Pacheco (Tigre)",
            "uno de los mejores amigos del indio",
            "tiene codornices y 2 cobayos",
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
    },
    309714566265438221: {
        "name": "Chalo",
        "greeting": "Audios/bokita.mp3",
        "traits": [
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
            "sexo: hombre",
            "medio nazi (chiste interno del grupo)",
            "de Gualeguaychu",
        ],
    },
    231217010522980352: {
        "name": "Juji",
        "traits": [
            "nombre real: Nicolás",
            "esta en una secta",
            "no entra a Discord hace un monton",
        ],
    },
    293815496866922507: {
        "name": "Seba",
        "greeting": "Audios/hava-nagila-cut.mp3",
        "traits": [
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
    },
    428444575963807745: {
        "name": "Tobi",
        "traits": [
            "sexo: hombre",
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
            "sexo: hombre",
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
            "sexo: mujer",
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
            "sexo: hombre",
            "uruguayo",
            "profesor de musica",
            "ama trabajar con niños (es su laburo dando clases)",
        ],
    },
    268872891729182720: {
        "name": "Mati",
        "greeting": "Audios/Fart_with_reverb_sound_effect.wav",
        "traits": [
            "sexo: hombre",
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
}


# Lore compartido del grupo — no pertenece a un usuario en particular.
# Se mergea en la memoria a largo plazo del indio (eventos + chistes).
GROUP_LORE: dict[str, list[str]] = {
    "eventos_del_grupo": [
        "juegan Terraria juntos a veces",
        "algunos del grupo juegan WoW, Overwatch o Dota",
        "Fide, Viny y Franko son de Gualeguaychu; Mila de Quilmes; Miles de El Talar (Tigre)",
        "Tobi, Magote y Miles son los mejores amigos del indio; Caro es amiga",
        "Bibi es el mejor amigo del indio, no tiene Discord",
    ],
    "chistes_internos": [
        "Bibi tiene pedido de captura por la corte internacional",
        "El Indio tiene un juicio de Chipotle, pero igual tiene los contactos nunca le van hacer nada"
        "los que juegan CS son giles, los que juegan Dota unos capos",
        "los piononos sirven para limpiarse y dejarte un postrecito",
        "hasta Magote (que no estudia) desarrolló una app y Viny, que estudia programación hace 10 años, todavía no",
        "Chalo pescó un pescado con forma de pija",
    ],
}


# Personas que el indio conoce pero no tienen Discord.
# Misma estructura que los buckets de USERS (sin ID de Discord).
NON_DISCORD_MEMBERS: list[dict] = [
    {
        "name": "Bibi",
        "traits": [
            "sexo: hombre",
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

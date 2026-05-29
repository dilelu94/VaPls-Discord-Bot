"""Static per-user configuration used by greeting playback and the indio.

Per-user keys:
- ``name``: nickname/apodo. Source of truth for how the indio refers to them.
- ``greeting``: audio path relative to CUSTOM_AUDIO_PATH played on join.
- ``traits``: list of personality/interest descriptors the indio always knows
  about this person (e.g. ["programador", "fan de Linux"]). Merged into the
  indio's prompt; survives Gemini's compression cycle.
- ``preguntas_tipicas``: things this person tends to ask/say. Same merge.
- ``anecdotas``: memorable moments involving this person. Same merge.

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
            "bombera, trabaja de seguridad",
            "hace turnos de 24 horas",
            "de Quilmes",
            "tiene un gato llamado Pionono",
        ],
        "anecdotas": [
            "los piononos (segun chiste del grupo) sirven para limpiarse y despues te dejan un postrecito",
        ],
    },
    211354006805676032: {
        "name": "Miles",
        "traits": [
            "programador del carajo (con IA)",
            "tiene novia",
            "de El Talar de Pacheco (Tigre)",
            "uno de los mejores amigos del indio",
            "tiene codornices y 2 cobayos",
        ],
    },
    189830039922016256: {
        "name": "Viny",
        "greeting": "Audios/Necesito pito.m4a",
        "traits": [
            "pelado",
            "no tiene trabajo",
            "de Gualeguaychu",
            "medio afeminado",
            "es re colgado, nunca quiere jugar a nada",
            "juega CS",
        ],
        "preguntas_tipicas": [
            "y la hermana de Mila?",
        ],
    },
    309714566265438221: {
        "name": "Chalo",
        "traits": [
            "programador jr",
            "le gusta pescar",
        ],
        "anecdotas": [
            "roba quesos cremosos",
        ],
    },
    471420397049479180: {
        "name": "Fide",
        "greeting": "Audios/aughhhhhhhhhh.mp3",
        "traits": [
            "medio nazi (chiste interno del grupo)",
            "de Gualeguaychu",
        ],
    },
    231217010522980352: {
        "name": "Juji",
        "traits": [
            "esta en una secta",
            "no entra a Discord hace un monton",
        ],
    },
    293815496866922507: {
        "name": "Seba",
        "traits": [
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
        "traits": [
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
        "traits": [
            "gotica, tatuadora",
        ],
    },
    138430902547120129: {
        "name": "Enrique",
        "greeting": "Audios/enrique.mp3",
        "traits": [
            "uruguayo",
            "profesor de musica",
            "ama trabajar con niños (es su laburo dando clases)",
        ],
    },
    268872891729182720: {
        "name": "Mati",
        "greeting": "Audios/Fart_with_reverb_sound_effect.wav",
        "traits": [
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
            "municipal, ñoqui (chiste interno: no labura)",
            "tiene una hija",
            "casado",
            "uno de los mejores amigos del indio",
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
        "Tobi, Magote y Miles son los mejores amigos del indio",
    ],
    "chistes_internos": [
        "los que juegan CS son giles, los que juegan Dota unos capos",
        "los piononos sirven para limpiarse y dejarte un postrecito",
    ],
}

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
"""
USERS: dict[int, dict] = {
    285116759525031937: {"name": "Mila", "greeting": "Mila/Milapollo.mp3"},
    211354006805676032: {"name": "Miles"},
    189830039922016256: {"name": "Viny", "greeting": "Audios/Necesito pito.m4a"},
    309714566265438221: {"name": "Chalo"},
    471420397049479180: {"name": "Fide", "greeting": "Audios/aughhhhhhhhhh.mp3"},
    231217010522980352: {"name": "Juji"},
    293815496866922507: {"name": "Seba"},
    428444575963807745: {"name": "Tobi"},
    495255209715433472: {"name": "Franko"},
    581288610410790912: {"name": "Caro"},
    138430902547120129: {"name": "Enrique", "greeting": "Audios/enrique.mp3"},
    268872891729182720: {
        "name": "Mati",
        "greeting": "Audios/Fart_with_reverb_sound_effect.wav",
        # Ejemplo de traits manuales — agregale lo que quieras a cada amigo.
        # "traits": ["programador", "fan de Linux"],
        # "preguntas_tipicas": ["cosas tecnicas"],
        # "anecdotas": ["rompio prod a las 3am una vez"],
    },
}

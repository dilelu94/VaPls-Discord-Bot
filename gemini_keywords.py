"""Central keyword registry for Indio tools.

Every verb, trigger phrase, and example that appears in INDIO_SYSTEM, tool
descriptions, resume context, or the deterministic play_sound gate lives here.
Edit this file to add/remove/rename trigger patterns — the prompts rebuild
automatically from these sources.

Naming convention:
  *VERBS         — plain verb strings for tool descriptions (formatted inline)
  *_RE_SOURCE    — raw regex pattern for deterministic gates
  SYSTEM_TRIGGERS     — full example phrases for INDIO_SYSTEM (formatted via _fmt_trigger)
  *_PHRASES      — multi-word phrases for context blocks
"""

# ── Regex source for the deterministic play_sound gate ───────────────────
# The gate classifies raw user text: verb present → commanded; clip name
# present → spontaneous; neither → dropped. Pattern uses \b word boundaries.
PLAY_ORDER_RE_SOURCE = (
    r"\b("
    r"tira(te|me|le|lo|la|nos)?|"
    r"pone(la|lo|le|me|nos|te)?|"
    r"mete(le|lo|la)?|"
    r"reproduci(lo|la|me)?|"
    r"hace(lo|la)?\s+sonar|"
    r"traete|"
    r"queremos\s+(escuchar|oir)"
    r")\b"
)

# ── Verb lists for tool descriptions ─────────────────────────────────────
# Each list enumerates the verbs that trigger a specific tool. These get
# formatted inline into the tool's "description" field.
# Keep them short (imperative forms) — Gemini sees them as the positive
# anchor set. Don't include stopwords or ambiguous filler.

PLAY_MUSIC_VERBS = [
    "poné",
    "ponete",
    "poneme",
    "metele",
    "tirá",
    "tirate",
    "reproducí",
    "dejá",
    "traete",
    "queremos escuchar",
]

PLAY_SOUND_VERBS = [
    "tirá",
    "tirate",
    "tirame",
    "pone",
    "poné",
    "ponete",
    "ponela",
    "ponelo",
    "mete",
    "metele",
    "hacé sonar",
    "hacelo sonar",
    "reproducí",
    "traete",
    "queremos escuchar",
]

SKIP_VERBS = ["saltea", "skip", "siguiente", "cambiá de tema"]

PAUSE_VERBS = ["pausá", "frená", "pará un toque"]

RESUME_VERBS = ["resumí", "continuá", "play", "pone play", "metele play"]

STOP_VERBS = ["pará", "cortala", "basta"]

DJ_VERBS = [
    "modo dj",
    "hacé de dj",
    "pinche",
    "ponga música en automático",
    "sea el dj",
    "prenda el auto dj",
]

# ── Full trigger phrases for INDIO_SYSTEM ───────────────────────────────
# These are rendered as quoted examples via _fmt_trigger. Each entry is a
# complete phrase the user might say that activates the corresponding tool.
SYSTEM_TRIGGERS: dict[str, list[str]] = {
    "play_music": [
        "poné un tema",
        "poné música",
        "ponete un tema",
        "poneme <algo>",
    ],
    "play_sound": [
        "tirá <clip>",
        "poné <clip>",
        "metele <clip>",
        "reproducí <clip>",
    ],
    "skip_music": [
        "salteá",
        "siguiente",
        "cambiá",
    ],
    "pause_music": [
        "pausá",
        "frená un toque",
    ],
    "resume_music": [
        "seguí",
        "continuá",
        "resumí",
        "play (sin artista)",
    ],
    "stop_music": [
        "pará la música",
        "cortala",
        "basta",
    ],
    "dj_mode": [
        "modo dj",
        "hacé de dj",
        "prendé el dj",
    ],
}

# ── Resume-context phrases ──────────────────────────────────────────────
# These go into _format_player_state() to tell the model how ambiguous play
# requests should route to resume_music. No "dale" — only concrete phrases.
RESUME_CONTEXT_PLAY_PHRASES = [
    "play",
    "pone play",
    "metele play",
    "continuá",
    "resumí",
    "retomá",
]

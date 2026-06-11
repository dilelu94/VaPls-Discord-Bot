"""Command flag system for routing and behavior control.

Each slash command gets a flag that determines where its output goes and how it
behaves:
  - ``music``:  Commands that produce music (/soundpad, /dj, /play).
                Invocation is deleted from the source channel and the command
                executes in ``INDIO_PLAY_CHANNEL_ID``.
  - ``text``:   Normal text commands. Responses go to ``INDIO_REPLY_CHANNEL_ID``.
  - ``response``: Special flag for the indio wake-word "reply to message" flow.
                  The indio responds only to the invoker (ephemeral) instead of
                  posting publicly.
"""

COMMAND_FLAGS: dict[str, str] = {
    "dj": "music",
    "play": "music",
    "soundpad": "music",
    "vapls": "text",
    "indio": "text",
    "parar": "text",
    "queue": "text",
    "generarimagen": "text",
    # "banana": "text",
    "sugerencias": "text",
    "sugerencias-ver": "text",
    "quit": "text",
    "entraindio": "text",
    "sensibilidad": "text",
    "huh": "text",
    "help": "text",
    "ranking": "text",
    "actividad": "text",
    "transferir": "text",
    "restart": "text",
}

VALID_FLAGS: frozenset[str] = frozenset({"music", "text", "response"})


def get_command_flag(name: str) -> str | None:
    """Return the flag for a command name, or ``None`` if unknown."""
    return COMMAND_FLAGS.get(name)


def is_music_command(name: str) -> bool:
    """True if the command is flagged as ``music``."""
    return COMMAND_FLAGS.get(name) == "music"


def is_text_command(name: str) -> bool:
    """True if the command is flagged as ``text``."""
    return COMMAND_FLAGS.get(name) == "text"


def is_response_flag(name: str) -> bool:
    """True if the command is flagged as ``response``."""
    return COMMAND_FLAGS.get(name) == "response"


def assert_all_commands_have_flags() -> list[str]:
    """Return a list of command names that have invalid or missing flags.

    Used in tests to enforce every slash command has a valid flag.
    """
    bad: list[str] = []
    for name, flag in COMMAND_FLAGS.items():
        if flag not in VALID_FLAGS:
            bad.append(f"{name}: invalid flag {flag!r}")
    return bad

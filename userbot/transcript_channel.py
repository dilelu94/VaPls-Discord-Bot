"""Resolución del canal donde el userbot publica los transcripts.

Aislado de ``userbot/bot.py`` para poder testearlo sin levantar discord.py-self
ni el resto del userbot. La regla:

- ``TRANSCRIPT_CHANNEL_ID`` gana sobre ``TRANSCRIPT_CHANNEL_NAME`` porque el
  ID sobrevive renombres del canal en Discord; el nombre rompe silencioso
  cuando alguien renombra el canal (bug visto en prod: ``.env`` tenía
  ``bot-testing`` pero el canal estaba renombrado a ``indio-cueva``, el
  wake-word disparaba la alerta sonora pero el dispatch a ``/indio`` nunca
  ocurría porque ``posted_channel_id`` quedaba ``None``).
- Cuando una config está seteada y no resuelve, se loggea un warning para
  que el silent-fail aparezca en logs.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_transcript_channel(client: Any, cfg: Any) -> Optional[Any]:
    """Resuelve el canal de transcripts del userbot a partir de la config.

    Args:
        client: Cliente discord.py con ``get_channel(int)`` y ``guilds``.
        cfg: Objeto/módulo con ``TRANSCRIPT_CHANNEL_ID`` (int, 0 = no set)
            y/o ``TRANSCRIPT_CHANNEL_NAME`` (str, vacío = no set).

    Returns:
        El channel object (con ``.send``) o ``None`` si ninguna config
        resuelve a un canal enviable.
    """
    channel_id = getattr(cfg, "TRANSCRIPT_CHANNEL_ID", 0) or 0
    if channel_id:
        chan = client.get_channel(int(channel_id))
        if chan is not None and hasattr(chan, "send"):
            return chan
        logger.warning(
            "TRANSCRIPT_CHANNEL_ID=%s no resuelve a un canal enviable — "
            "intento fallback por TRANSCRIPT_CHANNEL_NAME.", channel_id,
        )
    name = getattr(cfg, "TRANSCRIPT_CHANNEL_NAME", "") or ""
    if name:
        for guild in getattr(client, "guilds", []) or []:
            for ch in getattr(guild, "text_channels", []) or []:
                if getattr(ch, "name", None) == name:
                    return ch
        logger.warning(
            "TRANSCRIPT_CHANNEL_NAME=%r no existe en ningún guild — el "
            "canal puede haber sido renombrado. Configurá "
            "TRANSCRIPT_CHANNEL_ID en .env para evitar este silent-fail.",
            name,
        )
    return None

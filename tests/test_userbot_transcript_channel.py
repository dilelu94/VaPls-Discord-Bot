"""Tests del helper que resuelve el canal de transcripts del userbot.

Pinea la regla operativa:

- El ID gana sobre el nombre (siempre que el ID resuelva). El nombre se
  rompe cuando alguien renombra el canal en Discord — fue el bug en prod
  donde el ``.env`` tenía ``TRANSCRIPT_CHANNEL_NAME=bot-testing`` pero el
  canal estaba renombrado a ``indio-cueva``: wake-word disparaba la alerta
  sonora pero el dispatch a ``/indio`` no ocurría porque ``posted_channel_id``
  quedaba ``None`` y no había forma de saber por qué desde los logs sin
  cazar el caso.
- Cuando se configura solo nombre y ese nombre no resuelve, el helper
  loggea un warning explícito recomendando setear ``TRANSCRIPT_CHANNEL_ID``
  — protege contra una regresión del silent-fail original.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_module():
    """Carga ``userbot/transcript_channel.py`` sin tocar sys.path global.

    El módulo no depende de discord ni de nada del userbot, así que es un
    import simple — pero usamos ``importlib`` para mantenerlo aislado del
    bot principal (mismo patrón que test_userbot_greeting)."""
    spec = importlib.util.spec_from_file_location(
        "_test_transcript_channel", _USERBOT_DIR / "transcript_channel.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load_module()


def _fake_channel(channel_id: int, name: str, guild=None):
    """Channel con ``.send``, id, name y .guild (lo que el helper consulta)."""
    ch = MagicMock(name=f"Channel({channel_id},{name!r})")
    ch.id = channel_id
    ch.name = name
    ch.send = MagicMock()
    ch.guild = guild
    return ch


def _fake_guild(text_channels):
    g = MagicMock(name="Guild")
    g.text_channels = list(text_channels)
    return g


def _cfg(*, channel_id=0, channel_name=""):
    return SimpleNamespace(
        TRANSCRIPT_CHANNEL_ID=channel_id,
        TRANSCRIPT_CHANNEL_NAME=channel_name,
    )


def _client(channel_by_id=None, guilds=()):
    """Fake discord client. ``channel_by_id`` mapea int -> channel
    (lo que ``client.get_channel(id)`` devolvería)."""
    client = MagicMock(name="DiscordClient")
    by_id = dict(channel_by_id or {})
    client.get_channel = MagicMock(side_effect=lambda cid: by_id.get(int(cid)))
    client.guilds = list(guilds)
    return client


# ---------------------------------------------------------------------------
# ID path
# ---------------------------------------------------------------------------


def test_id_resolves_returns_channel():
    target = _fake_channel(1490008278275461280, "indio-cueva")
    client = _client(channel_by_id={1490008278275461280: target})
    cfg = _cfg(channel_id=1490008278275461280)

    assert tc.resolve_transcript_channel(client, cfg) is target


def test_id_wins_over_name_when_both_resolve():
    """Cuando los dos están seteados y ambos resuelven a canales distintos,
    el ID gana. Justifica el orden documentado en el módulo."""
    by_id_chan = _fake_channel(111, "actual-name")
    by_name_chan = _fake_channel(222, "old-name")
    guild = _fake_guild([by_name_chan])
    client = _client(channel_by_id={111: by_id_chan}, guilds=[guild])
    cfg = _cfg(channel_id=111, channel_name="old-name")

    assert tc.resolve_transcript_channel(client, cfg) is by_id_chan


def test_id_set_but_not_resolved_falls_back_to_name(caplog):
    """Si el ID está configurado pero ``get_channel`` no lo encuentra
    (canal borrado, bot no lo cachea), el helper cae al fallback por
    nombre — pero loggea un warning para que aparezca en logs."""
    by_name_chan = _fake_channel(222, "indio-cueva")
    guild = _fake_guild([by_name_chan])
    client = _client(channel_by_id={}, guilds=[guild])
    cfg = _cfg(channel_id=999, channel_name="indio-cueva")

    with caplog.at_level(logging.WARNING, logger=tc.logger.name):
        result = tc.resolve_transcript_channel(client, cfg)

    assert result is by_name_chan
    assert any("TRANSCRIPT_CHANNEL_ID=999" in r.message for r in caplog.records)


def test_id_resolves_to_object_without_send_is_rejected():
    """``client.get_channel`` puede devolver un VoiceChannel u otro tipo
    que no tiene ``.send``. El helper no debe devolverlo: el caller hace
    ``chan.send(...)`` y crashearía. Cae al fallback por nombre."""
    voice_chan = MagicMock(spec=["id", "name", "guild"])  # no .send
    voice_chan.id = 999
    voice_chan.name = "voice"
    by_name_chan = _fake_channel(222, "indio-cueva")
    guild = _fake_guild([by_name_chan])
    client = _client(
        channel_by_id={999: voice_chan},
        guilds=[guild],
    )
    cfg = _cfg(channel_id=999, channel_name="indio-cueva")

    assert tc.resolve_transcript_channel(client, cfg) is by_name_chan


# ---------------------------------------------------------------------------
# Name path
# ---------------------------------------------------------------------------


def test_name_only_resolves_returns_channel():
    target = _fake_channel(222, "bot-testing")
    guild = _fake_guild([_fake_channel(111, "general"), target])
    client = _client(guilds=[guild])
    cfg = _cfg(channel_name="bot-testing")

    assert tc.resolve_transcript_channel(client, cfg) is target


def test_name_scans_all_guilds_until_match():
    target = _fake_channel(333, "indio-cueva")
    guild_a = _fake_guild([_fake_channel(111, "general")])
    guild_b = _fake_guild([target])
    client = _client(guilds=[guild_a, guild_b])
    cfg = _cfg(channel_name="indio-cueva")

    assert tc.resolve_transcript_channel(client, cfg) is target


def test_name_set_but_not_resolved_returns_none_and_warns(caplog):
    """**Anti-regresión del bug original**: si solo se configura nombre y
    el canal fue renombrado (no aparece en ningún guild), el helper
    retorna None Y emite un warning explícito recomendando configurar
    ``TRANSCRIPT_CHANNEL_ID``. Antes del fix esto era un silent fail:
    posted_channel_id quedaba None, _dispatch_to_indio nunca corría, y
    desde los logs era imposible saber por qué."""
    guild = _fake_guild([_fake_channel(111, "general")])
    client = _client(guilds=[guild])
    cfg = _cfg(channel_name="bot-testing")

    with caplog.at_level(logging.WARNING, logger=tc.logger.name):
        result = tc.resolve_transcript_channel(client, cfg)

    assert result is None
    assert any(
        "'bot-testing'" in r.message and "TRANSCRIPT_CHANNEL_ID" in r.message
        for r in caplog.records
    ), (
        "el helper debe loggear un warning que mencione el nombre buscado "
        f"y recomendar TRANSCRIPT_CHANNEL_ID — caplog: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Empty config / no-op paths
# ---------------------------------------------------------------------------


def test_nothing_configured_returns_none_silently(caplog):
    """ID=0 y NAME="" significa "no posting" intencional. No debe loggear
    warning (no hay nada que arreglar)."""
    client = _client(guilds=[_fake_guild([])])
    cfg = _cfg()

    with caplog.at_level(logging.WARNING, logger=tc.logger.name):
        result = tc.resolve_transcript_channel(client, cfg)

    assert result is None
    assert caplog.records == [], (
        f"no warning expected when nothing is configured, got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_id_zero_is_treated_as_not_configured():
    """ID=0 (default cuando la env var falta) significa "no usar ID".
    Debe ir directo al fallback por nombre sin warning."""
    target = _fake_channel(222, "indio-cueva")
    guild = _fake_guild([target])
    client = _client(guilds=[guild])
    cfg = _cfg(channel_id=0, channel_name="indio-cueva")

    assert tc.resolve_transcript_channel(client, cfg) is target
    # get_channel no debe ser invocado cuando el ID es 0
    client.get_channel.assert_not_called()


def test_client_with_no_guilds_returns_none():
    """Edge case: el client todavía no cargó guilds (race al startup)."""
    client = _client(guilds=[])
    cfg = _cfg(channel_name="indio-cueva")

    assert tc.resolve_transcript_channel(client, cfg) is None

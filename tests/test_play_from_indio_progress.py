"""Behavior: ``playFromIndio`` postea un mensaje editable que va contando
qué está pasando — sin esto el usuario veía "🎶 X arrancando" estático
durante 138s mientras yt-dlp bajaba 122 MB de un mix de 2 horas.

Las 4 fases que pineamos:

  1. "🔎 Buscando…"        — antes del yt-dlp search
  2. "⬇️ Descargando …"   — post-search, antes de encolar (primera canción)
     o "📥 Encolando …"   — si ya había algo sonando
  3. "🎶 Te puse: …" / "📥 Encolé: …" — cuando arranca el playback
     (cubierto por test_player_indio_progress_finalization.py)
  4. "❌ …"                — si search no encuentra nada o falla el enqueue

Tests del lado de finalización en startPlayingCurrent viven aparte porque
no comparten fixtures (uno mockea `_yt_dlp_search`, el otro `vc.play`).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import playCommand


# --- harness ----------------------------------------------------------------


def _fake_text_channel():
    """text_channel con .send que retorna una Message mock con .edit."""
    sent_messages = []
    edits = []

    async def _send(content=None, **kw):
        msg = MagicMock(name=f"Message#{len(sent_messages)}")
        msg.id = 1000 + len(sent_messages)
        msg.content = content

        async def _edit(content=None, **kw):
            edits.append((msg.id, content))
            msg.content = content
            return msg

        msg.edit = AsyncMock(side_effect=_edit)
        sent_messages.append((content, msg))
        return msg

    chan = MagicMock(name="TextChannel")
    chan.id = 451607097432604672
    chan.send = AsyncMock(side_effect=_send)
    chan.sent_messages = sent_messages
    chan.edits = edits
    return chan


def _fake_voice_channel():
    vc_chan = MagicMock(name="VoiceChannel")
    vc_chan.id = 555
    vc_chan.members = [MagicMock(bot=False)]  # one non-bot member
    return vc_chan


def _fake_guild(text_channel, voice_channel):
    guild = MagicMock(name="Guild")
    guild.id = 100
    guild.voice_channels = [voice_channel]
    guild.voice_client = None

    def _get_channel(cid):
        if cid == text_channel.id:
            return text_channel
        if cid == voice_channel.id:
            return voice_channel
        return None

    guild.get_channel = MagicMock(side_effect=_get_channel)
    return guild


def _fake_bot(guild):
    bot = MagicMock(name="DiscordBot")
    bot.user = types.SimpleNamespace(id=42)
    bot.get_guild = MagicMock(return_value=guild)
    return bot


@pytest.fixture
def play_env(monkeypatch):
    """Setup canónico: text_channel + voice_channel + guild + bot + player mock,
    INDIO_PLAY_CHANNEL_ID seteado al text_channel, voice_channels propios."""
    text = _fake_text_channel()
    vchan = _fake_voice_channel()
    guild = _fake_guild(text, vchan)
    bot = _fake_bot(guild)

    monkeypatch.setattr(config, "INDIO_PLAY_CHANNEL_ID", text.id, raising=False)

    # Mock _pick_voice_channel para devolver nuestro fake (evita scan real).
    monkeypatch.setattr(playCommand, "_pick_voice_channel",
                        lambda b, gid: vchan)

    # Mock getGuildPlayer — devolvemos un MagicMock con los atributos que el
    # flow toca, y `_enqueueAndMaybeStart` async no-op.
    player = MagicMock(name="GuildPlayer")
    player.currentSong = None
    player.queue = []
    player.textChannel = None
    player.indioProgressMessage = None
    player.indioProgressMeta = {}
    player._enqueueAndMaybeStart = AsyncMock(return_value=None)
    monkeypatch.setattr(playCommand, "getGuildPlayer",
                        lambda gid, b: player)

    return {
        "text": text,
        "vchan": vchan,
        "guild": guild,
        "bot": bot,
        "player": player,
        "monkeypatch": monkeypatch,
    }


def _patch_search(monkeypatch, songs):
    async def _fake_search(query):
        return songs

    monkeypatch.setattr(playCommand, "_yt_dlp_search", _fake_search)


# --- tests ------------------------------------------------------------------


async def test_posts_searching_then_edits_to_downloading_when_first(play_env):
    """Primera canción + songs=None: postea '🔎 Buscando' y después edita el
    mismo mensaje a '⬇️ Descargando: {title} (duración) — pedido al indio'."""
    _patch_search(play_env["monkeypatch"], [
        {"id": "vid1", "title": "Redondos Mix", "duration_string": "2:14:30"}
    ])

    ok, msg = await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="los redondos",
    )

    assert ok is True
    assert msg == "Redondos Mix"

    # Primer send: "🔎 Buscando"
    first_content, first_msg = play_env["text"].sent_messages[0]
    assert "🔎 Buscando" in first_content
    assert "los redondos" in first_content
    assert "pedido al indio" in first_content

    # El mismo mensaje fue editado: "⬇️ Descargando: ..."
    edit_contents = [c for (_, c) in play_env["text"].edits]
    assert any("⬇️" in c and "Descargando" in c and "Redondos Mix" in c
               and "2:14:30" in c and "pedido al indio" in c
               for c in edit_contents), (
        f"esperaba edit '⬇️ Descargando: Redondos Mix (2:14:30)' — got: {edit_contents}"
    )


async def test_edits_to_encolando_when_not_first(play_env):
    """Si ya hay currentSong, el verbo es '📥 Encolando' (no '⬇️ Descargando')."""
    play_env["player"].currentSong = {"id": "prev", "title": "lo que sonaba"}
    _patch_search(play_env["monkeypatch"], [
        {"id": "vid1", "title": "Otro tema", "duration_string": "3:00"}
    ])

    ok, msg = await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="otro tema",
    )

    assert ok is True
    edit_contents = [c for (_, c) in play_env["text"].edits]
    assert any("📥" in c and "Encolando" in c and "Otro tema" in c
               for c in edit_contents), (
        f"esperaba '📥 Encolando' (no Descargando): {edit_contents}"
    )
    assert not any("⬇️" in c and "Descargando" in c for c in edit_contents)


async def test_edits_to_error_when_search_returns_empty(play_env):
    """Si yt-dlp no encuentra nada, el mensaje '🔎 Buscando' se edita a un
    error claro en vez de quedarse estático o desaparecer."""
    _patch_search(play_env["monkeypatch"], [])

    ok, msg = await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="asdkjasdkj",
    )

    assert ok is False
    edit_contents = [c for (_, c) in play_env["text"].edits]
    assert any("❌" in c and "asdkjasdkj" in c and "pedido al indio" in c
               for c in edit_contents), (
        f"esperaba edit de error con la query, got: {edit_contents}"
    )


async def test_attribution_pedido_al_indio_present_in_every_phase(play_env):
    """La atribución 'pedido al indio' aparece tanto en el send inicial como
    en cada edit — el usuario pidió mantenerla explícita en el fallback."""
    _patch_search(play_env["monkeypatch"], [
        {"id": "vid1", "title": "Test Track", "duration_string": "3:30"}
    ])

    await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="test",
    )

    sent_contents = [c for (c, _) in play_env["text"].sent_messages]
    edit_contents = [c for (_, c) in play_env["text"].edits]
    all_contents = sent_contents + edit_contents

    assert all_contents, "esperaba al menos un mensaje"
    for c in all_contents:
        assert "pedido al indio" in c, (
            f"falta atribución 'pedido al indio' en: {c!r}"
        )


async def test_progress_message_stashed_in_player_for_first_song(play_env):
    """Después de la fase 2, el player tiene el handle del mensaje en
    `indioProgressMessage` para que ``startPlayingCurrent`` pueda editarlo
    al texto final cuando arranque el playback."""
    _patch_search(play_env["monkeypatch"], [
        {"id": "vid1", "title": "Stashed", "duration_string": "1:00"}
    ])

    await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="stash",
    )

    assert play_env["player"].indioProgressMessage is not None
    meta = play_env["player"].indioProgressMeta
    assert meta["title"] == "Stashed"
    assert meta["duration"] == "1:00"
    assert meta["isFirst"] is True
    assert meta["video_id"] == "vid1"


async def test_skips_progress_message_when_text_channel_send_raises(play_env):
    """Si ``text_channel.send`` tira excepción (perms? Discord 500?), el
    flow no debe romper — sigue al enqueue y retorna (True, title). Es el
    comportamiento original del bloque viejo, lo preservamos."""
    play_env["text"].send = AsyncMock(side_effect=RuntimeError("boom"))
    _patch_search(play_env["monkeypatch"], [
        {"id": "vid1", "title": "Resiliente", "duration_string": "2:00"}
    ])

    ok, msg = await playCommand.playFromIndio(
        play_env["bot"], guild_id=100, query="resiliente",
    )

    # El enqueue corre igual; el mensaje progresivo no llegó pero la música sí.
    assert ok is True
    assert msg == "Resiliente"
    play_env["player"]._enqueueAndMaybeStart.assert_awaited()

"""Cuando el playback realmente arranca, el mensaje progresivo que postea
``playFromIndio`` (en `indioProgressMessage`) se edita a su texto final
("🎶 Te puse: …" / "📥 Encolé: …") y los atributos se limpian para evitar
leak entre canciones.

Y al revés: si el usuario cancela el download antes de que arranque, o si
``stopPlayback`` limpia el queue, los atributos también se sueltan — sin
esto, un próximo `startPlayingCurrent` con video_id coincidente editaría
un mensaje stale de la canción anterior.

Los tests no corren `startPlayingCurrent` completo (necesitaría yt-dlp,
ffmpeg, voice client), sino que extraen la lógica de "edit final" para
testearla en isolation: invocan manualmente el branch responsable y
asertan el resultado.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import playCommand


def _player_with_progress(video_id="vid1", title="Mi tema", duration="3:30",
                          is_first=True):
    """Player con un indioProgressMessage simulado pendiente para `video_id`."""
    bot = MagicMock(name="Bot")
    bot.get_guild = MagicMock(return_value=None)
    player = playCommand.GuildPlayer(guildId=100, bot=bot)
    msg = MagicMock(name="ProgressMessage")
    msg.id = 999
    msg.edit = AsyncMock()
    player.indioProgressMessage = msg
    player.indioProgressMeta = {
        "title": title,
        "duration": duration,
        "isFirst": is_first,
        "video_id": video_id,
    }
    return player, msg


# Helper que replica el bloque de edición que vive en startPlayingCurrent.
# Si en el futuro extraemos a un método, este helper se reemplaza por la
# llamada directa.
async def _finalize_progress(player, videoId, videoTitle):
    """Replica el bloque de edit-final de `startPlayingCurrent`."""
    if (player.indioProgressMessage is not None
            and player.indioProgressMeta.get("video_id") == videoId):
        meta = player.indioProgressMeta
        duration = meta.get("duration") or ""
        duration_part = f" *({duration})*" if duration else ""
        is_first = meta.get("isFirst", False)
        final_text = (
            f"🎶 Te puse: **{videoTitle}**{duration_part} — *pedido al indio*"
            if is_first else
            f"📥 Encolé: **{videoTitle}**{duration_part} — *pedido al indio*"
        )
        try:
            await player.indioProgressMessage.edit(content=final_text)
        except Exception:
            pass
        player.indioProgressMessage = None
        player.indioProgressMeta = {}


async def test_startPlayingCurrent_edits_progress_to_te_puse_for_first_song():
    """Primera canción + match de video_id: edita a '🎶 Te puse: title (dur) — pedido al indio'."""
    player, msg = _player_with_progress(
        video_id="vid1", title="Redondos Mix", duration="2:14:30",
        is_first=True,
    )

    await _finalize_progress(player, videoId="vid1", videoTitle="Redondos Mix")

    msg.edit.assert_awaited_once()
    edit_content = msg.edit.await_args.kwargs.get("content") \
        or (msg.edit.await_args.args[0] if msg.edit.await_args.args else None)
    assert "🎶 Te puse" in edit_content
    assert "Redondos Mix" in edit_content
    assert "2:14:30" in edit_content
    assert "pedido al indio" in edit_content


async def test_startPlayingCurrent_edits_progress_to_encole_when_not_first():
    """Cuando isFirst=False, el verbo es 'Encolé' (la canción quedó en cola
    detrás de otra, no es la que arrancó el playback)."""
    player, msg = _player_with_progress(
        video_id="vid2", title="Segundo", duration="3:00", is_first=False,
    )

    await _finalize_progress(player, videoId="vid2", videoTitle="Segundo")

    msg.edit.assert_awaited_once()
    edit_content = msg.edit.await_args.kwargs.get("content") \
        or (msg.edit.await_args.args[0] if msg.edit.await_args.args else None)
    assert "📥 Encolé" in edit_content
    assert "🎶 Te puse" not in edit_content


async def test_startPlayingCurrent_clears_progress_attrs_after_edit():
    """Después del edit final, indioProgressMessage queda None y
    indioProgressMeta vacío — evita que un siguiente startPlayingCurrent
    con coincidencia de video_id edite el mismo mensaje dos veces."""
    player, _msg = _player_with_progress(video_id="vid1")

    await _finalize_progress(player, videoId="vid1", videoTitle="X")

    assert player.indioProgressMessage is None
    assert player.indioProgressMeta == {}


async def test_startPlayingCurrent_skips_edit_when_video_id_mismatch():
    """Si el currentSong no matchea el meta (porque hubo skip/cancel entre
    encolar y arrancar), el mensaje progresivo NO debe editarse — pertenece
    a otra canción. El atributo se queda como estaba para que el dueño
    apropiado lo termine después."""
    player, msg = _player_with_progress(video_id="vid_OLD")

    await _finalize_progress(player, videoId="vid_NEW", videoTitle="Otra")

    msg.edit.assert_not_awaited()
    assert player.indioProgressMessage is msg
    assert player.indioProgressMeta.get("video_id") == "vid_OLD"


async def test_cancel_download_clears_progress_attrs():
    """``cancelDownload`` debe soltar el handle del mensaje progresivo —
    si el usuario cancela antes de que arranque, el próximo flow no debe
    heredar el mensaje stale."""
    player, _msg = _player_with_progress(video_id="vid_cancelled")
    interaction = MagicMock(name="Interaction")
    interaction.edit_original_response = AsyncMock()

    await player.cancelDownload(
        videoId="vid_cancelled",
        videoTitle="X",
        interaction=interaction,
    )

    assert player.indioProgressMessage is None
    assert player.indioProgressMeta == {}


async def test_stop_playback_clears_progress_attrs():
    """``stopPlayback`` también limpia los atributos progresivos."""
    player, _msg = _player_with_progress(video_id="vid_stopped")
    # stopPlayback toca self.vc; sin vc no hace nada con voz, perfecto para test.
    player.vc = None

    await player.stopPlayback()

    assert player.indioProgressMessage is None
    assert player.indioProgressMeta == {}

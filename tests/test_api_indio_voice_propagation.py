"""Behavioral test del endpoint HTTP POST /indio.

Pinea la propagación del flag ``is_voice`` desde el body del request hasta
``askIndio`` (y de ahí hasta ``indioFromVoice`` como ``from_voice``). Es la
columna vertebral del fix de canal: cuando la wake-word es de voz, el
override ``INDIO_REPLY_CHANNEL_ID`` debe saltearse — pero solo si el flag
viaja correctamente a través del HTTP boundary.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import geminiCommand
from apiServer import makeApp


API_SECRET = "test-secret"
HEADERS = {"X-API-Secret": API_SECRET}


@pytest.fixture(autouse=True)
def _api_secret(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET", API_SECRET, raising=False)


async def _client_for_bot(bot) -> TestClient:
    app = makeApp(bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _drain_tasks():
    """indioVoice spawnea un asyncio.create_task — esperar a que corra."""
    current = asyncio.current_task()
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _post_indio(monkeypatch, body):
    """POST /indio con un mock de askIndio para capturar lo que recibe."""
    captured = {}

    async def _fake_ask_indio(bot, text, **kwargs):
        captured["bot"] = bot
        captured["text"] = text
        captured.update(kwargs)
        return True

    monkeypatch.setattr(geminiCommand, "askIndio", _fake_ask_indio)
    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.post("/indio", json=body, headers=HEADERS)
        await _drain_tasks()
        return resp, captured
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# is_voice propagation
# ---------------------------------------------------------------------------


async def test_voice_wake_word_propagates_is_voice_true(monkeypatch):
    """Wake-word de voz (userbot dispatch, is_voice=True): el endpoint
    forwardea ``is_voice=True`` a ``askIndio``, que a su vez lo manda como
    ``from_voice=True`` a ``indioFromVoice`` para saltear el override."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "che indio que onda",
        "speaker_name": "Tobi",
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
        "is_voice": True,
    })
    assert resp.status == 200
    assert captured.get("is_voice") is True, (
        f"is_voice no llegó a askIndio: {captured!r}"
    )
    assert captured["channel_id"] == 555
    assert captured["guild_id"] == 100
    assert captured["user_id"] == 42


async def test_text_wake_word_propagates_is_voice_false(monkeypatch):
    """Wake-word de texto (alguien escribe 'che indio' en un canal): el
    userbot dispatch incluye ``is_voice=False``, el endpoint debe
    propagarlo así para que ``indioFromVoice`` SÍ aplique el override."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "che indio que onda",
        "speaker_name": "Tobi",
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
        "is_voice": False,
    })
    assert resp.status == 200
    assert captured.get("is_voice") is False, (
        f"is_voice debe llegar como False: {captured!r}"
    )


async def test_is_voice_defaults_to_true_when_absent(monkeypatch):
    """Back-compat: el callee viejo no manda el flag. El endpoint asume
    voz (es el caso histórico — el userbot solo dispatcheaba desde wake
    word de voz). Sin este default, una versión vieja del userbot
    aterrizaría en el path equivocado."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "hola",
        "speaker_name": "Tobi",
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
    })
    assert resp.status == 200
    assert captured.get("is_voice") is True


async def test_decifrar_alias_maps_to_is_voice(monkeypatch):
    """``decifrar`` es el nombre histórico del flag; el endpoint lo acepta
    como alias para no romper userbots viejos en el medio del rollout."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "hola",
        "speaker_name": "Tobi",
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
        "decifrar": False,
    })
    assert resp.status == 200
    assert captured.get("is_voice") is False, (
        "decifrar=False debe terminar como is_voice=False en askIndio"
    )


# ---------------------------------------------------------------------------
# [voz] prefix coupling
# ---------------------------------------------------------------------------


async def test_voice_path_adds_voz_prefix_to_text(monkeypatch):
    """Cuando is_voice=True, el endpoint prefija el texto con '[voz] '
    antes de pasarlo a askIndio (señal al prompt del Indio para que tolere
    errores de ASR). Es el otro flag que viaja en la misma decisión que
    is_voice; los pineamos juntos para que no se desincronicen."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "che indio que onda",
        "is_voice": True,
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
    })
    assert resp.status == 200
    assert captured["text"].startswith("[voz] "), (
        f"esperaba prefix [voz] en texto de voz, got: {captured['text']!r}"
    )


async def test_text_path_does_not_add_voz_prefix(monkeypatch):
    """Mismo acople en el caso contrario: texto NO debe llevar prefix."""
    resp, captured = await _post_indio(monkeypatch, {
        "pregunta": "che indio que onda",
        "is_voice": False,
        "guild_id": "100",
        "channel_id": "555",
        "user_id": "42",
    })
    assert resp.status == 200
    assert not captured["text"].startswith("[voz] "), (
        f"texto de texto NO debe llevar prefix [voz]: {captured['text']!r}"
    )
    assert captured["text"] == "che indio que onda"

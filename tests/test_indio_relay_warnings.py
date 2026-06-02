"""Anti-regresión: cuando `_invoke_slash_via_userbot` cae al fallback por
config faltante, el operador tiene que verlo en logs.

En prod vimos el flujo:

  1. Indio decide PLAY_MUSIC: "redondos"
  2. `_invoke_slash_via_userbot("invoke_play", channel_id=0, query=...)`
  3. Retorna `(False, "play channel not configured")` SIN logguear nada
  4. `_dispatch_indio_actions` cae al fallback `playFromIndio`
  5. En #sick-tunes aparece "🎶 X arrancando (pedido al indio)" — el path B
  6. Pero no había forma de saber POR QUÉ no se usó el path A (relay)

Este test pinea que ambos early-exits del helper loggean un warning con la
razón concreta (relay disabled por URL/SECRET vs INDIO_PLAY_CHANNEL_ID=0).
"""
from __future__ import annotations

import logging

import pytest

import config
import geminiCommand as gc


async def test_invoke_slash_via_userbot_logs_warning_when_relay_not_configured(
        monkeypatch, caplog):
    """Sin INDIO_RELAY_URL/SECRET, el helper retorna False pero ANTES logguea
    un warning que menciona la razón ("INDIO_RELAY_URL/SECRET missing")."""
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    with caplog.at_level(logging.WARNING, logger=gc.logger.name):
        ok, msg = await gc._invoke_slash_via_userbot(
            "invoke_play", channel_id=451607097432604672, query="x",
        )

    assert ok is False
    assert msg == "relay not configured"
    assert any(
        "relay disabled" in r.message and "INDIO_RELAY_URL" in r.message
        for r in caplog.records
    ), (
        "esperaba warning con 'relay disabled' + 'INDIO_RELAY_URL', "
        f"caplog: {[r.message for r in caplog.records]}"
    )


async def test_invoke_slash_via_userbot_logs_warning_when_channel_id_zero(
        monkeypatch, caplog):
    """Con URL/SECRET seteados pero channel_id=0 (INDIO_PLAY_CHANNEL_ID unset),
    también debe loggear warning con la razón concreta — fue el bug que vimos
    en prod cuando el .env no tenía INDIO_PLAY_CHANNEL_ID."""
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://localhost:8081", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "abc", raising=False)

    with caplog.at_level(logging.WARNING, logger=gc.logger.name):
        ok, msg = await gc._invoke_slash_via_userbot(
            "invoke_play", channel_id=0, query="redondos",
        )

    assert ok is False
    assert msg == "play channel not configured"
    assert any(
        "relay disabled" in r.message and "INDIO_PLAY_CHANNEL_ID" in r.message
        for r in caplog.records
    ), (
        "esperaba warning con 'relay disabled' + 'INDIO_PLAY_CHANNEL_ID', "
        f"caplog: {[r.message for r in caplog.records]}"
    )


async def test_invoke_slash_via_userbot_does_not_warn_when_relay_path_taken(
        monkeypatch, caplog):
    """Con config completa Y channel_id > 0, no debe loggear warning previo —
    el flujo va por el path normal. El warning del except (HTTP/timeout) se
    cubre por separado en el path de excepciones."""
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://localhost:8081", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "abc", raising=False)

    async def _fake_post(*a, **kw):
        class _R:
            status = 200
            async def text(self):
                return ""
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return None
        return _R()

    # Mock aiohttp.ClientSession to avoid actual HTTP
    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

        def post(self, *a, **kw):
            class _Resp:
                status = 200
                async def text(self):
                    return ""
                async def __aenter__(self_inner):
                    return self_inner
                async def __aexit__(self_inner, *a):
                    return None
            return _Resp()

    monkeypatch.setattr(gc.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession())

    with caplog.at_level(logging.WARNING, logger=gc.logger.name):
        ok, msg = await gc._invoke_slash_via_userbot(
            "invoke_play", channel_id=451607097432604672, query="x",
        )

    assert ok is True
    assert not any("relay disabled" in r.message for r in caplog.records), (
        f"no warning expected when relay path succeeds: "
        f"{[r.message for r in caplog.records]}"
    )

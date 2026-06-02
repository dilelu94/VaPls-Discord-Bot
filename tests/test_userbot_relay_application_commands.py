"""Anti-regresión de los 3 bugs que rompieron el relay /play del userbot
y dejaban al indio cayendo a ``playFromIndio`` para cada pedido de música:

  1. ``config.INDIO_RELAY_TIMEOUT`` no existía en ``userbot/config.py``
     (solo en el main bot). ``_resolve_slash_commands`` reventaba con
     ``AttributeError`` → HTTP 500 → fallback silencioso.

  2. ``Messageable.slash_commands`` quedó deprecated en discord.py-self 2.1
     y bajo ciertas condiciones devolvía algo no-awaitable que el helper
     intentaba ``await`` igual: ``object async_generator can't be used in
     'await' expression`` → HTTP 500 → fallback.

  3. Aunque el fetch ANDUVIERA, ``slash_commands(query=name)`` no traía
     los options del comando. Cuando el handler hacía
     ``await play_cmd(query=query)``, ``SlashCommand._parse_kwargs`` veía
     ``self.options = []`` y descartaba el ``query``. La invocación
     llegaba a Discord sin option y Discord rechazaba con
     ``50035 Invalid Form Body In data.option`` → HTTP 500 → fallback.

El fix migra ``_resolve_slash_commands`` a ``Messageable.application_commands``
(API nueva, ``2.1+``) que devuelve la lista entera con options completas, y
deja ``slash_commands`` solo como fallback para versions anteriores. El
filtro por name se hace client-side.

Estos tests pinean cada uno de los tres comportamientos por separado para
que una regresión futura mande señales claras en CI antes de que vuelva a
romperse en prod.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"
RELAY_SECRET = "test-relay-secret"
CHANNEL_ID = 111_222_333


def _load_relay_helpers():
    """Extrae ``_resolve_slash_commands`` + ``_pick_vapls_command`` +
    ``_command_owner_id`` + ``_relay_invoke_play`` del fuente del userbot
    y los ejecuta en un namespace aislado con stubs. Mirrors el patrón
    de ``test_userbot_relay_invoke_play.py`` para no importar ``userbot.bot``
    (que intentaría conectarse a Discord)."""
    src = (_USERBOT_DIR / "bot.py").read_text().splitlines()

    def _extract(name: str) -> str:
        start = next(
            i for i, line in enumerate(src)
            if line.startswith(f"async def {name}(") or line.startswith(f"def {name}(")
        )
        end = next(
            i for i, line in enumerate(src[start + 1:], start=start + 1)
            if line.startswith(("async def ", "def ", "class "))
        )
        return "\n".join(src[start:end])

    resolve_block = _extract("_resolve_slash_commands")
    handler_block = _extract("_relay_invoke_play")
    pick_block = _extract("_pick_vapls_command")
    owner_block = _extract("_command_owner_id")

    cfg = SimpleNamespace(
        RELAY_SECRET=RELAY_SECRET,
        INDIO_RELAY_TIMEOUT=2.0,
        VAPLS_BOT_ID=1_489_830_543_074_918_482,
    )
    cli = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda cid: None,
        fetch_channel=AsyncMock(side_effect=Exception("not found")),
    )
    log_stub = logging.getLogger("test_relay_application_commands")

    ns: dict = {
        "config": cfg,
        "client": cli,
        "log": log_stub,
        "web": web,
        "discord": discord,
        "asyncio": asyncio,
        "Optional": object,
    }
    exec(owner_block, ns)
    exec(pick_block, ns)
    exec(resolve_block, ns)
    exec(handler_block, ns)
    return ns["_resolve_slash_commands"], ns["_relay_invoke_play"], cfg, cli


_resolve, _handler, _cfg, _client = _load_relay_helpers()


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/invoke_play", _handler)
    return app


async def _start() -> TestClient:
    tc = TestClient(TestServer(_make_app()))
    await tc.start_server()
    return tc


# ---------------------------------------------------------------------------
# Bug 1: INDIO_RELAY_TIMEOUT existe en userbot/config.py
# ---------------------------------------------------------------------------


def test_userbot_config_exposes_indio_relay_timeout():
    """``userbot/bot.py`` espera ``config.INDIO_RELAY_TIMEOUT`` (se lo pasa
    a ``_resolve_slash_commands``). Si falta, todo invoke /play y /soundpad
    se rompe con ``AttributeError`` antes de tocar Discord. Importamos el
    módulo aislado para no levantar todo el bot."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_test_userbot_config", _USERBOT_DIR / "config.py",
    )
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)

    assert hasattr(cfg, "INDIO_RELAY_TIMEOUT"), (
        "userbot/config.py debe exponer INDIO_RELAY_TIMEOUT — el relay "
        "lo necesita para timeout-ear application_commands()"
    )
    assert isinstance(cfg.INDIO_RELAY_TIMEOUT, float)
    assert cfg.INDIO_RELAY_TIMEOUT > 0


# ---------------------------------------------------------------------------
# Bug 2: prefiere application_commands sobre slash_commands deprecated
# ---------------------------------------------------------------------------


async def test_resolve_prefers_application_commands_when_available():
    """Cuando el channel expone la API nueva ``application_commands`` Y la
    vieja ``slash_commands``, el helper usa la nueva — ``slash_commands``
    no se llama. Justifica el orden documentado en la docstring del helper."""
    fake_play = SimpleNamespace(name="play", application_id=1)
    fake_other = SimpleNamespace(name="other", application_id=2)
    new_api_called = []
    old_api_called = []

    async def _application_commands():
        new_api_called.append(True)
        return [fake_play, fake_other]

    def _slash_commands(query=None):
        old_api_called.append(query)
        async def _aiter():
            yield fake_play
        return _aiter()

    channel = SimpleNamespace(
        application_commands=_application_commands,
        slash_commands=_slash_commands,
    )

    result = await _resolve(channel, "play", timeout=2.0)

    assert new_api_called == [True], (
        "esperaba que se llame application_commands() (API nueva)"
    )
    assert old_api_called == [], (
        f"NO debería llamar slash_commands() (deprecated): {old_api_called!r}"
    )
    assert result == [fake_play], (
        f"esperaba filtrar a [play], got: {result!r}"
    )


async def test_resolve_filters_application_commands_by_name():
    """``application_commands()`` retorna TODOS los commands del channel
    sin filtro server-side. El helper tiene que filtrar client-side por
    name; sino el caller invocaría /soundpad cuando le pidieron /play."""
    play = SimpleNamespace(name="play", application_id=1)
    soundpad = SimpleNamespace(name="soundpad", application_id=1)
    indio = SimpleNamespace(name="indio", application_id=1)

    async def _application_commands():
        return [play, soundpad, indio]

    channel = SimpleNamespace(application_commands=_application_commands)

    result = await _resolve(channel, "play", timeout=2.0)
    assert result == [play], (
        f"esperaba solo el slash 'play' filtrado, got: {result!r}"
    )

    result = await _resolve(channel, "soundpad", timeout=2.0)
    assert result == [soundpad]


async def test_resolve_falls_back_to_slash_commands_when_no_new_api():
    """Back-compat: si el channel solo tiene ``slash_commands`` (versions
    anteriores a 2.1), el helper sigue usando la API vieja. Mantiene la
    función operativa en deploys que no actualizaron discord.py-self."""
    fake_play = SimpleNamespace(name="play", application_id=1)
    old_api_called = []

    def _slash_commands(query=None):
        old_api_called.append(query)
        async def _aiter():
            yield fake_play
        return _aiter()

    channel = SimpleNamespace(slash_commands=_slash_commands)
    # No tiene application_commands attr.

    result = await _resolve(channel, "play", timeout=2.0)

    assert old_api_called == ["play"], (
        f"esperaba fallback a slash_commands(query='play'): {old_api_called!r}"
    )
    assert result == [fake_play]


async def test_resolve_application_commands_respects_timeout():
    """Si ``application_commands()`` queda colgada (Discord cache stall,
    rate-limit silencioso), ``asyncio.wait_for`` la corta. Sin esto el
    handler bloquearía indefinido y el indio nunca recibiría respuesta."""
    async def _hanging_application_commands():
        await asyncio.Event().wait()  # never resolves
        return []

    channel = SimpleNamespace(application_commands=_hanging_application_commands)

    with pytest_raises_timeout():
        await _resolve(channel, "play", timeout=0.05)


# pytest doesn't auto-import; minimal helper to avoid adding `import pytest`.
def pytest_raises_timeout():
    import pytest
    return pytest.raises(asyncio.TimeoutError)


# ---------------------------------------------------------------------------
# Bug 3: el cmd recibe el option 'query' correctamente, NO 50035
# ---------------------------------------------------------------------------


async def test_invoke_play_passes_query_kwarg_to_command():
    """Anti-regresión del 50035: el handler debe terminar invocando el
    SlashCommand con ``query=<algo>`` como kwarg. Con la API vieja
    ``slash_commands(query=name)``, los options del cmd venían vacíos y el
    ``query`` se descartaba en ``_parse_kwargs`` → Discord 50035.

    Con la API nueva ``application_commands()`` los options vienen
    completos, ``_parse_kwargs`` acepta el ``query`` y la invocación se
    arma bien. Pineamos eso verificando lo que recibe el ``__call__``
    del comando."""
    received_kwargs: list[dict] = []

    class _PlayCmd:
        name = "play"
        application_id = 1_489_830_543_074_918_482

        async def __call__(self, **kwargs):
            received_kwargs.append(kwargs)
            return SimpleNamespace(id=1)  # fake Interaction

    async def _application_commands():
        return [_PlayCmd()]

    channel = SimpleNamespace(application_commands=_application_commands)
    _client.get_channel = lambda cid: channel
    _cfg.VAPLS_BOT_ID = 1_489_830_543_074_918_482

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_play",
            json={"channel_id": CHANNEL_ID, "query": "Patricio Rey"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 200, (
        f"esperaba 200 OK del happy path, got {resp.status}: {body!r}"
    )
    assert body.get("invoked") is True
    assert received_kwargs == [{"query": "Patricio Rey"}], (
        f"el cmd debe recibir query='Patricio Rey' como kwarg, "
        f"got: {received_kwargs!r}"
    )


async def test_invoke_play_rejects_when_no_vapls_command_in_results():
    """``application_commands()`` retorna TODOS los commands del channel
    (de varios bots). Si ninguno con name='play' es de VaPls, retorna
    404 — NO debe invocar un slash de otro bot por accidente."""
    other_play = SimpleNamespace(name="play", application_id=999_999)  # otro bot

    async def _application_commands():
        return [other_play]

    channel = SimpleNamespace(application_commands=_application_commands)
    _client.get_channel = lambda cid: channel
    _cfg.VAPLS_BOT_ID = 1_489_830_543_074_918_482  # ID distinto al de other_play

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_play",
            json={"channel_id": CHANNEL_ID, "query": "x"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 404
    assert "not found" in body.get("error", "").lower()


async def test_invoke_play_returns_200_via_application_commands_end_to_end():
    """Happy path full: discord.py-self 2.1+ con application_commands
    funcionando, el handler resuelve el slash de VaPls, lo invoca con
    query=X y devuelve 200. Es el path A que el usuario quiere que
    funcione siempre — sin esto, el indio siempre cae al fallback."""
    async def _application_commands():
        class _PlayCmd:
            name = "play"
            application_id = 1_489_830_543_074_918_482

            async def __call__(self, **kwargs):
                return SimpleNamespace(id=42)
        return [_PlayCmd()]

    channel = SimpleNamespace(application_commands=_application_commands)
    _client.get_channel = lambda cid: channel
    _cfg.VAPLS_BOT_ID = 1_489_830_543_074_918_482

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_play",
            json={"channel_id": CHANNEL_ID, "query": "redondos"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 200
    assert body == {"invoked": True, "query": "redondos"}

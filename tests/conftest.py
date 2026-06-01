"""Shared fixtures and fakes for the behavioral test suite.

Design goals (see plan): tests pin *observable behavior*, mocking only at true
process boundaries — the Discord gateway, the Gemini HTTP API, PostHog, and the
filesystem. Our own helpers are always exercised for real. Assertions look at
outcomes (what the user would see, what state results) rather than exact wording
or internal call counts, so the code stays free to be refactored.
"""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# tests/ lives one level below the repo root; make the bot modules importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Analytics is fire-and-forget infrastructure, not behavior. Neutralise it so
# nothing couples to event names/properties and no test ever reaches PostHog.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def stub_analytics(request, monkeypatch):
    if "test_posthog_client" in request.module.__name__:
        yield
        return
    import analytics
    for name in ("capture", "capture_exception", "identify_user",
                 "identify_guild", "shutdown"):
        monkeypatch.setattr(analytics, name, MagicMock(), raising=False)
    
    try:
        import posthog_client
        for name in ("track_request", "identify_user", "group_identify",
                     "capture_error", "track_ai_generation", "init_observability"):
            monkeypatch.setattr(posthog_client, name, MagicMock(), raising=False)
    except ImportError:
        pass
    yield


# --------------------------------------------------------------------------
# Fake Discord ApplicationContext: the single seam through which command logic
# talks to the user. `ctx.followup.send` records every message sent.
# --------------------------------------------------------------------------
def make_ctx(*, display_name="Tester", name="tester", user_id=1, guild_id=100):
    """Build a fake ApplicationContext.

    `display_name`/`name` may be None to exercise fall-through in the header
    formatter. `guild_id=None` simulates a DM (no guild).
    """
    author = types.SimpleNamespace(id=user_id)
    if display_name is not None:
        author.display_name = display_name
    if name is not None:
        author.name = name

    ctx = MagicMock(name="ApplicationContext")
    ctx.author = author
    if guild_id is None:
        ctx.guild = None
    else:
        ctx.guild = types.SimpleNamespace(id=guild_id)

    sent: list[str] = []
    _msg_id_counter = [1000]

    def _make_fake_message(content, sent_list):
        """Return a fake Discord Message with id, channel.id, and async edit()."""
        msg_id = _msg_id_counter[0]
        _msg_id_counter[0] += 1
        idx = len(sent_list)  # index into sent_list for this message

        class _FakeMessage:
            id = msg_id
            channel = types.SimpleNamespace(id=42)

            async def edit(self, *, content=None, **kwargs):
                # Update the recorded message text in-place so sent_text()
                # reflects the edited content.
                if content is not None and idx < len(sent_list):
                    sent_list[idx] = content

        return _FakeMessage()

    async def _send(content=None, **kwargs):
        msg = _make_fake_message(content, sent)
        sent.append(content)
        return msg

    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock(side_effect=_send)
    ctx.sent_messages = sent

    # Discord interaction surface: defer() ya ocurrió por safe_defer en los
    # comandos reales. Modelamos ctx.response.is_done() => True y exponemos
    # ctx.interaction.edit_original_response como AsyncMock. La semántica del
    # edit es "sobrescribir el deferred (slot 0) en lugar de appendear" — si
    # no hay nada en sent todavía, lo agrega como slot 0. Capturamos también
    # el historial completo de contenidos por los que pasó el deferred en
    # ``ctx.deferred_history`` para poder asertar que un mensaje transitorio
    # (ej. aviso de rotación) apareció antes de ser reemplazado.
    ctx.response = MagicMock()
    ctx.response.is_done = MagicMock(return_value=True)
    deferred_history: list[str] = []

    async def _edit_original(content=None, **kwargs):
        msg = _make_fake_message(content, sent)
        if sent:
            sent[0] = content
        else:
            sent.append(content)
        deferred_history.append(content)
        return msg

    ctx.interaction = MagicMock()
    ctx.interaction.edit_original_response = AsyncMock(side_effect=_edit_original)
    ctx.deferred_history = deferred_history
    return ctx


@pytest.fixture
def ctx_factory():
    return make_ctx


def sent_text(ctx) -> str:
    """All text the user would have seen, concatenated."""
    return "\n".join(m for m in ctx.sent_messages if m is not None)


# --------------------------------------------------------------------------
# Fake geminiClient.generate — lets command tests drive every success/error
# branch without hitting the network.
# --------------------------------------------------------------------------
@pytest.fixture
def patch_generate(monkeypatch):
    import geminiClient

    def _install(*, reply=None, error=None, replies=None, retries=0,
                 retry_key_suffix="abc123"):
        """`reply` = single GeminiReply for every call; `replies` = iterable of
        results (GeminiReply or Exception) consumed in order; `error` = raise
        it. ``retries`` simula que la primera/única llamada rotó N veces de
        key antes de resolver: invoca ``on_retry(attempt, total, key_suffix)``
        N veces antes de devolver el reply (o levantar el error). Útil para
        testear que el aviso de rotación se editó en el deferred. Records
        calls on the returned list."""
        calls: list[dict] = []
        seq = list(replies) if replies is not None else None

        async def _gen(**kwargs):
            calls.append(kwargs)
            on_retry = kwargs.get("on_retry")
            if retries > 0 and on_retry is not None:
                for i in range(retries):
                    await on_retry(i + 1, retries + 1, retry_key_suffix)
            if seq is not None:
                result = seq.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            if error is not None:
                raise error
            return reply

        monkeypatch.setattr(geminiClient, "generate", _gen)
        return calls

    return _install


def make_reply(text="hola che", *, finish_reason="STOP",
               prompt_tokens=10, response_tokens=20, model="gemini-2.5-flash",
               function_calls=None):
    from geminiClient import GeminiReply
    return GeminiReply(
        text=text,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        model=model,
        function_calls=list(function_calls or []),
    )


@pytest.fixture
def reply_factory():
    return make_reply


# --------------------------------------------------------------------------
# Fake aiohttp for geminiClient.generate's real network boundary.
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, *, status=200, payload=None, json_exc=None, enter_exc=None):
        self.status = status
        self._payload = payload
        self._json_exc = json_exc
        self._enter_exc = enter_exc

    async def __aenter__(self):
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


class _FakeSession:
    def __init__(self, resp, captured):
        self._resp = resp
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        # Capture the outgoing request so tests can assert what was sent.
        self._captured.append({"args": args, "kwargs": kwargs})
        return self._resp


@pytest.fixture
def gemini_http(monkeypatch):
    """Configure the fake Gemini HTTP boundary for geminiClient.generate.

    Sets a dummy API key by default. Call the returned function with either a
    payload+status, a json_exc (parse failure), or an enter_exc (timeout /
    client error raised while opening the response). The returned object's
    ``requests`` attribute is a list of captured POST calls so tests can
    inspect the body (e.g. that ``tools`` was forwarded).
    """
    import aiohttp
    import config
    import geminiClient
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEYS", ["test-key"], raising=False)
    # Limpiamos el estado del pool entre tests para que un 429 anterior no
    # deje a "test-key" en cooldown y contamine el proximo caso.
    geminiClient._key_cooldowns.clear()
    geminiClient._next_key_idx = 0
    geminiClient._sticky_key = None

    class _Spy:
        requests: list = []

    spy = _Spy()
    spy.requests = []

    def _configure(*, status=200, payload=None, json_exc=None, enter_exc=None):
        resp = _FakeResp(status=status, payload=payload,
                         json_exc=json_exc, enter_exc=enter_exc)
        monkeypatch.setattr(aiohttp, "ClientSession",
                            lambda *a, **k: _FakeSession(resp, spy.requests))
        return spy

    _configure.requests = spy.requests
    return _configure


# --------------------------------------------------------------------------
# Indio memory isolation: point persistence at a tmp file and reset the
# in-memory state around each test that touches it.
# --------------------------------------------------------------------------
@pytest.fixture
def indio(tmp_path, monkeypatch):
    import config
    import geminiCommand as gc

    mem_path = tmp_path / "indio_memory.json"
    monkeypatch.setattr(config, "INDIO_MEMORY_PATH", str(mem_path), raising=False)
    # Por default, los tests del Indio no redirigen a un canal externo — testean
    # el flow contra ctx.followup. Los tests que quieran ejercitar el override
    # de canal lo seteen explicito en su monkeypatch.
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 0, raising=False)

    def _clear():
        gc._indio_history.clear()
        gc._indio_last_seen.clear()
        gc._indio_long_term.clear()
        gc._indio_locks.clear()
        gc._indio_compressing.clear()
        # Music votes live in playCommand.active_votes now — cancel + clear.
        import playCommand
        for _v in list(playCommand.active_votes.values()):
            _v._closed = True
            if _v._close_task is not None and not _v._close_task.done():
                _v._close_task.cancel()
        playCommand.active_votes.clear()

    _clear()
    gc._mem_path = str(mem_path)  # convenience for tests
    yield gc
    _clear()

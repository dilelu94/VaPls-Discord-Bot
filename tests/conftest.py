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
def stub_analytics(monkeypatch):
    import analytics
    for name in ("capture", "capture_exception", "identify_user",
                 "identify_guild", "shutdown"):
        monkeypatch.setattr(analytics, name, MagicMock(), raising=False)
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

    async def _send(content=None, **kwargs):
        sent.append(content)

    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock(side_effect=_send)
    ctx.sent_messages = sent
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

    def _install(*, reply=None, error=None, replies=None):
        """`reply` = single GeminiReply for every call; `replies` = iterable of
        results (GeminiReply or Exception) consumed in order; `error` = raise it.
        Records calls on the returned list."""
        calls: list[dict] = []
        seq = list(replies) if replies is not None else None

        async def _gen(**kwargs):
            calls.append(kwargs)
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
               prompt_tokens=10, response_tokens=20, model="gemini-2.5-flash"):
    from geminiClient import GeminiReply
    return GeminiReply(
        text=text,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        model=model,
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
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return self._resp


@pytest.fixture
def gemini_http(monkeypatch):
    """Configure the fake Gemini HTTP boundary for geminiClient.generate.

    Sets a dummy API key by default. Call the returned function with either a
    payload+status, a json_exc (parse failure), or an enter_exc (timeout /
    client error raised while opening the response).
    """
    import aiohttp
    import config
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key", raising=False)

    def _configure(*, status=200, payload=None, json_exc=None, enter_exc=None):
        resp = _FakeResp(status=status, payload=payload,
                         json_exc=json_exc, enter_exc=enter_exc)
        monkeypatch.setattr(aiohttp, "ClientSession",
                            lambda *a, **k: _FakeSession(resp))

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

    def _clear():
        gc._indio_history.clear()
        gc._indio_last_seen.clear()
        gc._indio_long_term.clear()
        gc._indio_locks.clear()
        gc._indio_compressing.clear()

    _clear()
    gc._mem_path = str(mem_path)  # convenience for tests
    yield gc
    _clear()

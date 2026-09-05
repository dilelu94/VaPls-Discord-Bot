import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot import parse_clip_duration, clipLogic
import config


def get_sent_message(ctx) -> str:
    calls = ctx.respond.call_args_list or ctx.followup.send.call_args_list
    if not calls:
        return ""
    call = calls[-1]
    args, kwargs = call.args, call.kwargs
    if kwargs.get("content"):
        return str(kwargs["content"])
    if args:
        return str(args[0])
    return ""


def test_parse_clip_duration():
    assert parse_clip_duration("10m") == 600.0
    assert parse_clip_duration("5m") == 300.0
    assert parse_clip_duration("1m") == 60.0
    assert parse_clip_duration("30s") == 30.0
    assert parse_clip_duration("600") == 600.0
    assert parse_clip_duration(None) == 30.0
    assert parse_clip_duration("") == 30.0
    assert parse_clip_duration("invalid") == 30.0
    # Clamping
    assert parse_clip_duration("20m") == 600.0  # max 10m
    assert parse_clip_duration("1s") == 5.0     # min 5s


async def test_clip_command_requires_voice_channel(ctx_factory):
    ctx = ctx_factory()
    ctx.author.voice = None  # Not in VC

    await clipLogic(ctx, "10m")

    msg = get_sent_message(ctx)
    assert "canal de voz" in msg.lower()


async def test_clip_command_relay_disabled(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "")
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "")

    voice_state = MagicMock()
    voice_state.channel = MagicMock(name="General", id=111)
    ctx.author.voice = voice_state

    await clipLogic(ctx, "10m")

    msg = get_sent_message(ctx)
    assert "relay" in msg.lower()


async def test_clip_command_success(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://127.0.0.1:8081")
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "secret123")

    voice_state = MagicMock()
    voice_channel = MagicMock()
    voice_channel.name = "General"
    voice_channel.id = 111
    voice_state.channel = voice_channel
    ctx.author.voice = voice_state
    ctx.guild.id = 999

    fake_ogg_bytes = b"OggS_fake_audio_content"

    class DummyResponse:
        status = 200
        async def read(self):
            return fake_ogg_bytes
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class DummySession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None):
            return DummyResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", DummySession)

    await clipLogic(ctx, "5m")

    assert ctx.followup.send.called
    kwargs = ctx.followup.send.call_args.kwargs
    args = ctx.followup.send.call_args.args
    content = kwargs.get("content") or (args[0] if args else "")
    file_arg = kwargs.get("file")

    assert "Clip de audio" in content
    assert file_arg is not None
    assert file_arg.filename.endswith(".ogg")


async def test_clip_command_relay_error(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://127.0.0.1:8081")
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "secret123")

    voice_state = MagicMock()
    voice_channel = MagicMock()
    voice_channel.name = "General"
    voice_channel.id = 111
    voice_state.channel = voice_channel
    ctx.author.voice = voice_state
    ctx.guild.id = 999

    class DummyErrorResponse:
        status = 400
        async def json(self):
            return {"error": "No hay audio grabado en el canal"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class DummySession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None):
            return DummyErrorResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", DummySession)

    await clipLogic(ctx, "10m")

    msg = get_sent_message(ctx)
    assert "No hay audio grabado" in msg


async def test_indio_dispatch_make_clip(monkeypatch):
    from geminiCommand import _dispatch_indio_actions
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://127.0.0.1:8081")
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "secret123")

    fake_bot = MagicMock()
    fake_channel = AsyncMock()
    fake_bot.get_channel.return_value = fake_channel

    class DummyResponse:
        status = 200
        async def read(self):
            return b"OggS_test_bytes"
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class DummySession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None):
            return DummyResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", DummySession)

    statuses = await _dispatch_indio_actions(
        bot=fake_bot,
        guild_id=123,
        actions=[("MAKE_CLIP", "30s")],
    )

    assert any("make_clip: ok" in s for s in statuses)
    assert fake_channel.send.called

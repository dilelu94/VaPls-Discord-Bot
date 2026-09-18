"""Behavioral tests for pollinationsImage module and /imagen slash command."""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import pollinationsImage


class FakePollinationsResp:
    def __init__(self, status=200, content_type="image/jpeg", data=b"fake-image-bytes-123456"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._data


class FakePollinationsSession:
    def __init__(self, resp, captured):
        self._resp = resp
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self._captured.append(url)
        return self._resp


@pytest.fixture
def patch_pollinations_http(monkeypatch):
    import aiohttp

    def _install(status=200, data=b"fake-jpeg-bytes-data-1234567890", content_type="image/jpeg", exc=None):
        captured_urls = []
        if exc is not None:
            resp = MagicMock()
            resp.__aenter__ = AsyncMock(side_effect=exc)
        else:
            resp = FakePollinationsResp(status=status, content_type=content_type, data=data)

        def _session_factory(*args, **kwargs):
            return FakePollinationsSession(resp, captured_urls)

        monkeypatch.setattr(aiohttp, "ClientSession", _session_factory)
        return captured_urls

    return _install


@pytest.mark.asyncio
async def test_generate_or_edit_image_success(patch_pollinations_http):
    captured = patch_pollinations_http(status=200, data=b"valid-image-bytes-123")
    res = await pollinationsImage.generate_or_edit_image("un gato espacial")
    assert res == b"valid-image-bytes-123"
    assert len(captured) == 1
    assert "https://image.pollinations.ai/prompt/un%20gato%20espacial" in captured[0]
    assert "model=flux" in captured[0]


@pytest.mark.asyncio
async def test_generate_or_edit_image_with_image_url(patch_pollinations_http):
    captured = patch_pollinations_http(status=200, data=b"transformed-image-bytes")
    res = await pollinationsImage.generate_or_edit_image("cyberpunk style", image_url="https://example.com/foto.jpg", model="turbo")
    assert res == b"transformed-image-bytes"
    assert len(captured) == 1
    assert "image=https%3A%2F%2Fexample.com%2Ffoto.jpg" in captured[0]
    assert "model=turbo" in captured[0]


@pytest.mark.asyncio
async def test_generate_or_edit_image_empty_prompt():
    res = await pollinationsImage.generate_or_edit_image("   ")
    assert res is None


@pytest.mark.asyncio
async def test_generate_or_edit_image_http_error(patch_pollinations_http):
    patch_pollinations_http(status=500, data=b"Internal Server Error")
    res = await pollinationsImage.generate_or_edit_image("test prompt")
    assert res is None


@pytest.mark.asyncio
async def test_imagen_logic_success(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    monkeypatch.setattr(
        pollinationsImage,
        "generate_or_edit_image",
        AsyncMock(return_value=b"fake-generated-image"),
    )

    await pollinationsImage.imagenLogic(ctx, prompt="un paisaje galáctico")

    # Assert that followup.send was called with a discord.File attachment
    assert ctx.followup.send.called
    kwargs = ctx.followup.send.call_args.kwargs
    assert "file" in kwargs
    assert "un paisaje galáctico" in kwargs.get("content", "")


@pytest.mark.asyncio
async def test_imagen_logic_with_attachment(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    mock_gen = AsyncMock(return_value=b"transformed-bytes")
    monkeypatch.setattr(pollinationsImage, "generate_or_edit_image", mock_gen)

    attachment = types.SimpleNamespace(url="https://cdn.discordapp.com/attachments/123/foto.jpg")
    await pollinationsImage.imagenLogic(ctx, prompt="make it anime", image_attachment=attachment)

    mock_gen.assert_called_once_with(
        prompt="make it anime",
        image_url="https://cdn.discordapp.com/attachments/123/foto.jpg",
        model="flux",
    )
    assert ctx.followup.send.called
    assert "Imagen transformada" in ctx.followup.send.call_args.kwargs.get("content", "")


@pytest.mark.asyncio
async def test_imagen_logic_empty_prompt(ctx_factory):
    ctx = ctx_factory()
    await pollinationsImage.imagenLogic(ctx, prompt="")
    # Should reply directly or via followup with warning
    sent_text = "\n".join(ctx.sent_messages) if ctx.sent_messages else ""
    if not sent_text and ctx.followup.send.called:
        sent_text = str(ctx.followup.send.call_args)
    assert "Tenés que especificar" in sent_text or "prompt" in sent_text.lower()


@pytest.mark.asyncio
async def test_imagen_logic_failure_response(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    monkeypatch.setattr(pollinationsImage, "generate_or_edit_image", AsyncMock(return_value=None))

    await pollinationsImage.imagenLogic(ctx, prompt="imagen rota")

    assert ctx.followup.send.called or ctx.sent_messages
    call_str = str(ctx.followup.send.call_args) if ctx.followup.send.called else "\n".join(ctx.sent_messages)
    assert "No pude generar" in call_str or "Probá de nuevo" in call_str


def test_extract_image_url_from_message_attachments():
    att = types.SimpleNamespace(url="https://cdn.discordapp.com/test.png", content_type="image/png", filename="test.png")
    msg = types.SimpleNamespace(attachments=[att], embeds=[])
    url = pollinationsImage.extract_image_url_from_message(msg)
    assert url == "https://cdn.discordapp.com/test.png"


def test_extract_image_url_from_message_embeds():
    emb = types.SimpleNamespace(image=types.SimpleNamespace(url="https://example.com/embed.jpg"), thumbnail=None)
    msg = types.SimpleNamespace(attachments=[], embeds=[emb])
    url = pollinationsImage.extract_image_url_from_message(msg)
    assert url == "https://example.com/embed.jpg"


@pytest.mark.asyncio
async def test_resolve_target_image_url_reply_reference(ctx_factory):
    ctx = ctx_factory()
    ref_att = types.SimpleNamespace(url="https://cdn.discordapp.com/reply_photo.jpg", content_type="image/jpeg", filename="reply_photo.jpg")
    ref_msg = types.SimpleNamespace(attachments=[ref_att], embeds=[])
    ctx.message = types.SimpleNamespace(referenced_message=ref_msg)

    url = await pollinationsImage.resolve_target_image_url(ctx)
    assert url == "https://cdn.discordapp.com/reply_photo.jpg"


@pytest.mark.asyncio
async def test_resolve_target_image_url_channel_history_fallback(ctx_factory):
    ctx = ctx_factory()
    hist_att = types.SimpleNamespace(url="https://cdn.discordapp.com/hist_photo.jpg", content_type="image/jpeg", filename="hist_photo.jpg")
    hist_msg = types.SimpleNamespace(attachments=[hist_att], embeds=[])

    async def _async_gen():
        yield hist_msg

    ctx.channel.history = MagicMock(return_value=_async_gen())

    url = await pollinationsImage.resolve_target_image_url(ctx)
    assert url == "https://cdn.discordapp.com/hist_photo.jpg"


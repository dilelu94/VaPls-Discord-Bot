"""Behavioral tests for the /generarimagen command and huggingfaceImage module.

Fakes the external network boundary (Hugging Face Inference API via aiohttp)
and verifies prompt validations, token configuration checks, retries for
cold-loading, fallback models, size limits, and cleanup.
"""
import asyncio
import os
import pytest
import aiohttp
import discord
from unittest.mock import AsyncMock, MagicMock

import config
import huggingfaceImage
from huggingfaceImage import generarimagenLogic


class FakeHFResponse:
    """Fake aiohttp response for Hugging Face API."""
    def __init__(self, status=200, content_type="image/png", data=b"fake-image-bytes", text=""):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._data = data
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def read(self):
        return self._data

    async def text(self):
        return self._text


@pytest.fixture
def hf_http(monkeypatch):
    """Fixture to fake aiohttp.ClientSession for Hugging Face Inference API."""
    spy_requests = []
    response_queue = []

    class FakeSessionContext:
        def __init__(self):
            self.idx = 0

        def create_session(self, *args, **kwargs):
            nonlocal spy_requests, response_queue
            
            class FakeSession:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    return False
                def post(self_sess, url, **kwargs_post):
                    spy_requests.append({"url": url, "kwargs": kwargs_post})
                    if self.idx < len(response_queue):
                        resp = response_queue[self.idx]
                        self.idx += 1
                        return resp
                    return FakeHFResponse(status=500, text="Internal Server Error")
            return FakeSession()

    ctx = FakeSessionContext()
    monkeypatch.setattr(aiohttp, "ClientSession", ctx.create_session)

    # Mock asyncio.sleep to keep the tests running instantly
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    def _configure(responses):
        nonlocal response_queue
        response_queue.clear()
        response_queue.extend(responses)
        spy_requests.clear()
        ctx.idx = 0
        return spy_requests

    return _configure


# Set a default valid token for test cases (unless testing missing token)
@pytest.fixture(autouse=True)
def default_token(monkeypatch):
    monkeypatch.setattr(config, "HUGGINGFACE_API_TOKEN", "valid-hf-token", raising=False)


# Mock Gemini prompt refinement to return the prompt as-is in tests
@pytest.fixture(autouse=True)
def mock_refine_prompt(monkeypatch):
    async def dummy_refine(prompt):
        return prompt
    monkeypatch.setattr(huggingfaceImage, "_refine_prompt_with_gemini", dummy_refine)


def joined_messages(ctx) -> str:
    """Helper to concatenate all text messages sent through ctx.sent_messages or history."""
    msgs = []
    for m in ctx.sent_messages:
        if m is not None:
            msgs.append(m)
    for h in ctx.deferred_history:
        if h is not None:
            msgs.append(h)
    return "\n".join(msgs)


async def test_generarimagen_success(ctx_factory, hf_http):
    spy = hf_http([
        FakeHFResponse(status=200, content_type="image/png", data=b"flux-image-data")
    ])
    ctx = ctx_factory()
    
    # We spy on os.unlink to capture the temp file path before it gets deleted
    original_unlink = os.unlink
    deleted_paths = []
    def spy_unlink(path):
        deleted_paths.append(path)
        original_unlink(path)
    import os as os_mod
    os_mod.unlink = spy_unlink

    try:
        await generarimagenLogic(ctx, "un perrito en el espacio")
        
        # Verify the request went to the default FLUX model
        assert len(spy) == 1
        assert "FLUX.1-schnell" in spy[0]["url"]
        assert spy[0]["kwargs"]["json"] == {"inputs": "un perrito en el espacio"}
        assert spy[0]["kwargs"]["headers"] == {"Authorization": "Bearer valid-hf-token"}

        # Verify response message editing
        assert ctx.interaction.edit_original_response.call_count == 2
        _, kwargs = ctx.interaction.edit_original_response.call_args
        assert kwargs["content"] == ""
        assert isinstance(kwargs["file"], discord.File)
        assert kwargs["file"].filename == "imagen.png"

        # Verify the temp file was deleted
        assert len(deleted_paths) == 1
        assert not os.path.exists(deleted_paths[0])
    finally:
        os_mod.unlink = original_unlink


async def test_generarimagen_cold_loading_retry(ctx_factory, hf_http):
    spy = hf_http([
        FakeHFResponse(status=503, text="currently loading"),
        FakeHFResponse(status=503, text="ModelTooBusy"),
        FakeHFResponse(status=200, content_type="image/png", data=b"flux-success-after-retry")
    ])
    ctx = ctx_factory()

    await generarimagenLogic(ctx, "luna llena")

    # Verify we retried 3 times (all on FLUX) and finally succeeded
    assert len(spy) == 3
    for s in spy:
        assert "FLUX.1-schnell" in s["url"]
    
    # Verify the image was sent
    assert ctx.interaction.edit_original_response.call_count == 2
    _, kwargs = ctx.interaction.edit_original_response.call_args
    assert kwargs["content"] == ""
    assert kwargs["file"].filename == "imagen.png"


async def test_generarimagen_fallback_to_sdxl(ctx_factory, hf_http):
    # If the default model fails immediately (e.g. 500 error), it falls back to SDXL
    spy = hf_http([
        FakeHFResponse(status=500, text="Internal Server Error"),
        FakeHFResponse(status=200, content_type="image/png", data=b"sdxl-success-data")
    ])
    ctx = ctx_factory()

    await generarimagenLogic(ctx, "cielo estrellado")

    # Verify we hit FLUX first, then fallback SD3
    assert len(spy) == 2
    assert "FLUX.1-schnell" in spy[0]["url"]
    assert "stable-diffusion-3-medium-diffusers" in spy[1]["url"]

    # Verify the image was sent
    assert ctx.interaction.edit_original_response.call_count == 2
    _, kwargs = ctx.interaction.edit_original_response.call_args
    assert kwargs["file"].filename == "imagen.png"


async def test_generarimagen_total_failure(ctx_factory, hf_http):
    # Both default and fallback models fail
    spy = hf_http([
        FakeHFResponse(status=500, text="FLUX is broken"),
        FakeHFResponse(status=500, text="SDXL is also broken")
    ])
    ctx = ctx_factory()

    await generarimagenLogic(ctx, "algo imposible")

    # Verify both models were attempted
    assert len(spy) == 2
    assert "FLUX.1-schnell" in spy[0]["url"]
    assert "stable-diffusion-3-medium-diffusers" in spy[1]["url"]

    # Verify we showed a friendly error message to the user
    text = joined_messages(ctx)
    assert "No pude generar la imagen" in text
    assert ctx.interaction.edit_original_response.call_count == 2


async def test_generarimagen_missing_token(ctx_factory, monkeypatch):
    monkeypatch.setattr(config, "HUGGINGFACE_API_TOKEN", "", raising=False)
    
    # Assert network is not hit
    class NetworkHitError(Exception):
        pass
    def raise_network_error(*args, **kwargs):
        raise NetworkHitError("Network shouldn't be touched!")
    monkeypatch.setattr(aiohttp, "ClientSession", raise_network_error)

    ctx = ctx_factory()
    await generarimagenLogic(ctx, "algun prompt")

    text = joined_messages(ctx)
    assert "no está configurado" in text or "token" in text.lower()


async def test_generarimagen_empty_prompt(ctx_factory, monkeypatch):
    # Assert network is not hit
    def raise_network_error(*args, **kwargs):
        raise AssertionError("Network shouldn't be touched!")
    monkeypatch.setattr(aiohttp, "ClientSession", raise_network_error)

    ctx = ctx_factory()
    
    # Test empty or whitespace prompts
    await generarimagenLogic(ctx, "   ")
    text = joined_messages(ctx)
    assert "decime qué generar" in text

    await generarimagenLogic(ctx, "")
    text = joined_messages(ctx)
    assert "decime qué generar" in text


async def test_generarimagen_file_too_large(ctx_factory, hf_http):
    # Hugging Face succeeds, but Discord rejects the payload size (e.g. 413 Payload Too Large)
    hf_http([
        FakeHFResponse(status=200, content_type="image/png", data=b"huge-image-bytes")
    ])
    
    ctx = ctx_factory()
    # Mock edit_original_response to raise HTTPException for too large file
    mock_resp = MagicMock()
    mock_resp.status = 413
    ctx.interaction.edit_original_response.side_effect = discord.HTTPException(
        response=mock_resp,
        message="413 Payload Too Large"
    )

    deleted_paths = []
    original_unlink = os.unlink
    def spy_unlink(path):
        deleted_paths.append(path)
        original_unlink(path)
    import os as os_mod
    os_mod.unlink = spy_unlink

    try:
        await generarimagenLogic(ctx, "imagen gigante")
        
        # Verify the user gets a readable limit error message
        text = joined_messages(ctx)
        assert "supera el límite" in text or "8 MB" in text

        # Verify the temp file was still cleaned up
        assert len(deleted_paths) == 1
        assert not os.path.exists(deleted_paths[0])
    finally:
        os_mod.unlink = original_unlink


async def test_generarimagen_outside_channel_sends_to_target(ctx_factory, hf_http, monkeypatch):
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 1490008278275461280)
    hf_http([
        FakeHFResponse(status=200, content_type="image/png", data=b"image-outside")
    ])
    
    # Context invoked in channel 42 (not target channel)
    ctx = ctx_factory(channel_id=42)
    
    # Mock bot and the target channel
    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot._mock_custom_bot = True
    mock_bot.get_channel.return_value = mock_channel
    ctx.bot = mock_bot

    await generarimagenLogic(ctx, "perrito lindo")

    # Verify target channel received the file
    assert mock_channel.send.call_count == 1
    _, send_kwargs = mock_channel.send.call_args
    assert send_kwargs.get("file").filename == "imagen.png"
    assert "<@1>" in send_kwargs.get("content")
    assert "perrito lindo" in send_kwargs.get("content")

    # Verify invoking channel shows the "generating" status
    history_text = "\n".join(ctx.deferred_history)
    assert "Imagen generándose en <#1490008278275461280>" in history_text
    assert "Imagen generada en <#1490008278275461280>" in history_text


async def test_generarimagen_inside_channel_responds_directly(ctx_factory, hf_http, monkeypatch):
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 1490008278275461280)
    hf_http([
        FakeHFResponse(status=200, content_type="image/png", data=b"image-inside")
    ])
    
    # Context invoked inside the target channel
    ctx = ctx_factory(channel_id=1490008278275461280)
    mock_bot = MagicMock()
    # Explicitly do NOT set _mock_custom_bot to check that inside channel behaves direct
    ctx.bot = mock_bot

    await generarimagenLogic(ctx, "perrito lindo")

    # Verify target channel was NOT directly sent to via send
    assert mock_bot.get_channel.call_count == 0

    # Verify the image was sent via the edit_original_response
    assert ctx.interaction.edit_original_response.call_count == 2
    _, kwargs = ctx.interaction.edit_original_response.call_args
    assert kwargs["content"] == ""
    assert isinstance(kwargs["file"], discord.File)


async def test_generarimagen_outside_channel_no_access(ctx_factory, hf_http, monkeypatch):
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 1490008278275461280)
    
    # We shouldn't hit network if access check fails early
    def raise_network_error(*args, **kwargs):
        raise AssertionError("Network shouldn't be touched!")
    monkeypatch.setattr(aiohttp, "ClientSession", raise_network_error)

    # Invoked in channel 42 (outside)
    ctx = ctx_factory(channel_id=42)
    
    # Mock bot but return None for get_channel and fetch_channel
    mock_bot = MagicMock()
    mock_bot._mock_custom_bot = True
    mock_bot.get_channel.return_value = None
    async def fake_fetch(cid):
        return None
    mock_bot.fetch_channel = fake_fetch
    ctx.bot = mock_bot

    await generarimagenLogic(ctx, "un perrito")

    # Verify we edited to state we don't have access
    history_text = "\n".join(ctx.deferred_history)
    assert "no acceso al canal" in history_text


async def test_generarimagen_outside_channel_forbidden_on_send(ctx_factory, hf_http, monkeypatch):
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 1490008278275461280)
    hf_http([
        FakeHFResponse(status=200, content_type="image/png", data=b"image-outside")
    ])
    
    # Context invoked in channel 42 (outside)
    ctx = ctx_factory(channel_id=42)
    
    # Mock bot and channel that raises Forbidden on send
    mock_channel = AsyncMock()
    mock_channel.send.side_effect = discord.Forbidden(
        response=MagicMock(status=403),
        message="Forbidden"
    )
    mock_bot = MagicMock()
    mock_bot._mock_custom_bot = True
    mock_bot.get_channel.return_value = mock_channel
    ctx.bot = mock_bot

    await generarimagenLogic(ctx, "perrito lindo")

    # Verify history has "no acceso al canal"
    history_text = "\n".join(ctx.deferred_history)
    assert "no acceso al canal" in history_text

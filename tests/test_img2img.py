import asyncio
import os
import pytest
import aiohttp
import discord
from unittest.mock import AsyncMock, MagicMock, patch

import config
import huggingfaceImage
import geminiCommand
from geminiCommand import _dispatch_indio_actions


class FakeHTTPResponse:
    def __init__(self, status=200, data=b"fake-image-bytes-data", text_data=""):
        self.status = status
        self._data = data
        self._text_data = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def read(self):
        return self._data

    async def text(self):
        return self._text_data or self._data.decode("utf-8", errors="ignore")


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    monkeypatch.setattr(
        config, "CLOUDFLARE_ACCOUNT_ID", "valid-account-id", raising=False
    )
    monkeypatch.setattr(
        config, "CLOUDFLARE_API_TOKEN", "valid-api-token", raising=False
    )


@pytest.fixture(autouse=True)
def mock_refine_prompt(monkeypatch):
    async def dummy_refine(prompt):
        return prompt

    monkeypatch.setattr(huggingfaceImage, "_refine_prompt_with_gemini", dummy_refine)


# ==============================================================================
# Low-level generate_img2img Tests
# ==============================================================================


async def test_generate_img2img_success(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert "api.cloudflare.com" in url
            assert kwargs["headers"]["Authorization"] == "Bearer valid-api-token"
            assert "prompt" in kwargs["json"]
            assert "image_b64" in kwargs["json"]
            return FakeHTTPResponse(status=200, data=b"generated-output-image-bytes")

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    # Create dummy input image
    input_path = "image_cache/test_input.png"
    os.makedirs("image_cache", exist_ok=True)
    with open(input_path, "w") as f:
        f.write("dummy-input-data")

    try:
        out_path = await huggingfaceImage.generate_img2img(
            prompt="make it blue", init_image_path=input_path
        )

        assert out_path is not None
        assert os.path.exists(out_path)
        assert "cfi2i_" in out_path

        # Verify saved contents
        with open(out_path, "rb") as f:
            saved_bytes = f.read()
        assert saved_bytes == b"generated-output-image-bytes"

        # Cleanup
        if os.path.exists(out_path):
            os.unlink(out_path)
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


async def test_generate_img2img_missing_config(monkeypatch):
    monkeypatch.setattr(config, "CLOUDFLARE_ACCOUNT_ID", "", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        await huggingfaceImage.generate_img2img(
            prompt="make it blue", init_image_path="dummy.png"
        )
    assert "Configuración Faltante" in str(exc_info.value)
    assert "CLOUDFLARE_ACCOUNT_ID" in str(exc_info.value)


async def test_generate_img2img_api_failure(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            return FakeHTTPResponse(status=400, text_data="Bad Request Error")

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    # Create dummy input image
    input_path = "image_cache/test_input.png"
    os.makedirs("image_cache", exist_ok=True)
    with open(input_path, "w") as f:
        f.write("dummy-input-data")

    try:
        with pytest.raises(RuntimeError) as exc_info:
            await huggingfaceImage.generate_img2img(
                prompt="make it blue", init_image_path=input_path
            )
        assert "Cloudflare Workers AI falló" in str(exc_info.value)
        assert "400" in str(exc_info.value)
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


# ==============================================================================
# _dispatch_indio_actions EDIT_IMAGE Integration Tests
# ==============================================================================


@pytest.fixture
def mock_aiohttp_session(monkeypatch):
    """Fixture to fake all HTTP get (downloads) and post (Cloudflare AI) requests."""
    spy_calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self_sess, url, **kwargs):
            spy_calls.append({"method": "GET", "url": url, "kwargs": kwargs})
            return FakeHTTPResponse(status=200, data=b"downloaded-input-image-bytes")

        def post(self_sess, url, **kwargs):
            spy_calls.append({"method": "POST", "url": url, "kwargs": kwargs})
            return FakeHTTPResponse(status=200, data=b"generated-output-image-bytes")

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return spy_calls


async def test_dispatch_edit_image_success(mock_aiohttp_session, monkeypatch):
    # Spy on os.unlink to check file cleanup
    deleted_paths = []
    original_unlink = os.unlink

    def spy_unlink(path):
        deleted_paths.append(path)
        original_unlink(path)

    monkeypatch.setattr(os, "unlink", spy_unlink)

    # Set up mocks for Discord Bot and Channel
    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = mock_channel

    # User context and replied message attachment URLs
    requester = MagicMock(spec=discord.Member)
    requester.id = 123
    attachment_urls = [
        {
            "url": "https://cdn.discord/attachments/1.png",
            "mime_type": "image/png",
            "filename": "car.png",
        }
    ]

    actions = [("EDIT_IMAGE", "make it blue")]
    reply_handle = MagicMock()
    reply_handle.channel_id = 456

    statuses = await _dispatch_indio_actions(
        bot=mock_bot,
        guild_id=789,
        actions=actions,
        reply_handle=reply_handle,
        reply_text="🎨 Editando imagen...",
        requester_member=requester,
        attachment_urls=attachment_urls,
        source_message_id=999,
    )

    # Verify status logs success
    assert len(statuses) == 1
    assert "image_edit: ok — success" in statuses[0]

    # Verify input image was downloaded and post to Cloudflare occurred
    assert len(mock_aiohttp_session) == 2
    assert mock_aiohttp_session[0]["method"] == "GET"
    assert mock_aiohttp_session[0]["url"] == "https://cdn.discord/attachments/1.png"
    assert mock_aiohttp_session[1]["method"] == "POST"
    assert "api.cloudflare.com" in mock_aiohttp_session[1]["url"]

    # Verify it posted output image to correct channel
    assert mock_channel.send.call_count == 1
    _, send_kwargs = mock_channel.send.call_args
    assert send_kwargs.get("file").filename == "imagen_editada.png"
    assert "<@123>" in send_kwargs.get("content")
    assert "make it blue" in send_kwargs.get("content")

    # Verify BOTH input image and output image were unlinked (cleaned up)
    assert len(deleted_paths) == 2
    # Input was f"image_cache/input_999.png"
    assert any("input_999" in p for p in deleted_paths)
    # Output was f"image_cache/cfi2i_..."
    assert any("cfi2i_" in p for p in deleted_paths)
    assert not os.path.exists("image_cache/input_999.png")


def get_send_content(call_args):
    """Helper to extract text content from mock send call args/kwargs."""
    if not call_args:
        return ""
    args, kwargs = call_args
    if args:
        return args[0]
    return kwargs.get("content", "")


async def test_dispatch_edit_image_no_attachments():
    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = mock_channel

    actions = [("EDIT_IMAGE", "make it blue")]
    reply_handle = MagicMock()
    reply_handle.channel_id = 456

    statuses = await _dispatch_indio_actions(
        bot=mock_bot,
        guild_id=789,
        actions=actions,
        reply_handle=reply_handle,
        reply_text="🎨 Editando imagen...",
        requester_member=None,
        attachment_urls=None,
    )

    assert "image_edit: fail — no image to edit" in statuses[0]
    # Warning message sent
    assert mock_channel.send.call_count == 1
    assert "Tenés que responder a un mensaje con una imagen" in get_send_content(
        mock_channel.send.call_args
    )


async def test_dispatch_edit_image_missing_config_handling(
    mock_aiohttp_session, monkeypatch
):
    monkeypatch.setattr(config, "CLOUDFLARE_ACCOUNT_ID", "", raising=False)

    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = mock_channel

    requester = MagicMock(spec=discord.Member)
    requester.id = 123
    attachment_urls = [
        {
            "url": "https://cdn.discord/attachments/1.png",
            "mime_type": "image/png",
            "filename": "car.png",
        }
    ]

    actions = [("EDIT_IMAGE", "make it blue")]
    reply_handle = MagicMock()
    reply_handle.channel_id = 456

    statuses = await _dispatch_indio_actions(
        bot=mock_bot,
        guild_id=789,
        actions=actions,
        reply_handle=reply_handle,
        reply_text="🎨 Editando imagen...",
        requester_member=requester,
        attachment_urls=attachment_urls,
        source_message_id=999,
    )

    assert "image_edit: fail" in statuses[0]
    assert "Configuración Faltante" in statuses[0]

    # User-friendly warning was sent to channel
    assert mock_channel.send.call_count == 1
    assert "Configuración Faltante" in get_send_content(mock_channel.send.call_args)
    assert "CLOUDFLARE_ACCOUNT_ID" in get_send_content(mock_channel.send.call_args)

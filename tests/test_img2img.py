import asyncio
import base64
import io
import os
import pytest
import aiohttp
import discord
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

import config
import huggingfaceImage
import geminiCommand
from geminiCommand import _dispatch_indio_actions

MINI_PNG: bytes
_img = Image.new("RGB", (2, 2), color="red")
_buf = io.BytesIO()
_img.save(_buf, format="PNG")
MINI_PNG = _buf.getvalue()


class FakeHTTPResponse:
    def __init__(
        self, status=200, data=b"fake-image-bytes-data", text_data="", json_data=None
    ):
        self.status = status
        self._data = data
        self._text_data = text_data
        self._json_data = json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def read(self):
        return self._data

    async def text(self):
        return self._text_data or self._data.decode("utf-8", errors="ignore")

    async def json(self):
        return self._json_data or {}


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
    flux_resp = {"image": base64.b64encode(MINI_PNG).decode()}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert "api.cloudflare.com" in url
            assert kwargs["headers"]["Authorization"] == "Bearer valid-api-token"
            return FakeHTTPResponse(status=200, json_data=flux_resp)

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    input_path = "image_cache/test_input.png"
    os.makedirs("image_cache", exist_ok=True)
    with open(input_path, "wb") as f:
        f.write(MINI_PNG)

    try:
        out_path = await huggingfaceImage.generate_img2img(
            prompt="make it blue", init_image_paths=[input_path]
        )

        assert out_path is not None
        assert os.path.exists(out_path)
        assert "cfi2i_" in out_path

        with open(out_path, "rb") as f:
            saved_bytes = f.read()
        assert saved_bytes == MINI_PNG

        if os.path.exists(out_path):
            os.unlink(out_path)
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


async def test_generate_img2img_success_multi_ref(monkeypatch):
    flux_resp = {"image": base64.b64encode(MINI_PNG).decode()}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert "api.cloudflare.com" in url
            return FakeHTTPResponse(status=200, json_data=flux_resp)

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    os.makedirs("image_cache", exist_ok=True)
    paths = []
    for i in range(3):
        p = f"image_cache/test_input_{i}.png"
        with open(p, "wb") as f:
            f.write(MINI_PNG)
        paths.append(p)

    try:
        out_path = await huggingfaceImage.generate_img2img(
            prompt="style image 1 like image 0", init_image_paths=paths
        )

        assert out_path is not None
        assert os.path.exists(out_path)
        assert "cfi2i_" in out_path

        if os.path.exists(out_path):
            os.unlink(out_path)
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_generate_img2img_missing_config(monkeypatch):
    monkeypatch.setattr(config, "CLOUDFLARE_ACCOUNT_ID", "", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        await huggingfaceImage.generate_img2img(
            prompt="make it blue", init_image_paths=["dummy.png"]
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

    input_path = "image_cache/test_input.png"
    os.makedirs("image_cache", exist_ok=True)
    with open(input_path, "wb") as f:
        f.write(MINI_PNG)

    try:
        with pytest.raises(RuntimeError) as exc_info:
            await huggingfaceImage.generate_img2img(
                prompt="make it blue", init_image_paths=[input_path]
            )
        assert "FLUX.2 [dev] falló" in str(exc_info.value)
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

    flux_resp = {"image": base64.b64encode(MINI_PNG).decode()}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self_sess, url, **kwargs):
            spy_calls.append({"method": "GET", "url": url, "kwargs": kwargs})
            return FakeHTTPResponse(status=200, data=MINI_PNG)

        def post(self_sess, url, **kwargs):
            spy_calls.append({"method": "POST", "url": url, "kwargs": kwargs})
            return FakeHTTPResponse(status=200, json_data=flux_resp)

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return spy_calls


async def test_dispatch_edit_image_success(mock_aiohttp_session, monkeypatch):
    deleted_paths = []
    original_unlink = os.unlink

    def spy_unlink(path):
        deleted_paths.append(path)
        original_unlink(path)

    monkeypatch.setattr(os, "unlink", spy_unlink)

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

    assert len(statuses) == 1
    assert "image_edit: ok — success" in statuses[0]

    assert len(mock_aiohttp_session) == 2
    assert mock_aiohttp_session[0]["method"] == "GET"
    assert mock_aiohttp_session[0]["url"] == "https://cdn.discord/attachments/1.png"
    assert mock_aiohttp_session[1]["method"] == "POST"
    assert "api.cloudflare.com" in mock_aiohttp_session[1]["url"]

    assert mock_channel.send.call_count == 1
    _, send_kwargs = mock_channel.send.call_args
    assert send_kwargs.get("file").filename == "imagen_editada.png"
    assert "<@123>" in send_kwargs.get("content")
    assert "make it blue" in send_kwargs.get("content")

    assert len(deleted_paths) == 2
    assert any("input_999_0" in p for p in deleted_paths)
    assert any("cfi2i_" in p for p in deleted_paths)


async def test_dispatch_edit_image_multi_attachment(mock_aiohttp_session, monkeypatch):
    deleted_paths = []
    original_unlink = os.unlink

    def spy_unlink(path):
        deleted_paths.append(path)
        original_unlink(path)

    monkeypatch.setattr(os, "unlink", spy_unlink)

    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = mock_channel

    requester = MagicMock(spec=discord.Member)
    requester.id = 456
    attachment_urls = [
        {
            "url": "https://cdn.discord/attachments/style.png",
            "mime_type": "image/png",
            "filename": "style.png",
        },
        {
            "url": "https://cdn.discord/attachments/subject.png",
            "mime_type": "image/png",
            "filename": "subject.png",
        },
    ]

    actions = [("EDIT_IMAGE", "style image 1 like image 0")]
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
        source_message_id=888,
    )

    assert "image_edit: ok — success" in statuses[0]

    assert len(mock_aiohttp_session) == 3
    assert mock_aiohttp_session[0]["url"] == "https://cdn.discord/attachments/style.png"
    assert (
        mock_aiohttp_session[1]["url"] == "https://cdn.discord/attachments/subject.png"
    )
    assert mock_aiohttp_session[2]["method"] == "POST"

    assert any("input_888_0" in p for p in deleted_paths)
    assert any("input_888_1" in p for p in deleted_paths)


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

    assert mock_channel.send.call_count == 1
    assert "Configuración Faltante" in get_send_content(mock_channel.send.call_args)
    assert "CLOUDFLARE_ACCOUNT_ID" in get_send_content(mock_channel.send.call_args)

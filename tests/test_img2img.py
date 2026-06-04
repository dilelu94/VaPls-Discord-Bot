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


class FakePILImage:
    """Fake PIL Image returned by AsyncInferenceClient.image_to_image."""
    def __init__(self):
        self.saved_path = None
        self.save_called = False

    def save(self, path, format="PNG"):
        self.save_called = True
        self.saved_path = path
        # Write dummy content so the file exists on disk
        with open(path, "w") as f:
            f.write("fake-img2img-output-data")


@pytest.fixture
def mock_inference_client(monkeypatch):
    """Fixture to mock AsyncInferenceClient."""
    fake_client = MagicMock()
    fake_client.image_to_image = AsyncMock()
    
    class FakeClientClass:
        def __init__(self, api_key):
            self.api_key = api_key
        def __getattr__(self, name):
            return getattr(fake_client, name)

    monkeypatch.setattr("huggingface_hub.AsyncInferenceClient", FakeClientClass)
    return fake_client


@pytest.fixture(autouse=True)
def default_token(monkeypatch):
    monkeypatch.setattr(config, "HUGGINGFACE_API_TOKEN", "valid-token", raising=False)


@pytest.fixture(autouse=True)
def mock_refine_prompt(monkeypatch):
    async def dummy_refine(prompt):
        return prompt
    monkeypatch.setattr(huggingfaceImage, "_refine_prompt_with_gemini", dummy_refine)


# ==============================================================================
# Low-level generate_img2img Tests
# ==============================================================================

async def test_generate_img2img_success(mock_inference_client):
    fake_img = FakePILImage()
    mock_inference_client.image_to_image.return_value = fake_img

    # Create dummy input image
    input_path = "image_cache/test_input.png"
    os.makedirs("image_cache", exist_ok=True)
    with open(input_path, "w") as f:
        f.write("dummy-input-data")

    try:
        out_path = await huggingfaceImage.generate_img2img(
            prompt="futuristic sports car",
            init_image_path=input_path,
            token="valid-token"
        )

        assert out_path is not None
        assert os.path.exists(out_path)
        assert "hfi2i_" in out_path
        assert fake_img.save_called
        
        # Cleanup
        if os.path.exists(out_path):
            os.unlink(out_path)
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


async def test_generate_img2img_payment_required_402(mock_inference_client):
    # Simulate a 402 Payment Required error from Inference Provider
    mock_inference_client.image_to_image.side_effect = Exception(
        "402 Client Error: Payment Required for url: ..."
    )

    with pytest.raises(RuntimeError) as exc_info:
        await huggingfaceImage.generate_img2img(
            prompt="futuristic sports car",
            init_image_path="dummy.png",
            token="valid-token"
        )
    assert "Pago Requerido" in str(exc_info.value)
    assert "créditos" in str(exc_info.value)


async def test_generate_img2img_missing_token():
    out_path = await huggingfaceImage.generate_img2img(
        prompt="some prompt",
        init_image_path="dummy.png",
        token=""
    )
    assert out_path is None


# ==============================================================================
# _dispatch_indio_actions EDIT_IMAGE Integration Tests
# ==============================================================================

class FakeHTTPResponse:
    def __init__(self, status=200, data=b"replied-image-data"):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def read(self):
        return self._data


@pytest.fixture
def mock_aiohttp_get(monkeypatch):
    """Fixture to fake image download requests."""
    spy_get = []
    
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def get(self_sess, url, **kwargs):
            spy_get.append({"url": url, "kwargs": kwargs})
            return FakeHTTPResponse(status=200, data=b"input-image-data")

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return spy_get


async def test_dispatch_edit_image_success(mock_inference_client, mock_aiohttp_get, monkeypatch):
    fake_img = FakePILImage()
    mock_inference_client.image_to_image.return_value = fake_img

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
    attachment_urls = [{
        "url": "https://cdn.discord/attachments/1.png",
        "mime_type": "image/png",
        "filename": "car.png"
    }]

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
        source_message_id=999
    )

    # Verify status logs success
    assert len(statuses) == 1
    assert "image_edit: ok — success" in statuses[0]

    # Verify input image was downloaded
    assert len(mock_aiohttp_get) == 1
    assert mock_aiohttp_get[0]["url"] == "https://cdn.discord/attachments/1.png"

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
    # Output was f"image_cache/hfi2i_..."
    assert any("hfi2i_" in p for p in deleted_paths)
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
        attachment_urls=None
    )

    assert "image_edit: fail — no image to edit" in statuses[0]
    # Warning message sent
    assert mock_channel.send.call_count == 1
    assert "Tenés que responder a un mensaje con una imagen" in get_send_content(mock_channel.send.call_args)


async def test_dispatch_edit_image_payment_required_402_handling(mock_inference_client, mock_aiohttp_get, monkeypatch):
    mock_inference_client.image_to_image.side_effect = Exception(
        "402 Client Error: Payment Required"
    )

    mock_channel = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = mock_channel

    requester = MagicMock(spec=discord.Member)
    requester.id = 123
    attachment_urls = [{
        "url": "https://cdn.discord/attachments/1.png",
        "mime_type": "image/png",
        "filename": "car.png"
    }]

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
        source_message_id=999
    )

    assert "image_edit: fail" in statuses[0]
    assert "Pago Requerido" in statuses[0] or "402" in statuses[0]
    
    # User-friendly warning was sent to channel
    assert mock_channel.send.call_count == 1
    assert "Pago Requerido" in get_send_content(mock_channel.send.call_args)
    assert "créditos" in get_send_content(mock_channel.send.call_args)


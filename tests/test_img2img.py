import os
import pytest
import aiohttp

import config
import huggingfaceImage


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




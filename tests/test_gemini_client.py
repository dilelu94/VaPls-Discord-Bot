"""Behavior: geminiClient.generate turns a Gemini HTTP response into a
GeminiReply, and classifies every failure mode into a typed GeminiError.kind
so callers can show the right message. The HTTP layer is faked at aiohttp."""
import asyncio

import aiohttp
import pytest

import config
import geminiClient
from geminiClient import GeminiError


async def _gen(**kw):
    kw.setdefault("user_message", "hola")
    kw.setdefault("system_instruction", "sos un bot")
    return await geminiClient.generate(**kw)


async def test_missing_api_key_is_config_error(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "", raising=False)
    # Guard: the network must not be touched when unconfigured.
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network hit")))
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "config"


async def test_well_formed_response_parsed(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "hola "}, {"text": "mundo"}]},
        }],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7},
    })
    reply = await _gen(model="test-model")
    assert reply.text == "hola mundo"
    assert reply.finish_reason == "STOP"
    assert reply.prompt_tokens == 12
    assert reply.response_tokens == 7
    assert reply.model == "test-model"


async def test_http_429_is_http_error_with_status(gemini_http):
    gemini_http(status=429, payload={"error": {"message": "rate limited"}})
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "http"
    assert exc.value.status == 429


async def test_http_500_is_http_error_with_status(gemini_http):
    gemini_http(status=500, payload={"error": {"message": "boom"}})
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "http"
    assert exc.value.status == 500


async def test_timeout_is_timeout_error(gemini_http):
    gemini_http(enter_exc=asyncio.TimeoutError())
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "timeout"


async def test_client_error_is_http_error(gemini_http):
    gemini_http(enter_exc=aiohttp.ClientError())
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "http"


async def test_no_candidates_is_blocked_with_reason(gemini_http):
    gemini_http(status=200, payload={"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "blocked"
    assert exc.value.finish_reason == "SAFETY"


async def test_empty_text_is_empty_error(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{"finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": ""}]}}],
    })
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "empty"
    assert exc.value.finish_reason == "MAX_TOKENS"


async def test_unparseable_body_is_parse_error(gemini_http):
    gemini_http(status=200, json_exc=ValueError("not json"))
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "parse"

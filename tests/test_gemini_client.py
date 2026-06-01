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


async def test_non_429_failure_is_logged_with_status(gemini_http, caplog):
    """Operability: 5xx from Gemini debe quedar visible en los logs
    server-side (no solo informarle al usuario). 429 ya se logueaba aparte —
    este test pinea que el resto de los status también."""
    import logging
    gemini_http(status=503, payload={"error": {"message": "overloaded"}})
    with caplog.at_level(logging.WARNING, logger="bot.gemini"):
        with pytest.raises(GeminiError):
            await _gen()
    # No nos atamos al wording exacto: solo que el status aparezca en algún
    # WARNING para que el operador pueda buscar "503" en journalctl.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("503" in r.getMessage() for r in warnings)


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


# ---------------------------------------------------------------------------
# Function calling: callers can pass tools and Gemini's functionCall parts
# surface on the reply alongside (or instead of) text. The whole point is to
# stop relying on the model emitting a magic marker in free-form text.
# ---------------------------------------------------------------------------


async def test_tools_forwarded_in_request_body(gemini_http):
    spy = gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "ok"}]},
        }],
    })
    tools = [{
        "name": "play_music",
        "description": "Reproducir música",
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING"}},
            "required": ["query"],
        },
    }]
    await _gen(tools=tools)

    sent_body = spy.requests[-1]["kwargs"]["json"]
    declarations = sent_body["tools"][0]["function_declarations"]
    assert any(d["name"] == "play_music" for d in declarations)


async def test_function_call_surfaces_in_reply(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"text": "dale, va Queen"},
                {"functionCall": {"name": "play_music",
                                  "args": {"query": "Queen"}}},
            ]},
        }],
    })
    reply = await _gen(tools=[{"name": "play_music", "parameters": {}}])
    assert reply.text == "dale, va Queen"
    assert reply.function_calls == [
        {"name": "play_music", "args": {"query": "Queen"}},
    ]


async def test_function_call_without_text_is_not_empty(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"functionCall": {"name": "play_sound",
                                  "args": {"name": "milapollo"}}},
            ]},
        }],
    })
    reply = await _gen(tools=[{"name": "play_sound", "parameters": {}}])
    # No text but a function call still counts as a valid reply.
    assert reply.text == ""
    assert reply.function_calls[0]["name"] == "play_sound"
    assert reply.function_calls[0]["args"]["name"] == "milapollo"


async def test_no_text_no_function_calls_is_empty_error(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
    })
    with pytest.raises(GeminiError) as exc:
        await _gen()
    assert exc.value.kind == "empty"


# ---------------------------------------------------------------------------
# Prompt caching: the request is shaped so Gemini's implicit cache can hit on
# the stable system-prompt + tools prefix, and the cache-hit token count it
# reports surfaces on the reply for cost visibility (issue #16).
# ---------------------------------------------------------------------------


async def test_cached_token_count_surfaces_on_reply(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "hola"}]},
        }],
        "usageMetadata": {
            "promptTokenCount": 8000,
            "cachedContentTokenCount": 6000,
            "candidatesTokenCount": 42,
        },
    })
    reply = await _gen()
    assert reply.prompt_tokens == 8000
    assert reply.cached_tokens == 6000
    assert reply.response_tokens == 42


async def test_no_cache_hit_leaves_cached_tokens_unset(gemini_http):
    # A response without cachedContentTokenCount (cold cache) must not invent
    # a number — callers distinguish "no hit" from "zero".
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "hola"}]},
        }],
        "usageMetadata": {"promptTokenCount": 8000, "candidatesTokenCount": 7},
    })
    reply = await _gen()
    assert reply.cached_tokens is None


async def test_volatile_context_kept_out_of_system_instruction(gemini_http):
    # The cacheable prefix must stay pure: per-turn volatile context (e.g.
    # current player state) reaches the model but never the system prompt.
    spy = gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "ok"}]},
        }],
    })
    await _gen(system_instruction="PERSONA ESTABLE",
               volatile_context="[Estado] sonando: Queen")

    body = spy.requests[-1]["kwargs"]["json"]
    assert body["system_instruction"]["parts"][0]["text"] == "PERSONA ESTABLE"
    last_turn = "".join(p["text"] for p in body["contents"][-1]["parts"])
    assert "[Estado] sonando: Queen" in last_turn  # volatile context delivered
    assert "hola" in last_turn                      # ...alongside the user msg


async def test_sticky_key_reused_across_successful_calls(gemini_http, monkeypatch):
    # With multiple healthy keys, consecutive calls stick to one key so the
    # per-key implicit cache keeps hitting (round-robin would scatter them).
    monkeypatch.setattr(config, "GEMINI_API_KEYS", ["kA", "kB", "kC"], raising=False)
    geminiClient._key_cooldowns.clear()
    geminiClient._sticky_key = None
    spy = gemini_http(status=200, payload={
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "ok"}]}}],
    })
    await _gen()
    await _gen()
    keys_used = [r["kwargs"]["params"]["key"] for r in spy.requests]
    assert keys_used[0] == keys_used[1]


async def test_rotates_off_a_rate_limited_key(gemini_http, monkeypatch):
    # Once the sticky key is rate-limited (cooldown), the next call adopts a
    # different healthy key instead of waiting on the exhausted one.
    monkeypatch.setattr(config, "GEMINI_API_KEYS", ["kA", "kB", "kC"], raising=False)
    geminiClient._key_cooldowns.clear()
    geminiClient._sticky_key = None
    spy = gemini_http(status=200, payload={
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "ok"}]}}],
    })
    await _gen()
    first = spy.requests[-1]["kwargs"]["params"]["key"]
    geminiClient._mark_cooldown(first)  # simulate that key hitting its 429
    await _gen()
    second = spy.requests[-1]["kwargs"]["params"]["key"]
    assert second != first

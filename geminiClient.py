"""Async HTTP client for the Google Gemini generateContent API.

Single-shot, stateless from the client's perspective: callers own conversation
history and pass it in. Designed for the free tier of Gemini AI Studio:
https://aistudio.google.com/apikey
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("bot.gemini")

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_TIMEOUT_SEC = 45


class GeminiError(Exception):
    """Typed error for Gemini API failures."""
    def __init__(self, msg: str, *, kind: str, status: Optional[int] = None,
                 finish_reason: Optional[str] = None):
        super().__init__(msg)
        self.kind = kind  # "config" | "http" | "timeout" | "blocked" | "empty" | "parse"
        self.status = status
        self.finish_reason = finish_reason


@dataclass
class GeminiReply:
    """Parsed Gemini response payload."""

    text: str
    finish_reason: Optional[str]
    prompt_tokens: Optional[int]
    response_tokens: Optional[int]
    model: str


async def generate(
    *,
    user_message: str,
    system_instruction: str,
    history: Optional[list[dict]] = None,
    model: Optional[str] = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    max_output_tokens: int = 1024,
) -> GeminiReply:
    """Generate a single Gemini reply for a user message.

    Args:
        user_message: User input text.
        system_instruction: System prompt for persona guidance.
        history: Optional conversation history in Gemini format.
        model: Override model name; defaults to config.GEMINI_MODEL.
        timeout_sec: Total HTTP timeout.
        max_output_tokens: Max tokens for the response.

    Returns:
        GeminiReply with the rendered text and usage metadata.

    Raises:
        GeminiError: When configuration is missing or the API returns errors.

    Side Effects:
        Performs an outbound HTTPS request to the Gemini API.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not config.GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY not set", kind="config")

    mdl = model or config.GEMINI_MODEL
    body = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": (history or []) + [
            {"role": "user", "parts": [{"text": user_message}]},
        ],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = GEMINI_ENDPOINT.format(model=mdl)
    params = {"key": config.GEMINI_API_KEY}
    headers = {"Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, params=params, headers=headers, json=body) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    raise GeminiError(f"JSON parse failed: {e}", kind="parse", status=status)

                if status < 200 or status >= 300:
                    # Surface API error message if available
                    err = (data or {}).get("error") if isinstance(data, dict) else None
                    msg = (err or {}).get("message") if isinstance(err, dict) else None
                    raise GeminiError(
                        f"HTTP {status}: {msg or 'request failed'}",
                        kind="http",
                        status=status,
                    )
    except GeminiError:
        raise
    except asyncio.TimeoutError as e:
        raise GeminiError(f"Gemini timeout after {timeout_sec}s", kind="timeout") from e
    except aiohttp.ClientError as e:
        raise GeminiError(f"HTTP client error: {e}", kind="http") from e

    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        block_reason = None
        feedback = (data.get("promptFeedback") if isinstance(data, dict) else None) or {}
        if isinstance(feedback, dict):
            block_reason = feedback.get("blockReason")
        raise GeminiError(
            f"No candidates (blockReason={block_reason})",
            kind="blocked",
            finish_reason=block_reason,
        )

    cand = candidates[0] or {}
    finish = cand.get("finishReason")
    content = cand.get("content") or {}
    parts = content.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

    if not text:
        raise GeminiError(
            f"Empty text (finishReason={finish})",
            kind="empty",
            finish_reason=finish,
        )

    usage = data.get("usageMetadata") or {}
    return GeminiReply(
        text=text,
        finish_reason=finish,
        prompt_tokens=usage.get("promptTokenCount"),
        response_tokens=usage.get("candidatesTokenCount"),
        model=mdl,
    )

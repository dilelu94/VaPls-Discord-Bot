"""Async HTTP client for the Google Gemini generateContent API.

Single-shot, stateless from the client's perspective: callers own conversation
history and pass it in. Designed for the free tier of Gemini AI Studio:
https://aistudio.google.com/apikey
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import posthog_client

import aiohttp

import config
import geminiKeys

logger = logging.getLogger("bot.gemini")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_TIMEOUT_SEC = 45

# Cooldown aplicado a una key cuando devuelve HTTP 429. El free tier de Gemini
# es 10 RPM, asi que 60s alcanza para que el cupo se libere.
_KEY_COOLDOWN_SEC = 60.0
# Cuando todas las keys estan en cooldown, esperamos como mucho esto antes de
# rendirnos y devolver el ultimo 429 al caller.
_MAX_FAILOVER_WAIT_SEC = 3.0

# Map key -> timestamp (monotonic) hasta cuando esta marcada como agotada.
_key_cooldowns: dict[str, float] = {}
# Indice round-robin para elegir una key nueva cuando hay que adoptar una
# (la sticky se agoto o nunca hubo): reparte el arranque entre las sanas.
_next_key_idx: int = 0
# Key "pegajosa": la seguimos usando mientras este sana en vez de rotar en
# cada llamada. Gemini cachea el prompt de forma implicita POR API key, asi
# que mantenernos en una sola key hace que nuestro prefijo estable (system
# prompt + tools) pegue en cache una y otra vez. Round-robin tiraba cada
# llamada a una key distinta -> cada key veia 1/N del trafico y el cache casi
# nunca pegaba. Al 429 la key entra en cooldown y _pick_key adopta otra.
_sticky_key: Optional[str] = None


def _pool_keys() -> list[str]:
    """Return the current pool. Prefers geminiKeys (loaded from JSON, can
    grow at runtime via DM); falls back to the static config list when the
    JSON pool was never populated (e.g. tests)."""
    from_disk = geminiKeys.active_keys()
    return from_disk or list(config.GEMINI_API_KEYS)


def _available_keys() -> list[str]:
    """Return all configured keys whose cooldown has expired (or never set)."""
    import time as _time

    now = _time.monotonic()
    return [k for k in _pool_keys() if _key_cooldowns.get(k, 0.0) <= now]


def _pick_key() -> Optional[str]:
    """Pick a key, preferring to stick with the current one.

    Sticky selection: while the last-adopted key is healthy we keep returning
    it, so Gemini's implicit prompt cache (keyed per-API-key) keeps hitting on
    our stable system-prompt + tools prefix. Only when that key enters cooldown
    (429) do we adopt the next available key, round-robin, and stick to it.

    Returns ``None`` when there are no keys configured. When every key is in
    cooldown, returns the one whose cooldown expires soonest (so the caller
    can decide whether to wait or surface a 429).
    """
    global _next_key_idx, _sticky_key
    keys = _pool_keys()
    if not keys:
        return None
    available = _available_keys()
    if available:
        # Stay on the sticky key while it's healthy (maximizes cache hits).
        if _sticky_key is not None and _sticky_key in available:
            return _sticky_key
        # Sticky key is gone (cooled down or removed from the pool): adopt the
        # next available one round-robin and make it the new sticky key.
        start = _next_key_idx % len(keys)
        for offset in range(len(keys)):
            candidate = keys[(start + offset) % len(keys)]
            if candidate in available:
                _next_key_idx = (start + offset + 1) % len(keys)
                _sticky_key = candidate
                return candidate
    # Todas en cooldown: devolvemos la que se libera antes (sin tocar sticky).
    return min(keys, key=lambda k: _key_cooldowns.get(k, 0.0))


def _mark_cooldown(key: str) -> None:
    import time as _time

    _key_cooldowns[key] = _time.monotonic() + _KEY_COOLDOWN_SEC


class GeminiError(Exception):
    """Typed error for Gemini API failures."""

    def __init__(
        self,
        msg: str,
        *,
        kind: str,
        status: Optional[int] = None,
        finish_reason: Optional[str] = None,
    ):
        super().__init__(msg)
        self.kind = (
            kind  # "config" | "http" | "timeout" | "blocked" | "empty" | "parse"
        )
        self.status = status
        self.finish_reason = finish_reason


@dataclass
class GeminiReply:
    """Parsed Gemini response payload.

    ``function_calls`` carries any ``functionCall`` parts the model emitted
    when the caller passed ``tools=`` to :func:`generate`. Each item has
    shape ``{"name": str, "args": dict}``. Empty for plain text replies.
    """

    text: str
    finish_reason: Optional[str]
    prompt_tokens: Optional[int]
    response_tokens: Optional[int]
    model: str
    function_calls: list[dict] = field(default_factory=list)
    # How many of ``prompt_tokens`` were served from Gemini's implicit cache
    # (billed at a fraction of the normal input price). ``None`` when the API
    # didn't report it (e.g. no cache hit, or a model without caching).
    cached_tokens: Optional[int] = None


async def generate(
    *,
    user_message: str,
    system_instruction: str,
    history: Optional[list[dict]] = None,
    model: Optional[str] = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    max_output_tokens: int = 1024,
    tools: Optional[list[dict]] = None,
    volatile_context: Optional[str] = None,
    image_parts: Optional[list[dict]] = None,
    distinct_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    on_retry: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
) -> GeminiReply:
    """Generate a single Gemini reply for a user message.

    Args:
        user_message: User input text.
        system_instruction: System prompt for persona guidance. Treated as a
            *stable prefix* — keep it byte-identical across calls in a session
            so Gemini's implicit cache can hit on it (don't fold per-turn
            volatile data in here; use ``volatile_context``).
        history: Optional conversation history in Gemini format.
        model: Override model name; defaults to config.GEMINI_MODEL.
        timeout_sec: Total HTTP timeout.
        max_output_tokens: Max tokens for the response.
        tools: Optional list of FunctionDeclaration dicts. When provided, the
            model can emit ``functionCall`` parts that surface in
            ``GeminiReply.function_calls``.
        volatile_context: Optional per-turn context (e.g. current player state)
            that changes call-to-call. Sent at the very end of the request,
            bundled into the final user turn, so it never poisons the cacheable
            system-prompt + tools prefix.
        image_parts: Optional list of ``{"inlineData": {"mimeType": str, "data": str}}``
            dicts to include as image parts in the user message. Injected between
            volatile_context and the text message, so Gemini sees images in context.
        on_retry: Optional async callback invoked when a key returns 429 and we
            rotate to another key. Receives ``(attempt, total_attempts, key_suffix)``
            where ``key_suffix`` is the last 6 chars of the failed key. Awaited
            inline (must be fast — Discord edits are ~50-200ms). Best-effort:
            any exception inside is swallowed and logged at debug level so a
            broken callback never blocks the retry loop.

    Returns:
        GeminiReply with the rendered text and usage metadata.

    Raises:
        GeminiError: When configuration is missing or the API returns errors.

    Side Effects:
        Performs an outbound HTTPS request to the Gemini API.

    Async:
        This function is a coroutine and must be awaited.
    """
    t_start = time.monotonic()
    if not _pool_keys():
        raise GeminiError("GEMINI_API_KEY not set", kind="config")

    mdl = model or config.GEMINI_MODEL
    # Stable prefix (system_instruction, then tools) goes first so Gemini's
    # implicit cache can match on it; volatile per-turn context rides at the
    # very end, in the final user turn, after the history.
    user_parts: list[dict] = []
    if volatile_context:
        user_parts.append({"text": volatile_context})
    if image_parts:
        user_parts.extend(image_parts)
    user_parts.append({"text": user_message})
    body: dict = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": (history or [])
        + [
            {"role": "user", "parts": user_parts},
        ],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if tools:
        body["tools"] = [{"function_declarations": tools}]
    url = GEMINI_ENDPOINT.format(model=mdl)
    headers = {"Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    data: Optional[dict] = None
    status = 0
    last_429_msg: Optional[str] = None
    # Probamos hasta una vez por key configurada antes de rendirnos.
    attempts = max(1, len(_pool_keys()))
    used_keys: set[str] = set()
    try:
        for attempt in range(attempts):
            picked = _pick_key()
            if picked is None:
                raise GeminiError("GEMINI_API_KEY not set", kind="config")
            if picked in used_keys:
                # Ya probamos todas las keys disponibles; _pick_key esta
                # devolviendo "la menos peor" que ya esta en cooldown.
                # Devolvemos el 429 acumulado.
                raise GeminiError(
                    f"HTTP 429: {last_429_msg or 'all keys rate-limited'}",
                    kind="http",
                    status=429,
                )
            used_keys.add(picked)
            params = {"key": picked}
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(
                    url, params=params, headers=headers, json=body
                ) as resp:
                    status = resp.status
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as e:
                        raise GeminiError(
                            f"JSON parse failed: {e}", kind="parse", status=status
                        )

                    if 200 <= status < 300:
                        break

                    err = (data or {}).get("error") if isinstance(data, dict) else None
                    msg = (err or {}).get("message") if isinstance(err, dict) else None
                    if status == 429:
                        _mark_cooldown(picked)
                        last_429_msg = msg or "rate limited"
                        logger.warning(
                            "gemini key …%s hit 429 (attempt %d/%d): %s",
                            picked[-6:],
                            attempt + 1,
                            attempts,
                            last_429_msg,
                        )
                        if on_retry is not None:
                            try:
                                await on_retry(attempt + 1, attempts, picked[-6:])
                            except Exception:
                                logger.debug("on_retry callback failed", exc_info=True)
                        continue
                    logger.warning(
                        "gemini http %d (key …%s): %s",
                        status,
                        picked[-6:],
                        msg or "request failed",
                    )
                    raise GeminiError(
                        f"HTTP {status}: {msg or 'request failed'}",
                        kind="http",
                        status=status,
                    )
        else:
            # No conseguimos respuesta 2xx con ninguna key: el ultimo error fue 429.
            raise GeminiError(
                f"HTTP 429: {last_429_msg or 'all keys rate-limited'}",
                kind="http",
                status=429,
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
        feedback = (
            data.get("promptFeedback") if isinstance(data, dict) else None
        ) or {}
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
    text_chunks: list[str] = []
    function_calls: list[dict] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if "text" in p and p.get("text"):
            text_chunks.append(p["text"])
        fc = p.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            args = fc.get("args")
            function_calls.append(
                {
                    "name": str(fc["name"]),
                    "args": args if isinstance(args, dict) else {},
                }
            )
    text = "".join(text_chunks).strip()

    if not text and not function_calls:
        raise GeminiError(
            f"Empty text (finishReason={finish})",
            kind="empty",
            finish_reason=finish,
        )

    usage = data.get("usageMetadata") or {}
    prompt_tokens = usage.get("promptTokenCount")
    cached_tokens = usage.get("cachedContentTokenCount")
    response_tokens = usage.get("candidatesTokenCount")

    # Per-call token visibility (issue #16): without this we were guessing
    # which calls were expensive. cached_tokens shows the implicit cache
    # actually hitting on our stable prefix.
    logger.info(
        "gemini ok model=%s prompt_tokens=%s cached_tokens=%s response_tokens=%s",
        mdl,
        prompt_tokens,
        cached_tokens,
        response_tokens,
    )

    # Track Gemini call in PostHog
    posthog_client.track_ai_generation(
        model=mdl,
        user_message=user_message,
        system_instruction=system_instruction,
        history=history,
        response=text,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        cached_tokens=cached_tokens,
        t_start=t_start,
        user_id=distinct_id,
        guild_id=guild_id,
    )

    return GeminiReply(
        text=text,
        finish_reason=finish,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        model=mdl,
        function_calls=function_calls,
        cached_tokens=cached_tokens,
    )

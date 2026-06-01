"""posthog_client.py

Single-responsibility module for all PostHog integrations:
  - Analytics (event capture)
  - User identification
  - Error tracking
  - Structured logs (via OpenTelemetry -> PostHog)
  - AI observability for Gemini (via OpenTelemetry & manual tracking)

Usage:
  from posthog_client import init_observability, track_request, capture_error, get_logger, request_context, track_ai_generation

Call `init_observability()` once at app startup, before anything else.
"""

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Optional

# --- Safe module imports to protect startup ---
_POSTHOG_AVAILABLE = False
try:
    from posthog import Posthog, new_context, identify_context, tag
    _POSTHOG_AVAILABLE = True
except ImportError:
    pass

# OpenTelemetry: logs
_OTEL_LOGS_AVAILABLE = False
try:
    from opentelemetry import logs as otel_logs
    from opentelemetry.sdk.logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk.logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    _OTEL_LOGS_AVAILABLE = True
except ImportError:
    pass

# OpenTelemetry: traces (for AI observability)
_OTEL_TRACES_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from posthog.ai.otel import PostHogSpanProcessor
    from opentelemetry.instrumentation.google_generativeai import GoogleGenerativeAiInstrumentor
    _OTEL_TRACES_AVAILABLE = True
except ImportError:
    pass


_posthog: Optional[Any] = None
_logger: logging.Logger = logging.getLogger("posthog_client")
_observability_initialized = False


def init_observability(service_name: str = "vapls-app") -> None:
    """Call this ONCE at application startup (e.g. in bot.py or userbot/bot.py).

    Sets up:
      - PostHog client (analytics + error tracking)
      - OpenTelemetry -> PostHog log pipeline (asynchronous log batching)
      - OpenTelemetry tracing for Gemini AI observability (auto-instrumented if SDK is used)

    Args:
        service_name: Identifies your app (e.g., 'vapls-main-bot' or 'vapls-userbot').
    """
    global _posthog, _observability_initialized

    if _observability_initialized:
        return

    api_key = os.getenv("POSTHOG_API_KEY")
    if not api_key:
        _logger.info("PostHog API key not set; observability is disabled/no-op.")
        _observability_initialized = True
        return

    host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")

    # 1. PostHog Client
    if _POSTHOG_AVAILABLE:
        try:
            _posthog = Posthog(
                project_api_key=api_key,
                host=host,
                enable_exception_autocapture=True,       # catches unhandled exceptions automatically
                capture_exception_code_variables=False,  # disabled code variable capturing to prevent accidental leakage
            )
            _logger.info("PostHog initialized for %s (host=%s)", service_name, host)
        except Exception as e:
            _logger.warning("Failed to initialize PostHog client: %s", e)
    else:
        _logger.warning("PostHog package is not installed; analytics/errors will be no-op.")

    # 2. OpenTelemetry Logs -> PostHog Log Pipeline
    if _OTEL_LOGS_AVAILABLE:
        try:
            # We explicitly define the Resource with service_name so logs are easily differentiated
            resource = Resource(attributes={SERVICE_NAME: service_name}) if _OTEL_TRACES_AVAILABLE else None
            
            logger_provider = LoggerProvider(resource=resource)
            otel_logs.set_logger_provider(logger_provider)

            otlp_exporter = OTLPLogExporter(
                endpoint=f"{host}/i/v1/logs",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            # BatchLogRecordProcessor processes logs asynchronously on a background thread
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(otlp_exporter))

            otel_handler = LoggingHandler(logger_provider=logger_provider)
            root_logger = logging.getLogger()
            root_logger.addHandler(otel_handler)
            _logger.info("OpenTelemetry logs pipeline hooked to standard Python logging.")
        except Exception as e:
            _logger.warning("Failed to hook OpenTelemetry logs pipeline: %s", e)
    else:
        _logger.warning("OpenTelemetry logs packages are not installed; OTLP log pipeline disabled.")

    # 3. OpenTelemetry Traces -> PostHog (for Gemini AI observability auto-instrumentation)
    if _OTEL_TRACES_AVAILABLE:
        try:
            resource = Resource(attributes={SERVICE_NAME: service_name})
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(
                PostHogSpanProcessor(api_key=api_key, host=host)
            )
            trace.set_tracer_provider(tracer_provider)

            # Auto-instrument standard google-generativeai SDK calls if they ever happen
            GoogleGenerativeAiInstrumentor().instrument()
            _logger.info("OpenTelemetry auto-instrumentation for Gemini SDK enabled.")
        except Exception as e:
            _logger.warning("Failed to hook OpenTelemetry trace pipeline/Gemini instrumentor: %s", e)
    else:
        _logger.warning("OpenTelemetry trace packages not fully installed; tracing auto-instrumentation disabled.")

    _observability_initialized = True


# --- Context manager ---

@contextmanager
def request_context(user_id: str, **extra_tags):
    """Wraps a user request in a PostHog context so every event, log, and
    exception captured inside automatically carries the user's distinct_id
    and any extra tags you pass.

    Usage:
        with request_context(user_id, endpoint="/chat", plan="pro"):
            process_user_request(...)

    Args:
        user_id:    The user's unique ID.
        **extra_tags: Any additional key-value metadata to attach to all events.
    """
    if _POSTHOG_AVAILABLE and _posthog is not None:
        try:
            with new_context():
                identify_context(str(user_id))
                for key, value in extra_tags.items():
                    tag(key, value)
                yield
        except Exception as e:
            _logger.debug("Error in request_context: %s", e)
            yield
    else:
        yield


# --- Analytics ---

def track_request(user_id: Optional[str], event: str, **properties) -> None:
    """Capture a custom analytics event for a user action.

    Usage:
        track_request(user_id, "chat_message_sent", model="gemini-2.0-flash")

    Args:
        user_id:      The user's unique ID.
        event:        Event name, e.g. "chat_message_sent".
        **properties: Any key-value pairs to attach to the event.
    """
    if _posthog is None:
        return
    try:
        if user_id:
            _posthog.capture(distinct_id=str(user_id), event=event, properties=properties or None)
        else:
            # Fallback to context or personless
            _posthog.capture(event=event, properties=properties or None)
    except Exception as e:
        _logger.debug("track_request failed: %s", e)


def identify_user(user_id: str, **person_properties) -> None:
    """Set or update properties on a person profile in PostHog.

    Usage:
        identify_user(user_id, email="user@example.com", plan="pro")

    Args:
        user_id:           The user's unique ID.
        **person_properties: Properties to set on the person.
    """
    if _posthog is None:
        return
    try:
        _posthog.capture(
            distinct_id=str(user_id),
            event="$identify",
            properties={"$set": person_properties},
        )
    except Exception as e:
        _logger.debug("identify_user failed: %s", e)


# --- Error tracking ---

def capture_error(error: Exception, user_id: Optional[str] = None, **properties) -> None:
    """Manually capture an exception and send it to PostHog Error Tracking.
    Use this inside try/except blocks for handled errors.

    Usage:
        try:
            risky_operation()
        except Exception as e:
            capture_error(e, user_id, endpoint="/generate")
            raise

    Args:
        error:       The exception to capture.
        user_id:     Optional. Links the error to a specific user.
        **properties: Optional extra context.
    """
    if _posthog is None:
        return
    try:
        # Safe string conversion of inputs
        did = str(user_id) if user_id else None
        _posthog.capture_exception(
            error,
            distinct_id=did,
            properties=properties or None,
        )
    except Exception as e:
        _logger.debug("capture_error failed: %s", e)


# --- Logs ---

def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Returns a standard Python logger.
    All loggers in Python propagate to the root logger, so logs will be
    piped via OpenTelemetry and also print to the standard terminal handlers automatically.
    """
    return logging.getLogger(name or __name__)


# --- AI / LLM Observability ---

def track_ai_generation(
    model: str,
    user_message: str,
    system_instruction: str,
    response: str,
    prompt_tokens: Optional[int],
    response_tokens: Optional[int],
    t_start: float,
    history: Optional[list] = None,
    user_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    **properties
) -> None:
    """Capture a detailed Gemini LLM generation event in PostHog.
    This generates a standard `$ai_generation` event which maps beautifully to
    PostHog's LLM Observability dashboard.

    Args:
        model:              The exact Gemini model used.
        user_message:       The raw user prompt string.
        system_instruction: The raw system instruction string.
        response:           The response text generated.
        prompt_tokens:      Number of input tokens.
        response_tokens:    Number of output tokens.
        t_start:            The time.monotonic() timestamp when the API call started.
        history:            Optional conversation history in Gemini format.
        user_id:            Optional user distinct_id.
        guild_id:           Optional Discord guild ID.
        **properties:       Additional custom metadata properties.
    """
    if _posthog is None:
        return

    import time

    # Calculate precise latency
    latency_sec = time.monotonic() - t_start

    # Build prompt messages context for PostHog AI/LLM dashboard
    prompt_messages = []
    if system_instruction:
        prompt_messages.append({"role": "system", "content": system_instruction})
    if history:
        for turn in history:
            role = turn.get("role")
            parts = turn.get("parts", [])
            turn_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            prompt_messages.append({"role": role, "content": turn_text})
    prompt_messages.append({"role": "user", "content": user_message})

    # Build the standard $ai_generation event payload
    props = {
        "$ai_model": model,
        "$ai_latency": latency_sec,
        "$ai_input_tokens": int(prompt_tokens) if prompt_tokens is not None else 0,
        "$ai_output_tokens": int(response_tokens) if response_tokens is not None else 0,
        "$ai_input": prompt_messages,
        "$ai_output_choices": [{"text": response}],
    }

    if guild_id:
        props["guild_id"] = str(guild_id)

    # Calculate token cost (pricing reference for Gemini 1.5/2.5 Flash)
    pt = prompt_tokens or 0
    rt = response_tokens or 0
    input_cost = pt * 0.075 / 1_000_000
    output_cost = rt * 0.30 / 1_000_000
    props["$ai_total_cost_usd"] = input_cost + output_cost

    props.update(properties)

    try:
        if user_id:
            _posthog.capture(distinct_id=str(user_id), event="$ai_generation", properties=props)
        else:
            # Let contextvars identify the current distinct_id automatically
            _posthog.capture(event="$ai_generation", properties=props)
    except Exception as e:
        _logger.debug("Failed to capture AI generation: %s", e)

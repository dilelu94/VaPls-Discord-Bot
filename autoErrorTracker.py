"""Automatic GitHub Issue tracking for unhandled background errors and exceptions.

Intercepts uncaught exceptions, asyncio loop errors, and `log.exception` calls,
calculates a unique error fingerprint to prevent duplicate issues, and posts
new issues or reincidence comments to GitHub.
"""

from __future__ import annotations

import asyncio
import hashlib
import datetime
import logging
import os
import platform
import sys
import threading
import traceback
from typing import Any, Optional

import config
import githubIssues

logger = logging.getLogger("bot.error_tracker")

_known_issues: dict[str, int] = {}
_last_reported_time: dict[str, float] = {}
_occurrence_counts: dict[str, int] = {}
_tracker_initialized = False
_current_process_name = "main-bot"


def calculate_fingerprint(
    error: BaseException,
    tb: Optional[Any] = None,
) -> str:
    """Calculate a deterministic fingerprint for an error based on its type and top frame location.

    Returns a 12-character hex string.
    """
    exc_type_name = type(error).__qualname__
    target_tb = tb or getattr(error, "__traceback__", None)

    filename = "unknown"
    func_name = "unknown"

    if target_tb is not None:
        extracted = traceback.extract_tb(target_tb)
        if extracted:
            # Pick the last frame in application code if available, else absolute last frame
            app_frames = [
                f for f in extracted if "site-packages" not in f.filename and "dist-packages" not in f.filename
            ]
            chosen_frame = app_frames[-1] if app_frames else extracted[-1]
            try:
                filename = os.path.relpath(chosen_frame.filename)
            except Exception:
                filename = os.path.basename(chosen_frame.filename)
            func_name = chosen_frame.name

    raw = f"{exc_type_name}:{filename}:{func_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _extract_context(
    ctx: Optional[Any] = None,
    extra: Optional[dict] = None,
) -> dict[str, str]:
    """Extract human-readable execution context from Discord ctx or extra params."""
    context: dict[str, str] = {}
    if ctx is not None:
        command = getattr(getattr(ctx, "command", None), "name", None)
        if command:
            context["Command"] = f"/{command}"

        author = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        if author:
            disp_name = getattr(author, "display_name", None) or getattr(author, "name", "unknown")
            context["User"] = f"{disp_name} (ID: {getattr(author, 'id', 'unknown')})"

        guild = getattr(ctx, "guild", None)
        if guild:
            g_name = getattr(guild, "name", "unknown")
            context["Guild"] = f"{g_name} (ID: {getattr(guild, 'id', 'unknown')})"

        channel = getattr(ctx, "channel", None)
        if channel:
            c_name = getattr(channel, "name", "unknown")
            context["Channel"] = f"#{c_name} (ID: {getattr(channel, 'id', 'unknown')})"

    if extra:
        for k, v in extra.items():
            if v is not None:
                context[str(k).capitalize()] = str(v)

    return context


def format_issue_body(
    *,
    error: BaseException,
    fingerprint: str,
    process_name: str,
    ctx: Optional[Any] = None,
    extra: Optional[dict] = None,
    logger_name: Optional[str] = None,
) -> str:
    """Format markdown body for creating a new GitHub error issue."""
    exc_type_name = type(error).__qualname__
    exc_msg = str(error) or "(Sin mensaje de excepción)"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    context = _extract_context(ctx, extra)
    if logger_name:
        context["Logger"] = logger_name

    context_lines = "\n".join(f"- **{k}**: {v}" for k, v in context.items()) if context else "- *Sin contexto específico*"

    env_info = (
        f"- **Proceso**: `{process_name}`\n"
        f"- **Python**: `{sys.version.split()[0]}`\n"
        f"- **Sistema**: `{platform.platform()}`\n"
        f"- **Timestamp UTC**: `{timestamp}`"
    )

    return f"""<!-- error-fingerprint: {fingerprint} -->
## 🚨 Excepción en segundo plano: `{exc_type_name}`

> **Mensaje**: `{exc_msg}`

### 📌 Contexto de Ejecución
{context_lines}

### ⚙️ Entorno y Telemetría
{env_info}
- **Fingerprint ID**: `{fingerprint}`

### 📄 Traceback
```python
{tb_str}
```
"""


def format_comment_body(
    *,
    error: BaseException,
    fingerprint: str,
    process_name: str,
    occurrences: int = 1,
    ctx: Optional[Any] = None,
    extra: Optional[dict] = None,
    logger_name: Optional[str] = None,
) -> str:
    """Format markdown body for commenting a reincidence on an existing GitHub error issue."""
    exc_type_name = type(error).__qualname__
    exc_msg = str(error) or "(Sin mensaje de excepción)"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    context = _extract_context(ctx, extra)
    if logger_name:
        context["Logger"] = logger_name

    context_lines = "\n".join(f"- **{k}**: {v}" for k, v in context.items()) if context else "- *Sin contexto específico*"

    count_str = f" ({occurrences} ocurrencias registradas en el período)" if occurrences > 1 else ""

    return f"""### 🔄 Reincidencia detectada{count_str}

> **Timestamp**: `{timestamp}` | **Proceso**: `{process_name}`
> **Mensaje**: `{exc_msg}`

#### Contexto de Ejecución
{context_lines}

<details>
<summary><b>Traceback completo (hacé click para desplegar)</b></summary>

```python
{tb_str}
```

</details>
"""


async def report_error(
    error: BaseException,
    *,
    process_name: Optional[str] = None,
    ctx: Optional[Any] = None,
    extra: Optional[dict] = None,
    logger_name: Optional[str] = None,
) -> Optional[int]:
    """Report an error to GitHub Issues with deduplication and reincidence commenting."""
    if not getattr(config, "GITHUB_AUTO_ERROR_ENABLED", True):
        return None

    gh_token = getattr(config, "GITHUB_ISSUES_TOKEN", getattr(config, "GITHUB_TOKEN", ""))
    gh_repo = getattr(config, "GITHUB_REPO", "")
    if not gh_token or not gh_repo:
        logger.debug("GitHub Token/Repo not configured, skipping issue reporting")
        return None

    proc_name = process_name or _current_process_name
    fingerprint = calculate_fingerprint(error)

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cooldown = float(getattr(config, "GITHUB_ERROR_COOLDOWN_SECONDS", 300))
    last_time = _last_reported_time.get(fingerprint, 0.0)

    # Rate limiting / anti-spam cooldown check
    if last_time > 0 and (now - last_time) < cooldown:
        _occurrence_counts[fingerprint] = _occurrence_counts.get(fingerprint, 1) + 1
        logger.debug(
            "Fingerprint %s within cooldown (occurrences: %d), skipping GitHub API call",
            fingerprint,
            _occurrence_counts[fingerprint],
        )
        return _known_issues.get(fingerprint)

    occurrences = _occurrence_counts.get(fingerprint, 1)
    _last_reported_time[fingerprint] = now
    _occurrence_counts[fingerprint] = 0

    error_label = getattr(config, "GITHUB_ERROR_LABEL", "bot-error")

    # Step 1: Check in-memory cache or query GitHub API for existing issue
    issue_data: Optional[dict] = None
    existing_number = _known_issues.get(fingerprint)

    if existing_number is not None:
        issue_data = {"number": existing_number, "state": "open"}
    else:
        try:
            issue_data = await githubIssues.find_issue_by_fingerprint(
                fingerprint, label=error_label
            )
        except Exception as e:
            logger.warning("Failed to search GitHub issue for fingerprint %s: %s", fingerprint, e)

    exc_type_name = type(error).__qualname__
    exc_msg = (str(error) or "").strip()
    short_msg = (exc_msg[:60] + "...") if len(exc_msg) > 60 else exc_msg
    title = f"[Auto-Bug] {exc_type_name}: {short_msg} [fp:{fingerprint}]"

    # Step 2: Handle reincidence vs new issue creation
    if issue_data and issue_data.get("number"):
        number = issue_data["number"]
        _known_issues[fingerprint] = number

        # Reopen if closed
        if issue_data.get("state") == "closed":
            try:
                await githubIssues.reopen_issue(number)
                logger.info("Reopened closed GitHub issue #%d for error %s", number, fingerprint)
            except Exception as e:
                logger.warning("Failed to reopen GitHub issue #%d: %s", number, e)

        # Post reincidence comment
        comment_body = format_comment_body(
            error=error,
            fingerprint=fingerprint,
            process_name=proc_name,
            occurrences=occurrences,
            ctx=ctx,
            extra=extra,
            logger_name=logger_name,
        )
        ok = await githubIssues.add_comment(number, body=comment_body)
        if ok:
            logger.info("Added reincidence comment to GitHub issue #%d for fp:%s", number, fingerprint)
        return number
    else:
        # Create new issue
        body = format_issue_body(
            error=error,
            fingerprint=fingerprint,
            process_name=proc_name,
            ctx=ctx,
            extra=extra,
            logger_name=logger_name,
        )
        new_number = await githubIssues.create_issue(
            title=title,
            body=body,
            labels=[error_label],
        )
        if new_number:
            _known_issues[fingerprint] = new_number
            logger.info("Created new GitHub issue #%d for error fp:%s", new_number, fingerprint)
        return new_number


def _submit_error_task(
    error: BaseException,
    *,
    process_name: Optional[str] = None,
    ctx: Optional[Any] = None,
    extra: Optional[dict] = None,
    logger_name: Optional[str] = None,
) -> None:
    """Submit error report as a background asyncio task if an event loop is running."""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(
                report_error(
                    error,
                    process_name=process_name,
                    ctx=ctx,
                    extra=extra,
                    logger_name=logger_name,
                )
            )
    except RuntimeError:
        # No running event loop
        pass


class GitHubErrorLoggingHandler(logging.Handler):
    """Custom logging handler to intercept log.exception and log.error with tracebacks."""

    def __init__(self, process_name: str = "main-bot") -> None:
        super().__init__(level=logging.ERROR)
        self.process_name = process_name

    def emit(self, record: logging.LogRecord) -> None:
        # Avoid recursive logging loops
        if record.name.startswith("bot.github_issues") or record.name.startswith("bot.error_tracker"):
            return

        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            _submit_error_task(
                exc,
                process_name=self.process_name,
                logger_name=record.name,
                extra={"LogMessage": record.getMessage()},
            )


def init_auto_error_tracker(process_name: str = "main-bot") -> None:
    """Initialize automatic GitHub issue error tracking for the current process.

    Call ONCE at startup in bot.py, userbot/bot.py, or golive/bot.py.
    """
    global _tracker_initialized, _current_process_name
    if _tracker_initialized:
        return
    _tracker_initialized = True
    _current_process_name = process_name

    # 1. Attach logging handler to root logger
    handler = GitHubErrorLoggingHandler(process_name=process_name)
    logging.getLogger().addHandler(handler)

    # 2. Hook global unhandled exceptions
    orig_excepthook = sys.excepthook

    def custom_excepthook(exc_type, exc_value, exc_traceback):
        if exc_value is not None:
            _submit_error_task(
                exc_value,
                process_name=process_name,
                logger_name="sys.excepthook",
            )
        orig_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = custom_excepthook

    # 3. Hook threading unhandled exceptions
    if hasattr(threading, "excepthook"):
        orig_thread_excepthook = threading.excepthook

        def custom_thread_excepthook(args):
            if args.exc_value is not None:
                _submit_error_task(
                    args.exc_value,
                    process_name=process_name,
                    logger_name="threading.excepthook",
                    extra={"Thread": getattr(args.thread, "name", "unknown")},
                )
            orig_thread_excepthook(args)

        threading.excepthook = custom_thread_excepthook

    logger.info("Auto error tracker initialized for process '%s'", process_name)

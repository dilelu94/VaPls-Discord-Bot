"""Behavior: when VAPLS_BOT_ID is misconfigured, the userbot must surface
the problem loudly enough that operators can find it in journalctl. The
silent-failure mode where every relay invocation 404s without any
discoverable breadcrumb has cost real debugging time, so we promote the
log level to ``error`` for both:

  - the unset VAPLS_BOT_ID case (logged once at startup); and
  - the "command found in channel but not owned by VaPls" case
    (logged every time a relay endpoint hits the misconfig).

Boundary mocked: only the log handler / record capture. The
``_pick_vapls_command`` helper is exercised directly with synthetic
SlashCommand-like objects.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_pick_helpers():
    """Extract ``_pick_vapls_command`` + ``_command_owner_id`` from the
    userbot source without importing the whole module (which would try to
    connect to Discord)."""
    src = (_USERBOT_DIR / "bot.py").read_text().splitlines()

    def _extract(name: str) -> str:
        start = next(
            i for i, line in enumerate(src)
            if line.startswith(f"def {name}(") or line.startswith(f"async def {name}(")
        )
        end = next(
            i for i, line in enumerate(src[start + 1:], start=start + 1)
            if line.startswith(("async def ", "def ", "class "))
        )
        return "\n".join(src[start:end])

    cfg = SimpleNamespace(VAPLS_BOT_ID=999_999)
    log_stub = logging.getLogger("test_vapls_bot_id")
    ns: dict = {
        "config": cfg,
        "log": log_stub,
        "Optional": object,  # used in type hint inside _command_owner_id
    }
    exec(_extract("_command_owner_id"), ns)
    exec(_extract("_pick_vapls_command"), ns)
    return ns["_pick_vapls_command"], cfg


_pick, _cfg = _load_pick_helpers()


def _cmd(name: str, owner_id: int):
    return SimpleNamespace(name=name, application_id=owner_id)


def test_pick_returns_command_when_owned_by_vapls():
    """Sanity: with a matching VaPls bot id, the helper returns the
    command and emits no error log."""
    cmds = [_cmd("play", 999_999), _cmd("play", 555)]
    found = _pick(cmds, "play")
    assert found is not None and found.application_id == 999_999


def test_pick_logs_error_when_no_match_owned_by_vapls(caplog):
    """The misconfiguration mode that hurts operators: /play *is* in the
    channel but every instance belongs to another bot. The log must be
    at error level so it survives normal log filtering and surfaces in
    journalctl without -p warning required."""
    cmds = [_cmd("play", 555), _cmd("play", 666)]
    with caplog.at_level(logging.WARNING, logger="test_vapls_bot_id"):
        found = _pick(cmds, "play")
    assert found is None

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, (
        "expected an ERROR-level log so the misconfig surfaces — "
        f"only got {[r.levelname for r in caplog.records]}"
    )
    # The message must mention the configured bot id so the operator can
    # cross-reference with the misconfigured value.
    assert any("999999" in r.getMessage() for r in error_records), (
        "expected the configured VAPLS_BOT_ID in the error message"
    )


def test_pick_returns_none_silently_when_no_candidates_at_all(caplog):
    """When the channel has zero /play commands (any bot), there's nothing
    to warn about — that's a channel-permission issue, not a misconfig.
    Don't spam error logs in this branch."""
    cmds = [_cmd("status", 999_999)]  # only an unrelated command
    with caplog.at_level(logging.WARNING, logger="test_vapls_bot_id"):
        assert _pick(cmds, "play") is None
    # No warning/error logs about VaPls ownership when the name itself
    # never matched.
    ownership_logs = [r for r in caplog.records if "owned by VaPls" in r.getMessage()]
    assert not ownership_logs

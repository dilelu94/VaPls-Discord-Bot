"""Behavior:
1. Normal messages require the text wake-word "indio" to trigger auto-reply.
2. Replies to OTHER users' messages ALSO require the text wake-word "indio".
3. ONLY replies directly to the Indio/VaPls bot messages trigger WITHOUT needing the text wake-word "indio".
"""

import re
import pytest

_INDIO_TEXT_WAKE_RE = re.compile(r"\bindio\b", re.IGNORECASE)
INDIO_USER_ID = 519594605520486428
VAPLS_BOT_ID = 1489830543074918482


def should_trigger_indio_autoreply(content: str, ref_author_id: int | None) -> bool:
    is_reply_to_indio = (
        ref_author_id in {INDIO_USER_ID, VAPLS_BOT_ID}
        if ref_author_id is not None
        else False
    )
    return is_reply_to_indio or bool(_INDIO_TEXT_WAKE_RE.search(content or ""))


def test_normal_message_without_indio_does_not_trigger():
    assert should_trigger_indio_autoreply("hola gente como andan", ref_author_id=None) is False


def test_normal_message_with_indio_triggers():
    assert should_trigger_indio_autoreply("hola indio como andas", ref_author_id=None) is True


def test_reply_to_human_without_indio_does_not_trigger():
    assert (
        should_trigger_indio_autoreply("jajaja tal cual", ref_author_id=285116759525031937)
        is False
    )


def test_reply_to_human_with_indio_triggers():
    assert (
        should_trigger_indio_autoreply(
            "que opinas de esto indio?", ref_author_id=285116759525031937
        )
        is True
    )


def test_reply_to_indio_without_indio_triggers():
    assert (
        should_trigger_indio_autoreply("sos un capo maquina", ref_author_id=INDIO_USER_ID)
        is True
    )


def test_reply_to_vapls_bot_without_indio_triggers():
    assert (
        should_trigger_indio_autoreply("gracias por la musica", ref_author_id=VAPLS_BOT_ID)
        is True
    )

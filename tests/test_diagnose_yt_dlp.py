"""Pin the behavior of ``_diagnoseYtDlpFailure``.

Lo que probamos es lo *observable*: cada categoría de stderr produce el
``audience`` esperado, el mensaje formateado tiene el next-step relevante para
quien puede resolverlo, y los casos ambiguos exponen tanto la pista para el
usuario como la del admin. No fijamos el wording exacto: el código puede
reescribir los mensajes mientras siga distinguiendo a quién apuntan.
"""
import pytest

from playCommand import _diagnoseYtDlpFailure


def _formatted(stderr, returncode=1):
    diag = _diagnoseYtDlpFailure(stderr, returncode)
    return diag, diag.format()


@pytest.mark.parametrize("stderr", [
    "ERROR: Sign in to confirm you're not a bot",
    "Please confirm you're not a bot",
])
def test_bot_check_targets_both_audiences(stderr):
    diag, msg = _formatted(stderr)
    assert diag.audience == "both"
    assert "[Usuario]" in msg and "[Admin]" in msg


def test_age_restricted_targets_both():
    diag, msg = _formatted("ERROR: Sign in to confirm your age")
    assert diag.audience == "both"
    assert "[Usuario]" in msg and "[Admin]" in msg


@pytest.mark.parametrize("stderr,fragment", [
    ("ERROR: Join this channel to get access to members-only content", "members-only"),
    ("This video is private", "privado"),
    ("Video unavailable", "disponible"),
    ("Premieres in 3 hours", "premiere"),
    ("This live event will begin in 10 minutes", "live"),
    ("Video is no longer available due to a copyright claim", "copyright"),
    ("ERROR: requested format is not available", "audio"),
])
def test_content_problems_are_user_only(stderr, fragment):
    diag, msg = _formatted(stderr)
    assert diag.audience == "user"
    assert "[Usuario]" in msg
    assert "[Admin]" not in msg
    assert fragment.lower() in msg.lower()


@pytest.mark.parametrize("stderr", [
    "ERROR: unable to extract player response",
    "No supported JavaScript runtime found",
    "Temporary failure in name resolution",
    "Connection refused",
])
def test_server_side_problems_are_admin_only(stderr):
    diag, msg = _formatted(stderr)
    assert diag.audience == "admin"
    assert "[Admin]" in msg
    assert "[Usuario]" not in msg


def test_http_429_surfaces_user_wait_and_admin_followup():
    diag, msg = _formatted("ERROR: HTTP Error 429: Too Many Requests")
    assert diag.audience == "admin"
    # 429 le da al usuario una pista de esperar y al admin contexto: ambos lados.
    assert "[Usuario]" in msg and "[Admin]" in msg


def test_http_403_is_ambiguous_and_shows_both():
    diag, msg = _formatted("ERROR: unable to download: HTTP Error 403: Forbidden")
    assert diag.audience == "both"
    assert "[Usuario]" in msg and "[Admin]" in msg


def test_empty_stderr_returncode_2_flags_missing_binary():
    diag, msg = _formatted("", returncode=2)
    assert diag.audience == "admin"
    assert "yt-dlp" in msg.lower()


def test_empty_stderr_other_returncode_is_admin():
    diag, msg = _formatted("", returncode=137)
    assert diag.audience == "admin"
    assert "[Admin]" in msg
    assert "137" in msg


def test_fallback_uses_last_nonempty_stderr_line():
    stderr = "noise line\n\nERROR: something exotic happened at frobnitz\n"
    diag, msg = _formatted(stderr)
    # Fallback es ambiguo por diseño: pista al usuario + tail al admin.
    assert diag.audience == "both"
    assert "frobnitz" in msg


def test_summary_is_short_for_logs():
    # ``summary`` lo usamos para logs y analytics — debe quedar acotado para no
    # spamear PostHog con stderrs gigantes.
    diag, _ = _formatted("ERROR: HTTP Error 429: Too Many Requests")
    assert len(diag.summary) < 200

"""Behavior: when a user with a configured ``disconnect_reaction`` disconnects
from a voice channel where the userbot is sitting, a random TTS phrase is
synthesized and played with a per-user probability roll.

No hardcoded user IDs — the reaction is driven entirely by the user's entry
in ``users.USERS``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loader (same pattern as test_userbot_greeting.py)
# ---------------------------------------------------------------------------
_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_userbot_greeting():
    real_config = sys.modules.get("config")

    uc_spec = importlib.util.spec_from_file_location(
        "userbot_config", _USERBOT_DIR / "config.py",
    )
    uc = importlib.util.module_from_spec(uc_spec)
    sys.modules["config"] = uc
    uc_spec.loader.exec_module(uc)

    try:
        g_spec = importlib.util.spec_from_file_location(
            "userbot_greeting", _USERBOT_DIR / "greeting.py",
        )
        g = importlib.util.module_from_spec(g_spec)
        g_spec.loader.exec_module(g)
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)
    return g, uc


greeting, ubcfg = _load_userbot_greeting()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    greeting._last_disconnect_reaction.clear()
    yield
    greeting._last_disconnect_reaction.clear()


@pytest.fixture
def fake_users(monkeypatch):
    """Inject a controlled users.USERS map for the duration of the test."""
    def _set(mapping):
        monkeypatch.setattr(greeting, "_users_map", lambda: mapping)
    return _set


def _make_vc(*, connected=True, playing=False):
    vc = MagicMock(name="VoiceClient")
    vc.is_connected = MagicMock(return_value=connected)
    vc.is_playing = MagicMock(return_value=playing)
    vc.play = MagicMock()
    vc.channel = SimpleNamespace(id=999)
    return vc


def _chalo_config(chance=0.20):
    return {
        "chance": chance,
        "phrases": [
            "Se re calentó el {name}.",
            "Se re calentó el puto de {name}.",
            "{name} se re calentó loco.",
        ],
    }


# ---------------------------------------------------------------------------
# get_user_disconnect_config
# ---------------------------------------------------------------------------

def test_returns_config_when_present(fake_users):
    """Users with disconnect_reaction return their config dict."""
    cfg = _chalo_config()
    fake_users({100: {"name": "Chalo", "disconnect_reaction": cfg}})
    result = greeting.get_user_disconnect_config(100)
    assert result is not None
    assert result["chance"] == 0.20
    assert len(result["phrases"]) == 3


def test_returns_none_when_absent(fake_users):
    """Users without disconnect_reaction return None."""
    fake_users({200: {"name": "Mila", "traits": []}})
    assert greeting.get_user_disconnect_config(200) is None


def test_returns_none_for_unknown_user(fake_users):
    """Unknown users return None."""
    fake_users({})
    assert greeting.get_user_disconnect_config(9999) is None


def test_returns_none_for_none_user_id(fake_users):
    fake_users({100: {"disconnect_reaction": _chalo_config()}})
    assert greeting.get_user_disconnect_config(None) is None


def test_returns_none_for_config_without_phrases(fake_users):
    """Config missing 'phrases' key is treated as absent."""
    fake_users({100: {"disconnect_reaction": {"chance": 0.5}}})
    assert greeting.get_user_disconnect_config(100) is None


# ---------------------------------------------------------------------------
# play_user_disconnect_reaction — probability roll
# ---------------------------------------------------------------------------

async def test_plays_when_random_roll_passes(fake_users, monkeypatch, tmp_path):
    """When random roll < chance, TTS is synthesized and audio is played."""
    fake_users({100: {"name": "Chalo", "disconnect_reaction": _chalo_config(chance=0.20)}})

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module
    synthesized = []

    def fake_generate(text, output_path=None):
        synthesized.append(text)
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    # Force roll to always pass
    monkeypatch.setattr(greeting.random, "random", lambda: 0.05)

    vc = _make_vc()
    result = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert result is True
    vc.play.assert_called_once()
    assert len(synthesized) == 1


async def test_skips_when_random_roll_fails(fake_users, monkeypatch, tmp_path):
    """When random roll >= chance, no TTS is generated and no audio is played."""
    fake_users({100: {"name": "Chalo", "disconnect_reaction": _chalo_config(chance=0.20)}})
    monkeypatch.setattr(greeting.random, "random", lambda: 0.90)

    vc = _make_vc()
    result = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert result is False
    vc.play.assert_not_called()


async def test_skips_user_without_disconnect_reaction(fake_users, monkeypatch):
    """Users without disconnect_reaction configured are silently skipped."""
    fake_users({200: {"name": "Mila"}})
    # Even if we force roll to pass, nothing happens
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    result = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=200)
    assert result is False
    vc.play.assert_not_called()


async def test_skips_bot_member(fake_users, monkeypatch):
    """Bot members are skipped even if configured."""
    fake_users({999: {"name": "GoLive", "disconnect_reaction": _chalo_config(chance=1.0)}})
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    bot_member = SimpleNamespace(bot=True, id=999)
    result = await greeting.play_user_disconnect_reaction(
        vc, channel_id=1, user_id=999, member=bot_member
    )
    assert result is False
    vc.play.assert_not_called()


# ---------------------------------------------------------------------------
# play_user_disconnect_reaction — phrase and name substitution
# ---------------------------------------------------------------------------

async def test_name_substituted_in_phrase(fake_users, monkeypatch, tmp_path):
    """The {name} placeholder in phrases is replaced with the user's resolved name."""
    fake_users({100: {"name": "Chalo", "disconnect_reaction": _chalo_config(chance=1.0)}})

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module
    synthesized_texts = []

    def fake_generate(text, output_path=None):
        synthesized_texts.append(text)
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)
    # Force the first phrase
    monkeypatch.setattr(greeting.random, "choice", lambda lst: lst[0])

    vc = _make_vc()
    await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)

    assert len(synthesized_texts) == 1
    assert "Chalo" in synthesized_texts[0]
    assert "{name}" not in synthesized_texts[0]


async def test_name_falls_back_to_member_display_name(fake_users, monkeypatch, tmp_path):
    """If user has no 'name' in USERS, member.display_name is used in phrase."""
    cfg = {"chance": 1.0, "phrases": ["Adiós {name}!"]}
    fake_users({300: {"disconnect_reaction": cfg}})

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module
    synthesized_texts = []

    def fake_generate(text, output_path=None):
        synthesized_texts.append(text)
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)
    monkeypatch.setattr(greeting.random, "choice", lambda lst: lst[0])

    member = SimpleNamespace(display_name="NombreEnDiscord", bot=False)
    vc = _make_vc()
    await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=300, member=member)

    assert synthesized_texts == ["Adiós NombreEnDiscord!"]


async def test_name_falls_back_to_usuario_if_no_name(fake_users, monkeypatch, tmp_path):
    """If no name can be resolved, 'usuario' is used as fallback."""
    cfg = {"chance": 1.0, "phrases": ["Chau {name}.."]}
    fake_users({400: {"disconnect_reaction": cfg}})

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module
    synthesized_texts = []

    def fake_generate(text, output_path=None):
        synthesized_texts.append(text)
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)
    monkeypatch.setattr(greeting.random, "choice", lambda lst: lst[0])

    vc = _make_vc()
    await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=400)

    assert synthesized_texts == ["Chau usuario.."]


# ---------------------------------------------------------------------------
# play_user_disconnect_reaction — throttling
# ---------------------------------------------------------------------------

async def test_throttle_blocks_second_call_within_window(fake_users, monkeypatch, tmp_path):
    """A second disconnect within DISCONNECT_REACTION_THROTTLE_SECONDS is suppressed."""
    fake_users({100: {"name": "Chalo", "disconnect_reaction": _chalo_config(chance=1.0)}})

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module

    def fake_generate(text, output_path=None):
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    first = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert first is True

    second = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert second is False
    assert vc.play.call_count == 1


async def test_throttle_is_per_user_channel(fake_users, monkeypatch, tmp_path):
    """Throttle is independent per (channel, user) pair."""
    cfg = {"chance": 1.0, "phrases": ["{name} se fue."]}
    fake_users({
        100: {"name": "Chalo", "disconnect_reaction": cfg},
        200: {"name": "Viny", "disconnect_reaction": cfg},
    })

    fake_wav = tmp_path / "reaction.wav"
    fake_wav.write_bytes(b"fake")

    import tts as _tts_module

    def fake_generate(text, output_path=None):
        fake_wav.write_bytes(b"fake")
        return str(fake_wav)

    monkeypatch.setattr(_tts_module, "generate_tts_wav", fake_generate)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    # Chalo fires on channel 1
    assert await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100) is True
    # Viny fires on channel 1 — different user, no throttle
    assert await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=200) is True
    # Chalo fires on channel 2 — different channel, no throttle
    assert await greeting.play_user_disconnect_reaction(vc, channel_id=2, user_id=100) is True
    assert vc.play.call_count == 3


# ---------------------------------------------------------------------------
# play_user_disconnect_reaction — edge cases
# ---------------------------------------------------------------------------

async def test_globally_disabled_skips(fake_users, monkeypatch):
    """If GREETING_ENABLED is False, disconnect reaction is suppressed."""
    monkeypatch.setattr(ubcfg, "GREETING_ENABLED", False)
    fake_users({100: {"disconnect_reaction": _chalo_config(chance=1.0)}})
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    result = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert result is False
    vc.play.assert_not_called()


async def test_tts_failure_returns_false(fake_users, monkeypatch):
    """If TTS generation fails (returns None), play_user_disconnect_reaction returns False."""
    fake_users({100: {"name": "Chalo", "disconnect_reaction": _chalo_config(chance=1.0)}})

    import tts as _tts_module
    monkeypatch.setattr(_tts_module, "generate_tts_wav", lambda *a, **kw: None)
    monkeypatch.setitem(sys.modules, "tts", _tts_module)
    monkeypatch.setattr(greeting.random, "random", lambda: 0.0)

    vc = _make_vc()
    result = await greeting.play_user_disconnect_reaction(vc, channel_id=1, user_id=100)
    assert result is False
    vc.play.assert_not_called()

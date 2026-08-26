"""Behavior: the userbot plays a per-user greeting only when ``users.USERS``
has an explicit ``greeting`` path for that user — never a default fallback —
and never twice in a row within the throttle window."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Load the userbot greeting module without polluting sys.path globally. We
# briefly register userbot/config.py under name "config" so `import config`
# inside greeting.py resolves to the userbot's config, then restore the main
# bot's config (already loaded by conftest) so other tests stay intact.
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


@pytest.fixture(autouse=True)
def _reset_throttle():
    greeting._last_greeting.clear()
    greeting._pity_state.clear()
    greeting._pity_loaded = True
    yield
    greeting._last_greeting.clear()
    greeting._pity_state.clear()
    greeting._pity_loaded = False


@pytest.fixture
def fake_users(monkeypatch):
    """Inject a controlled users.USERS map for the duration of the test."""
    def _set(mapping):
        monkeypatch.setattr(greeting, "_users_map", lambda: mapping)
    return _set


@pytest.fixture(autouse=True)
def _audio_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ubcfg, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ubcfg, "GREETING_PITY_PATH", str(tmp_path / "greeting_pity.json"))
    return tmp_path


def _make_vc(*, connected=True, playing=False):
    vc = MagicMock(name="VoiceClient")
    vc.is_connected = MagicMock(return_value=connected)
    vc.is_playing = MagicMock(return_value=playing)
    vc.play = MagicMock()
    vc.channel = SimpleNamespace(id=999)
    return vc


def test_user_with_greeting_resolves_to_absolute_path(fake_users, _audio_dir):
    fake_users({42: {"name": "Mati", "greeting": "Audios/fart.wav"}})
    path = greeting.resolve_greeting_path(42)
    assert path is not None
    assert path.endswith("Audios/fart.wav")
    assert str(_audio_dir) in path


def test_user_without_greeting_returns_none_no_default(fake_users):
    """KEY BEHAVIOR: no default fallback — users without explicit greeting do
    not trigger anything."""
    fake_users({211354006805676032: {"name": "Miles", "traits": []}})  # no greeting key
    assert greeting.resolve_greeting_path(211354006805676032) is None


def test_unknown_user_returns_none(fake_users):
    fake_users({})
    assert greeting.resolve_greeting_path(9999999) is None


def test_none_user_id_returns_none(fake_users):
    fake_users({0: {"greeting": "a.mp3"}})
    assert greeting.resolve_greeting_path(None) is None


async def test_play_skips_user_without_greeting(fake_users):
    fake_users({1: {"name": "noaudio"}})  # no greeting
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=1, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_play_invokes_vc_play_for_configured_user(
    fake_users, _audio_dir, monkeypatch,
):
    audio = _audio_dir / "Audios" / "test.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "Audios/test.mp3"}})
    # Stub FFmpegOpusAudio so we don't actually spawn ffmpeg.
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace(args=a, kwargs=k))
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is True
    vc.play.assert_called_once()


async def test_throttle_blocks_second_call_within_window(
    fake_users, _audio_dir, monkeypatch,
):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=7) is True
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=7) is False
    vc.play.assert_called_once()


async def test_throttle_is_per_channel(fake_users, _audio_dir, monkeypatch):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=1) is True
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=2) is True
    assert vc.play.call_count == 2


async def test_missing_audio_file_skipped(fake_users, monkeypatch):
    fake_users({42: {"greeting": "does/not/exist.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_already_playing_vc_skipped(fake_users, _audio_dir, monkeypatch):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc(playing=True)
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_disabled_globally_short_circuits(fake_users, monkeypatch):
    monkeypatch.setattr(ubcfg, "GREETING_ENABLED", False)
    fake_users({42: {"greeting": "g.mp3"}})
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


def test_pity_increases_effective_weight_for_rare_audio():
    items = [
        {"path": "common.mp3", "weight": 99},
        {"path": "rare_1pct.mp3", "weight": 1},
    ]
    # At 0 misses
    paths, weights, rare = greeting.calculate_effective_weights(items, user_id=10, pity_state={})
    assert paths == ["common.mp3", "rare_1pct.mp3"]
    assert weights == [99.0, 1.0]
    assert rare == {"rare_1pct.mp3"}

    # At 10 misses for rare_1pct
    paths, weights, _ = greeting.calculate_effective_weights(
        items, user_id=10, pity_state={"rare_1pct.mp3": 10}
    )
    assert weights == [99.0, 11.0]

    # At 99 misses for rare_1pct -> weight is 1 * (1 + 99) = 100 (more than common!)
    paths, weights, _ = greeting.calculate_effective_weights(
        items, user_id=10, pity_state={"rare_1pct.mp3": 99}
    )
    assert weights == [99.0, 100.0]


def test_common_audios_do_not_gain_pity():
    # 50/50 audios (base prob 50% > 5%)
    items = [
        {"path": "caro1.mp3", "weight": 50},
        {"path": "caro2.mp3", "weight": 50},
    ]
    paths, weights, rare = greeting.calculate_effective_weights(
        items, user_id=20, pity_state={"caro1.mp3": 50, "caro2.mp3": 50}
    )
    assert rare == set()
    assert weights == [50.0, 50.0]


def test_pity_counter_increments_on_miss_and_resets_on_hit(fake_users, monkeypatch):
    fake_users({
        30: {
            "greeting": [
                {"path": "common.mp3", "weight": 99},
                {"path": "rare.mp3", "weight": 1},
            ]
        }
    })

    # Simulate 3 rolls where common is picked every time
    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["common.mp3"])
    greeting.resolve_greeting_path(30)
    assert greeting._pity_state[30]["rare.mp3"] == 1

    greeting.resolve_greeting_path(30)
    assert greeting._pity_state[30]["rare.mp3"] == 2

    greeting.resolve_greeting_path(30)
    assert greeting._pity_state[30]["rare.mp3"] == 3

    # Now simulate rare is picked
    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["rare.mp3"])
    greeting.resolve_greeting_path(30)
    assert greeting._pity_state[30]["rare.mp3"] == 0


def test_multiple_rare_audios_track_independently(fake_users, monkeypatch):
    fake_users({
        40: {
            "greeting": [
                {"path": "common.mp3", "weight": 98},
                {"path": "rare_a.mp3", "weight": 1},
                {"path": "rare_b.mp3", "weight": 1},
            ]
        }
    })

    # 1. common picked -> both rare_a and rare_b increment
    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["common.mp3"])
    greeting.resolve_greeting_path(40)
    assert greeting._pity_state[40]["rare_a.mp3"] == 1
    assert greeting._pity_state[40]["rare_b.mp3"] == 1

    # 2. rare_a picked -> rare_a resets to 0, rare_b increments to 2
    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["rare_a.mp3"])
    greeting.resolve_greeting_path(40)
    assert greeting._pity_state[40]["rare_a.mp3"] == 0
    assert greeting._pity_state[40]["rare_b.mp3"] == 2


def test_pity_state_persists_to_disk_and_reloads(fake_users, tmp_path, monkeypatch):
    pity_file = tmp_path / "greeting_pity.json"
    monkeypatch.setattr(ubcfg, "GREETING_PITY_PATH", str(pity_file))

    fake_users({
        50: {
            "greeting": [
                {"path": "common.mp3", "weight": 99},
                {"path": "rare.mp3", "weight": 1},
            ]
        }
    })

    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["common.mp3"])
    greeting.resolve_greeting_path(50)
    assert pity_file.exists()

    # Clear memory and reload from disk
    greeting._pity_state.clear()
    greeting._pity_loaded = False
    loaded = greeting.load_pity_state(str(pity_file))
    assert loaded[50]["rare.mp3"] == 1


async def test_throttled_greeting_does_not_advance_pity(fake_users, _audio_dir, monkeypatch):
    audio = _audio_dir / "common.mp3"
    audio.write_bytes(b"fake")
    fake_users({
        60: {
            "greeting": [
                {"path": "common.mp3", "weight": 99},
                {"path": "rare.mp3", "weight": 1},
            ]
        }
    })
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())
    # Pin the random selection so common.mp3 is always picked (file exists),
    # regardless of member_count scaling affecting rare.mp3's effective weight.
    monkeypatch.setattr(greeting.random, "choices", lambda paths, weights, k=1: ["common.mp3"])
    vc = _make_vc()

    # First call succeeds
    assert await greeting.play_user_greeting(vc, user_id=60, channel_id=5) is True
    misses_after_first = greeting._pity_state.get(60, {}).get("rare.mp3", 0)

    # Second call is throttled -> pity counter should NOT change
    assert await greeting.play_user_greeting(vc, user_id=60, channel_id=5) is False
    assert greeting._pity_state.get(60, {}).get("rare.mp3", 0) == misses_after_first


def test_member_count_doubles_rare_audio_effective_weight_at_10_members():
    items = [
        {"path": "common.mp3", "weight": 99},
        {"path": "rare_1pct.mp3", "weight": 1},
    ]

    # At 1 member (solo join)
    _, weights_1, _ = greeting.calculate_effective_weights(
        items, user_id=10, pity_state={}, member_count=1
    )
    assert weights_1 == [99.0, 1.0]

    # At 10 members in voice channel -> rare weight doubles (2.0x)
    _, weights_10, _ = greeting.calculate_effective_weights(
        items, user_id=10, pity_state={}, member_count=10
    )
    assert weights_10 == [99.0, 2.0]

    # At 10 members with 4 misses: base_w (1.0) * (1 + 4) * 2.0 = 10.0
    _, weights_10_misses, _ = greeting.calculate_effective_weights(
        items, user_id=10, pity_state={"rare_1pct.mp3": 4}, member_count=10
    )
    assert weights_10_misses == [99.0, 10.0]


async def test_play_user_greeting_detects_channel_member_count(fake_users, _audio_dir, monkeypatch):
    ( _audio_dir / "common.mp3" ).write_bytes(b"fake")
    ( _audio_dir / "rare.mp3" ).write_bytes(b"fake")
    fake_users({
        70: {
            "greeting": [
                {"path": "common.mp3", "weight": 99},
                {"path": "rare.mp3", "weight": 1},
            ]
        }
    })
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", lambda *a, **k: SimpleNamespace())

    # Mock voice client with 10 human members in channel
    vc = _make_vc()
    members = [SimpleNamespace(id=i, bot=False) for i in range(10)]
    vc.channel.members = members

    captured_member_counts = []
    orig_calc = greeting.calculate_effective_weights

    def spy_calc(items, user_id, pity_state=None, rare_threshold=None, member_count=1):
        captured_member_counts.append(member_count)
        return orig_calc(items, user_id, pity_state, rare_threshold, member_count=member_count)

    monkeypatch.setattr(greeting, "calculate_effective_weights", spy_calc)

    played = await greeting.play_user_greeting(vc, user_id=70, channel_id=500)
    assert played is True
    assert captured_member_counts == [10]



"""Behavior: the userbot only follows a user that moves to a new voice
channel when its current channel becomes empty. If the current channel still
has humans, the userbot stays put."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_userbot_bot_helpers():
    """Load the userbot/bot.py module just enough to pull out the pure helpers
    (_channel_has_humans, _should_follow_user) without running discord setup.

    We can't import bot.py directly because top-level code creates a discord
    Client and patches voice_recv. So we read the source, extract the helper
    functions via exec into a clean namespace, and inject a minimal config
    stub for IGNORE_USER_IDS."""
    src_path = _USERBOT_DIR / "bot.py"
    src = src_path.read_text()
    # Stub modules the helpers depend on.
    config_stub = SimpleNamespace(IGNORE_USER_IDS=set())
    client_stub = SimpleNamespace(user=SimpleNamespace(id=999))
    ns = {
        "config": config_stub,
        "client": client_stub,
        "Optional": __import__("typing").Optional,
        "discord": SimpleNamespace(Guild=object),
    }
    # Extract only the two helper definitions by line markers.
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("def _channel_has_humans"))
    end = next(i for i, l in enumerate(lines[start + 1:], start=start + 1)
               if l.startswith("def ") and not l.startswith("def _channel_has_humans")
               and not l.startswith("def _should_follow_user"))
    # Re-find: we want from _channel_has_humans through end of _should_follow_user.
    end = None
    in_block = False
    for i, l in enumerate(lines):
        if l.startswith("def _channel_has_humans"):
            start = i
            in_block = True
            continue
        if in_block and l.startswith("def ") and not l.startswith(
            ("def _channel_has_humans", "def _should_follow_user")
        ):
            end = i
            break
    block = "\n".join(lines[start:end])
    exec(block, ns)
    return ns, config_stub, client_stub


_NS, _CFG, _CLIENT = _load_userbot_bot_helpers()
channel_has_humans = _NS["_channel_has_humans"]
should_follow = _NS["_should_follow_user"]


def _channel(*member_ids, channel_id=1, name="ch", guild=None):
    """Build a fake voice channel containing humans by id."""
    members = [SimpleNamespace(id=mid, bot=False) for mid in member_ids]
    return SimpleNamespace(
        id=channel_id, name=name, members=members, guild=guild,
    )


def _channel_with(members):
    """Build a channel with arbitrary fake member objects."""
    return SimpleNamespace(id=1, name="ch", members=members)


def test_empty_channel_has_no_humans():
    assert channel_has_humans(_channel()) is False


def test_channel_with_a_real_user_has_humans():
    assert channel_has_humans(_channel(123)) is True


def test_bot_in_channel_does_not_count_as_human():
    bot_member = SimpleNamespace(id=42, bot=True)
    assert channel_has_humans(_channel_with([bot_member])) is False


def test_self_in_channel_does_not_count_as_human():
    # Default self_id is 999 (from the client stub).
    self_member = SimpleNamespace(id=999, bot=False)
    assert channel_has_humans(_channel_with([self_member])) is False


def test_ignored_users_do_not_count(monkeypatch):
    monkeypatch.setattr(_CFG, "IGNORE_USER_IDS", {7}, raising=False)
    ignored = SimpleNamespace(id=7, bot=False)
    assert channel_has_humans(_channel_with([ignored])) is False


def test_self_muted_users_do_not_count():
    """A user who muted themselves isn't actively participating, so the bot
    shouldn't anchor on them when deciding whether to follow active movers."""
    muted = SimpleNamespace(
        id=5, bot=False,
        voice=SimpleNamespace(self_mute=True, mute=False),
    )
    assert channel_has_humans(_channel_with([muted])) is False


def test_server_muted_users_do_not_count():
    muted = SimpleNamespace(
        id=5, bot=False,
        voice=SimpleNamespace(self_mute=False, mute=True),
    )
    assert channel_has_humans(_channel_with([muted])) is False


def test_unmuted_user_alongside_muted_user_still_counts():
    """If at least one human in the channel is unmuted, the channel still
    has a participating human; the bot should stay."""
    muted = SimpleNamespace(
        id=5, bot=False,
        voice=SimpleNamespace(self_mute=True, mute=False),
    )
    active = SimpleNamespace(
        id=6, bot=False,
        voice=SimpleNamespace(self_mute=False, mute=False),
    )
    assert channel_has_humans(_channel_with([muted, active])) is True


def test_self_deafened_users_do_not_count():
    """A deafened user can't hear the bot or anyone else — for follow
    purposes they're as good as not in the channel."""
    deafened = SimpleNamespace(
        id=5, bot=False,
        voice=SimpleNamespace(
            self_mute=False, mute=False, self_deaf=True, deaf=False,
        ),
    )
    assert channel_has_humans(_channel_with([deafened])) is False


def test_server_deafened_users_do_not_count():
    deafened = SimpleNamespace(
        id=5, bot=False,
        voice=SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=True,
        ),
    )
    assert channel_has_humans(_channel_with([deafened])) is False


def test_user_without_voice_state_still_counts():
    """Defensive: a member entry without `.voice` (cache quirks) shouldn't
    silently disappear. Treat them as present."""
    member = SimpleNamespace(id=5, bot=False)  # no `voice` attribute
    assert channel_has_humans(_channel_with([member])) is True


def test_none_channel_has_no_humans():
    assert channel_has_humans(None) is False


# ----- _should_follow_user -------------------------------------------------


def test_follow_when_not_yet_in_any_channel():
    target = _channel(123, channel_id=2)
    assert should_follow(None, target) is True


def test_do_not_follow_when_current_channel_still_has_humans():
    """KEY BEHAVIOR: someone leaves the bot's channel, but others remain.
    Bot must NOT follow the leaver."""
    current = _channel(111, 222, channel_id=1)  # 111 and 222 are still here
    target = _channel(333, channel_id=2)
    assert should_follow(current, target) is False


def test_follow_when_current_channel_becomes_empty():
    current = _channel(channel_id=1)  # nobody left
    target = _channel(111, channel_id=2)
    assert should_follow(current, target) is True


def test_follow_when_only_bot_is_in_current_channel():
    self_member = SimpleNamespace(id=999, bot=False)
    current = SimpleNamespace(id=1, name="empty", members=[self_member])
    target = _channel(111, channel_id=2)
    assert should_follow(current, target) is True


def test_same_channel_is_a_noop_follow():
    """Re-joining the same channel (e.g. a server-side reconnect) returns True
    so the join path can re-arm the listener/greeting without a stay-put log."""
    current = _channel(111, channel_id=1)
    target = _channel(111, channel_id=1)
    assert should_follow(current, target) is True


def test_no_target_channel_means_do_not_move():
    current = _channel(111, channel_id=1)
    assert should_follow(current, None) is False


def test_follow_out_of_afk_channel_even_if_it_has_humans():
    """If the userbot is parked in the guild's AFK channel, it should always
    follow a user moving to a non-AFK channel — even if other (AFK) humans
    are still there. The AFK channel is for idle users; the bot shouldn't
    treat it as the room to anchor on."""
    afk = SimpleNamespace(id=1)
    guild = SimpleNamespace(afk_channel=afk)
    current = _channel(111, 222, channel_id=1, name="afk", guild=guild)
    # Patch the AFK reference to point at the same object we'll use as current.
    guild.afk_channel = current
    target = _channel(333, channel_id=2, name="general", guild=guild)
    assert should_follow(current, target) is True


def test_non_afk_channel_with_humans_still_blocks_follow():
    """Sanity check: the AFK exception only fires when the bot is *in* the
    AFK channel. A normal channel with other humans still blocks follow."""
    afk = SimpleNamespace(id=99, members=[])
    guild = SimpleNamespace(afk_channel=afk)
    current = _channel(111, 222, channel_id=1, name="general", guild=guild)
    target = _channel(333, channel_id=2, name="other", guild=guild)
    assert should_follow(current, target) is False


def test_guild_without_afk_channel_does_not_crash():
    """Guilds may have no AFK channel configured; the helper must handle
    `guild.afk_channel = None` cleanly."""
    guild = SimpleNamespace(afk_channel=None)
    current = _channel(111, 222, channel_id=1, name="general", guild=guild)
    target = _channel(333, channel_id=2, name="other", guild=guild)
    assert should_follow(current, target) is False

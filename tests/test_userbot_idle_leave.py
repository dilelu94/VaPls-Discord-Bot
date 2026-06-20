"""Behavior: the userbot stays in the channel for IDLE_LEAVE_SECONDS after
the guild goes quiet, then disconnects. Any human (re)joining cancels the
pending disconnect. The wake-up re-checks the state so a race-late join
doesn't get abandoned. Timers are kept per-guild so multiple guilds don't
interfere."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


# ---------- Source extraction ----------------------------------------------


def _extract_func_block(src_lines: list[str], names: list[str]) -> str:
    """Pull each named top-level function definition out of ``src_lines``.

    Picks every contiguous run starting with ``def <name>(`` or
    ``async def <name>(`` and ending at the next top-level ``def``.
    """
    out: list[str] = []
    i = 0
    while i < len(src_lines):
        line = src_lines[i]
        matched = None
        for name in names:
            if line.startswith(f"def {name}(") or line.startswith(f"async def {name}("):
                matched = name
                break
        if matched is None:
            i += 1
            continue
        start = i
        i += 1
        # Consume the function body: any indented or blank line.
        while i < len(src_lines):
            ln = src_lines[i]
            if ln and not ln.startswith((" ", "\t")):
                # Top-level statement — end of body.
                if (
                    ln.startswith("def ")
                    or ln.startswith("async def ")
                    or ln.startswith("@")
                    or ln.startswith("class ")
                ):
                    break
                # A top-level non-def line also ends the body.
                break
            i += 1
        out.extend(src_lines[start:i])
        out.append("")
    return "\n".join(out)


def _load_idle_leave_helpers(idle_seconds: float = 0.05):
    src = (_USERBOT_DIR / "bot.py").read_text()
    lines = src.splitlines()

    voice_clients_list: list = []

    config_stub = SimpleNamespace(
        IGNORE_USER_IDS=set(),
        IDLE_LEAVE_SECONDS=idle_seconds,
    )
    client_stub = SimpleNamespace(
        user=SimpleNamespace(id=999),
        voice_clients=voice_clients_list,
    )
    log_stub = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )

    ns = {
        "config": config_stub,
        "client": client_stub,
        "asyncio": asyncio,
        "log": log_stub,
        "Optional": __import__("typing").Optional,
        "discord": SimpleNamespace(Guild=object),
        "voice_recv": SimpleNamespace(VoiceRecvClient=object),
        "_idle_leave_tasks": {},
        "_active_streams": {},
    }

    block = _extract_func_block(
        lines,
        [
            "_vc_for_guild",
            "_channel_has_humans",
            "_guild_has_humans",
            "_cancel_idle_leave",
            "_idle_leave_after_delay",
            "_schedule_idle_leave",
            "_leave_if_empty",
        ],
    )
    exec(block, ns)
    return ns, config_stub, client_stub, voice_clients_list


# ---------- Test fixtures ---------------------------------------------------


class _FakeVoiceClient:
    """Minimal VoiceClient stand-in: records disconnect/stop_listening
    invocations so tests can verify what happened."""

    def __init__(self, guild):
        self.guild = guild
        self.disconnected = False
        self.disconnect_args: dict = {}
        self.listening = True

    def is_listening(self):
        return self.listening

    def stop_listening(self):
        self.listening = False

    async def disconnect(self, **kwargs):
        self.disconnected = True
        self.disconnect_args = kwargs


def _make_guild(*, name="g", guild_id=1, channels=None):
    return SimpleNamespace(
        id=guild_id,
        name=name,
        voice_channels=list(channels or []),
    )


def _make_channel(*member_ids, channel_id=1):
    members = [SimpleNamespace(id=mid, bot=False) for mid in member_ids]
    return SimpleNamespace(id=channel_id, members=members)


@pytest.fixture
def env():
    """Fresh idle-leave namespace per test."""
    ns, cfg, client, voice_clients = _load_idle_leave_helpers(idle_seconds=0.05)
    yield ns, cfg, client, voice_clients
    # Cancel any leftover tasks so they don't bleed into other tests.
    for t in list(ns["_idle_leave_tasks"].values()):
        if not t.done():
            t.cancel()


# ---------- Behaviors -------------------------------------------------------


async def test_empty_guild_schedules_a_leave_task(env):
    ns, _cfg, _client, voice_clients = env
    guild = _make_guild(channels=[_make_channel()])  # empty channel
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    await ns["_leave_if_empty"](guild)
    # A task is now pending for this guild.
    assert guild.id in ns["_idle_leave_tasks"]
    assert not ns["_idle_leave_tasks"][guild.id].done()


async def test_guild_with_humans_does_not_schedule_anything(env):
    ns, _cfg, _client, voice_clients = env
    guild = _make_guild(channels=[_make_channel(111, channel_id=1)])
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    await ns["_leave_if_empty"](guild)
    assert guild.id not in ns["_idle_leave_tasks"]


async def test_pending_leave_is_cancelled_when_someone_rejoins(env):
    """The pending disconnect must not fire if a human comes back before
    the delay elapses. We cancel the task by calling the same code path
    that `on_voice_state_update` uses when a join is observed."""
    ns, _cfg, _client, voice_clients = env
    guild = _make_guild(channels=[_make_channel()])  # empty
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    await ns["_leave_if_empty"](guild)
    # Someone rejoins → handler calls _cancel_idle_leave.
    ns["_cancel_idle_leave"](guild.id)
    await asyncio.sleep(0.1)  # well past the 0.05s delay
    assert vc.disconnected is False
    assert guild.id not in ns["_idle_leave_tasks"]


async def test_disconnect_fires_when_still_empty_after_delay(env):
    ns, _cfg, _client, voice_clients = env
    guild = _make_guild(channels=[_make_channel()])  # empty
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    await ns["_leave_if_empty"](guild)
    await asyncio.sleep(0.1)
    assert vc.disconnected is True
    # Task cleaned up after firing.
    assert guild.id not in ns["_idle_leave_tasks"]


async def test_disconnect_skipped_if_humans_returned_before_wakeup(env):
    """Race safety: the cancellation from on_voice_state_update might
    arrive milliseconds late. The task itself re-checks the state and
    must skip the disconnect when it finds the guild non-empty."""
    ns, _cfg, _client, voice_clients = env
    empty_channel = _make_channel(channel_id=1)
    guild = _make_guild(channels=[empty_channel])
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    await ns["_leave_if_empty"](guild)
    # Before the delay elapses, a human appears in the guild (simulate by
    # mutating the channel's member list — exactly what Discord's state
    # cache does when a user joins).
    empty_channel.members.append(SimpleNamespace(id=111, bot=False))
    await asyncio.sleep(0.1)
    assert vc.disconnected is False


async def test_timers_are_per_guild_and_do_not_interfere(env):
    """Two guilds, both empty, each schedules its own timer. Cancelling
    one must not cancel the other."""
    ns, _cfg, _client, voice_clients = env
    guild_a = _make_guild(name="A", guild_id=1, channels=[_make_channel()])
    guild_b = _make_guild(name="B", guild_id=2, channels=[_make_channel()])
    vc_a = _FakeVoiceClient(guild_a)
    vc_b = _FakeVoiceClient(guild_b)
    voice_clients.extend([vc_a, vc_b])

    await ns["_leave_if_empty"](guild_a)
    await ns["_leave_if_empty"](guild_b)
    assert guild_a.id in ns["_idle_leave_tasks"]
    assert guild_b.id in ns["_idle_leave_tasks"]

    # Cancel A only; B should still fire.
    ns["_cancel_idle_leave"](guild_a.id)
    await asyncio.sleep(0.1)
    assert vc_a.disconnected is False
    assert vc_b.disconnected is True


async def test_re_scheduling_replaces_previous_task(env):
    """Calling _schedule_idle_leave twice for the same guild must leave
    exactly one pending task — the second call cancels the first."""
    ns, _cfg, _client, voice_clients = env
    guild = _make_guild(channels=[_make_channel()])
    vc = _FakeVoiceClient(guild)
    voice_clients.append(vc)

    ns["_schedule_idle_leave"](guild)
    first_task = ns["_idle_leave_tasks"][guild.id]
    ns["_schedule_idle_leave"](guild)
    second_task = ns["_idle_leave_tasks"][guild.id]
    assert first_task is not second_task
    # Let the event loop process the cancellation queued for first_task.
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()
    # Second task fires the disconnect cleanly.
    await asyncio.sleep(0.1)
    assert vc.disconnected is True

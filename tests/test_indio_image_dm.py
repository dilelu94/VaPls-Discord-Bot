"""Behavioral tests for Indio DM image collection access gate.

The Indio must only accept images via DM from users who are members of
a specific guild AND have the 'Main Characters' role. This module tests the
pure check function ``_can_send_indio_images`` so the requirement cannot be
silently removed.
"""

from unittest.mock import MagicMock

from geminiCommand import _can_send_indio_images, _INDIO_IMAGE_ROLE


def _make_member(role_names: list[str]):
    """Build a fake discord.Member with the given role names."""
    member = MagicMock()
    member.roles = []
    for name in role_names:
        r = MagicMock()
        r.name = name
        member.roles.append(r)
    return member


def test_gate_allows_user_with_role():
    member = _make_member([_INDIO_IMAGE_ROLE])
    ok, err = _can_send_indio_images(member)
    assert ok is True
    assert err == ""


def test_gate_denies_user_without_role():
    member = _make_member(["Everyone", "Nuevo"])
    ok, err = _can_send_indio_images(member)
    assert ok is False
    assert _INDIO_IMAGE_ROLE in err


def test_gate_denies_non_guild_member():
    ok, err = _can_send_indio_images(None)
    assert ok is False
    assert "guild" in err.lower()


def test_gate_custom_role_name():
    member = _make_member(["VIP"])
    ok, _ = _can_send_indio_images(member, role_name="VIP")
    assert ok is True

    ok, _ = _can_send_indio_images(member, role_name="Admin")
    assert ok is False

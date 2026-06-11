"""Behavioral tests for /spacewar: response content and ephemeral flag.

Covers:
- Response is ephemeral
- Response mentions Spacewar
- Response mentions steam://run/480
- Response includes Linux/Deck and Bazzite commands
"""

from conftest import sent_text, ephemeral_for


async def test_spacewar_is_ephemeral(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    assert ctx.sent_messages, "expected at least one message"
    assert ctx.sent_ephemeral[0] is True, "response should be ephemeral"


async def test_spacewar_contains_key_content(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "Spacewar" in text
    assert "steam://run/480" in text
    assert "Linux" in text or "Steam Deck" in text


async def test_spacewar_includes_linux_command(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "steam steam://run/480" in text


async def test_spacewar_includes_bazzite_command(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "Bazzite" in text or "flatpak" in text

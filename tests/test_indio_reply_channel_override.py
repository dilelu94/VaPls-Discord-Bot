"""Behavior: cuando INDIO_REPLY_CHANNEL_ID esta seteado, todas las respuestas
del Indio aterrizan en ese canal — sin importar donde se disparo el trigger
(slash command, wake-word de texto, voz, HTTP). Cuando esta en 0 (default en
los tests via conftest), el comportamiento clasico se preserva."""
import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from geminiCommand import indioLogic, indioFromVoice


async def _drain():
    current = asyncio.current_task()
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _fake_target_channel(channel_id=9999, guild_id=100):
    """Return a fake Discord channel with async .send() recording its messages
    and a .guild.id attribute (used by indioFromVoice's override resolver)."""
    sent: list[str] = []
    _msg_id = [5000]

    async def _send(content=None, **kwargs):
        _msg_id[0] += 1
        sent.append(content)
        return types.SimpleNamespace(
            id=_msg_id[0],
            channel=types.SimpleNamespace(id=channel_id),
        )

    chan = MagicMock(name=f"TargetChannel({channel_id})")
    chan.id = channel_id
    chan.send = AsyncMock(side_effect=_send)
    chan.guild = types.SimpleNamespace(id=guild_id)
    chan.sent_messages = sent
    return chan


async def test_indioLogic_redirects_reply_to_override_channel(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    """When INDIO_REPLY_CHANNEL_ID resolves to a real channel, the reply text
    appears in that channel — not in the slash command's followup."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    # Disable relay so the test exercises direct channel.send (not _relay_to_userbot).
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="todo tranqui che"))

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    ctx = ctx_factory(display_name="Mati", guild_id=100)
    ctx.bot = MagicMock()
    ctx.bot.get_channel = MagicMock(return_value=target)

    await indioLogic(ctx, "que onda", nuevo=False)
    await _drain()

    # The Indio's text reached the target channel.
    assert any("todo tranqui che" in (m or "") for m in target.sent_messages)
    # And NOT via the slash command's followup (the user header should also
    # live in the target channel, so ctx.sent_messages stays empty).
    assert all("todo tranqui che" not in (m or "") for m in ctx.sent_messages)


async def test_indioLogic_falls_back_when_override_channel_not_resolvable(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    """If INDIO_REPLY_CHANNEL_ID is set but bot.get_channel returns None (bot
    doesn't see the channel), fall back to posting via ctx.followup so the user
    still gets an answer instead of silence."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="igual te respondo"))

    ctx = ctx_factory(guild_id=100)
    ctx.bot = MagicMock()
    ctx.bot.get_channel = MagicMock(return_value=None)

    await indioLogic(ctx, "hola", nuevo=False)
    await _drain()

    # Reply still reached the user — just via the slash command's followup.
    assert any("igual te respondo" in (m or "") for m in ctx.sent_messages)


async def test_indioFromVoice_redirects_to_override_channel(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word de texto, voz, y HTTP-direct desembocan en indioFromVoice.
    Con el override seteado, la respuesta debe aparecer en el canal override
    aunque el caller pase otro channel_id."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="ahi te explico"))

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    original_chan = _fake_target_channel(channel_id=111, guild_id=100)

    bot = MagicMock()
    # When override resolves, indioFromVoice asks bot.get_channel(override_id).
    # When the function later resolves the guild it'll ask guild.get_channel
    # too, so we wire both to return the target.
    def _get_channel(cid):
        if cid == 9999:
            return target
        if cid == 111:
            return original_chan
        return None
    bot.get_channel = MagicMock(side_effect=_get_channel)

    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(side_effect=_get_channel)
    guild.emojis = []
    member = types.SimpleNamespace(id=42, display_name="Tobi", name="tobi")
    guild.get_member = MagicMock(return_value=member)
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=111,
        pregunta="che indio que onda", speaker_name="Tobi",
    )
    await _drain()

    # Reply landed in the override target — not in the original channel passed
    # by the wake-word dispatcher.
    assert any("ahi te explico" in (m or "") for m in target.sent_messages)
    assert all("ahi te explico" not in (m or "") for m in original_chan.sent_messages)


async def test_indioFromVoice_does_not_spam_source_channel_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """Cuando el wake-word dispara desde otro canal, el bot NO debe postear
    nada visible en el canal original — toda la conversacion se mueve al
    target para evitar ruido fuera del canal designado."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="todo en el target"))

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    source = _fake_target_channel(channel_id=555, guild_id=100)

    bot = MagicMock()

    def _get_channel(cid):
        return {9999: target, 555: source}.get(cid)
    bot.get_channel = MagicMock(side_effect=_get_channel)

    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(side_effect=_get_channel)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(
            id=42, display_name="Tobi", name="tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="indio que onda", speaker_name="Tobi",
    )
    await _drain()

    assert source.sent_messages == [], (
        f"source channel got unexpected messages: {source.sent_messages!r}"
    )
    # And the actual answer landed in the target.
    assert any("todo en el target" in (m or "") for m in target.sent_messages)


async def test_indioFromVoice_posts_user_mention_header_in_target_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """Para que el user reciba notificacion en el target, el bot debe postear
    un header que pingee a <@user_id> justo antes de la respuesta."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="ahi te explico todo"))

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    source = _fake_target_channel(channel_id=555, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(
        side_effect=lambda cid: {9999: target, 555: source}.get(cid))
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = bot.get_channel
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="bibi como anda?", speaker_name="Tobi",
    )
    await _drain()

    # Header arrived in target before the reply, and pings the user.
    target_text = "\n".join(m or "" for m in target.sent_messages)
    assert "<@42>" in target_text, (
        f"expected user mention in target, got: {target_text!r}"
    )
    # The question itself is quoted in the header so the answer has context.
    assert "bibi como anda?" in target_text


async def test_indioFromVoice_no_header_when_source_equals_target(
        indio, patch_generate, reply_factory, monkeypatch):
    """Cuando el wake-word ya cae en el target (no hay redirect), no hace
    falta pingear al user — esta ahi mismo viendo la respuesta. El header
    seria ruido."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="tranqui"))

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=target)
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=target)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=9999,
        pregunta="hola", speaker_name="Tobi",
    )
    await _drain()

    target_text = "\n".join(m or "" for m in target.sent_messages)
    assert "<@42>" not in target_text, (
        "no user mention expected when source == target"
    )
    assert "tranqui" in target_text


async def test_indioFromVoice_dms_user_with_forwarded_reply_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """Cuando la respuesta se mueve a otro canal, el userbot relay /dm
    le forwardea la respuesta + un puntero al canal target — asi el user
    se entera por DM (lo mas cercano a un ephemeral en el wake-word path).
    """
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="acordate que el bibi anda mal"))

    dm_calls: list[tuple[int, str]] = []

    async def _fake_dm(user_id, content):
        dm_calls.append((user_id, content))
        return True

    monkeypatch.setattr(gc, "_relay_dm_user", _fake_dm)

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    source = _fake_target_channel(channel_id=555, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(
        side_effect=lambda cid: {9999: target, 555: source}.get(cid))
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = bot.get_channel
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="indio", speaker_name="Tobi",
    )
    await _drain()

    assert len(dm_calls) == 1, f"expected exactly one DM, got {dm_calls!r}"
    sent_user_id, sent_content = dm_calls[0]
    assert sent_user_id == 42
    # The DM should pointer at the target channel and include the actual answer.
    assert "9999" in sent_content
    assert "acordate que el bibi anda mal" in sent_content


async def test_indioFromVoice_no_dm_when_source_equals_target(
        indio, patch_generate, reply_factory, monkeypatch):
    """Sin redirect (el wake-word cayo en el mismo canal target), no se manda
    DM — el user esta ahi viendo la respuesta y un DM seria spam."""
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="ya estoy aca"))

    dm_calls: list = []

    async def _fake_dm(user_id, content):
        dm_calls.append((user_id, content))
        return True

    monkeypatch.setattr(gc, "_relay_dm_user", _fake_dm)

    target = _fake_target_channel(channel_id=9999, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=target)
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=target)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=9999,
        pregunta="hola", speaker_name="Tobi",
    )
    await _drain()

    assert dm_calls == [], f"unexpected DM(s): {dm_calls!r}"


async def test_indioFromVoice_falls_back_when_override_unresolvable(
        indio, patch_generate, reply_factory, monkeypatch):
    """Si el override no se puede resolver, la respuesta cae al canal original
    (el del trigger) en vez de perderse en silencio."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 8888, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="rebote ok"))

    original_chan = _fake_target_channel(channel_id=222, guild_id=100)
    bot = MagicMock()

    def _get_channel(cid):
        if cid == 222:
            return original_chan
        return None  # 8888 doesn't resolve
    bot.get_channel = MagicMock(side_effect=_get_channel)

    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(side_effect=_get_channel)
    guild.emojis = []
    member = types.SimpleNamespace(id=42, display_name="Tobi", name="tobi")
    guild.get_member = MagicMock(return_value=member)
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=222,
        pregunta="hola", speaker_name="Tobi",
    )
    await _drain()

    # Reply lands in the original channel because the override was unusable.
    assert any("rebote ok" in (m or "") for m in original_chan.sent_messages)

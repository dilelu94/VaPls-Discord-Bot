"""Behavior: cuando INDIO_REPLY_CHANNEL_ID esta seteado, las respuestas del
Indio del path de TEXTO (slash /indio, wake-word de texto, HTTP con
is_voice=False) aterrizan en ese canal. La wake-word de VOZ
(from_voice=True) queda exenta del override: la respuesta cae en el
channel_id provisto por el caller (transcript channel del userbot), sin
header/DM/delete del fuente.

Cuando INDIO_REPLY_CHANNEL_ID esta en 0 (default en los tests via conftest)
el comportamiento clasico se preserva en todos los paths."""
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
    """Wake-word de texto y HTTP-direct (is_voice=False) desembocan en
    indioFromVoice con from_voice=False. Con el override seteado, la respuesta
    debe aparecer en el canal override aunque el caller pase otro channel_id.
    La path de voz (from_voice=True) tiene su propio test que verifica el
    skip del override."""
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


async def test_indioFromVoice_dms_user_with_link_to_target_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """Cuando la respuesta se mueve a otro canal, el userbot relay /dm le
    manda al user un mensaje cortito: link al mensaje en el target + el
    emoji :ElIndio:. NO se forwardea el contenido completo de la respuesta
    (eso seria duplicado — el mensaje del Indio ya esta en el target)."""
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
    # The DM contains a discord.com message link pointing at the target.
    assert "discord.com/channels/100/9999/" in sent_content
    # The DM should NOT carry the full reply text (the answer lives in target).
    assert "acordate que el bibi anda mal" not in sent_content
    # No custom :ElIndio: emoji — custom server emojis don't render in DMs,
    # they appear as literal ":ElIndio:" which looks broken.
    assert "ElIndio" not in sent_content


async def test_indioFromVoice_deletes_source_message_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """When the wake-word triggered from another channel and a source
    message id was provided, the bot deletes the original message so the
    source channel stays clean. Best-effort: a missing-permissions error
    must not break the rest of the flow."""
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    monkeypatch.setattr(gc, "_relay_dm_user", AsyncMock(return_value=True))
    patch_generate(reply=reply_factory(text="ok"))

    # Source channel with a get_partial_message() that records what got asked
    # to delete; the partial message's delete() succeeds quietly.
    deleted_ids: list[int] = []

    class _PartialMsg:
        def __init__(self, mid):
            self.id = mid

        async def delete(self):
            deleted_ids.append(self.id)

    source = _fake_target_channel(channel_id=555, guild_id=100)
    source.get_partial_message = MagicMock(side_effect=lambda mid: _PartialMsg(mid))
    target = _fake_target_channel(channel_id=9999, guild_id=100)

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
        source_message_id=12345,
    )
    await _drain()

    assert deleted_ids == [12345], (
        f"expected delete of source message 12345, got {deleted_ids!r}"
    )


async def test_indioFromVoice_swallows_source_delete_failure(
        indio, patch_generate, reply_factory, monkeypatch):
    """If deleting the source message fails (e.g. missing Manage Messages),
    the rest of the flow still runs — the reply lands in the target and
    the DM goes out. The user is never left hanging because of a perms
    issue in the source channel."""
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    dm_calls: list = []

    async def _fake_dm(user_id, content):
        dm_calls.append((user_id, content))
        return True

    monkeypatch.setattr(gc, "_relay_dm_user", _fake_dm)
    patch_generate(reply=reply_factory(text="igual respondo"))

    class _BoomPartial:
        async def delete(self):
            raise PermissionError("Manage Messages required")

    source = _fake_target_channel(channel_id=555, guild_id=100)
    source.get_partial_message = MagicMock(return_value=_BoomPartial())
    target = _fake_target_channel(channel_id=9999, guild_id=100)

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
        source_message_id=12345,
    )
    await _drain()

    # Reply still arrived in target.
    assert any("igual respondo" in (m or "") for m in target.sent_messages)
    # And DM still went out.
    assert len(dm_calls) == 1


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


async def test_indioFromVoice_replies_to_user_message_when_no_redirect(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word en el mismo canal target: la respuesta del Indio usa la
    feature de Discord "responder al mensaje" apuntando al mensaje original
    del user (el wake-word), asi queda atado visualmente en el hilo."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="ahi te explico"))

    sent_kwargs: list[dict] = []

    async def _capture_send(content=None, **kw):
        sent_kwargs.append(kw)
        return types.SimpleNamespace(
            id=6000 + len(sent_kwargs),
            channel=types.SimpleNamespace(id=9999),
        )

    target = MagicMock()
    target.id = 9999
    target.guild = types.SimpleNamespace(id=100)
    target.send = AsyncMock(side_effect=_capture_send)

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
        source_message_id=88888,
    )
    await _drain()

    # The first send carries a reference back to the user's wake-word message.
    assert sent_kwargs, "expected at least one send"
    ref = sent_kwargs[0].get("reference")
    assert ref is not None, f"first send had no reply reference: {sent_kwargs[0]!r}"
    assert int(getattr(ref, "message_id", 0)) == 88888


async def test_indioFromVoice_does_not_self_reply_to_header_when_redirected(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word desde otro canal: el header lo postea el mismo bot, asi
    que el reply del Indio NO debe usar Discord "reply" — seria un
    auto-reply (Indio contestandose a si mismo). El header queda visible
    arriba sin hilo formal."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="todo en orden"))

    sent_calls: list[dict] = []

    async def _capture_send(content=None, **kw):
        sent_calls.append({"content": content, **kw})
        return types.SimpleNamespace(
            id=7000 + len(sent_calls),
            channel=types.SimpleNamespace(id=9999),
        )

    target = MagicMock()
    target.id = 9999
    target.guild = types.SimpleNamespace(id=100)
    target.send = AsyncMock(side_effect=_capture_send)
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
        pregunta="que onda?", speaker_name="Tobi",
    )
    await _drain()

    assert len(sent_calls) >= 2, f"expected header + reply sends, got {sent_calls!r}"
    header_call = sent_calls[0]
    reply_call = sent_calls[1]
    # Header is posted plain.
    assert "preguntó" in (header_call.get("content") or "")
    # The Indio's reply does NOT carry a reference back to the header —
    # that would render as the bot replying to itself.
    assert reply_call.get("reference") is None, (
        f"unexpected self-reply reference: {reply_call!r}"
    )


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


# ---------------------------------------------------------------------------
# Voice path: from_voice=True exenta del override INDIO_REPLY_CHANNEL_ID
# ---------------------------------------------------------------------------


async def test_indioFromVoice_skips_override_when_from_voice(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word de voz: aunque INDIO_REPLY_CHANNEL_ID este seteado, la
    respuesta queda en el channel_id que mando el caller (transcript
    channel del userbot) — no se redirige al override."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="hablo en el transcript"))

    transcript_chan = _fake_target_channel(channel_id=555, guild_id=100)
    override_chan = _fake_target_channel(channel_id=9999, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(
        side_effect=lambda cid: {9999: override_chan, 555: transcript_chan}.get(cid))
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
        pregunta="che indio que onda", speaker_name="Tobi",
        from_voice=True,
    )
    await _drain()

    assert any("hablo en el transcript" in (m or "")
               for m in transcript_chan.sent_messages)
    assert override_chan.sent_messages == [], (
        f"override channel should be untouched, got: {override_chan.sent_messages!r}"
    )


async def test_indioFromVoice_no_header_when_from_voice(
        indio, patch_generate, reply_factory, monkeypatch):
    """from_voice=True: sin override no hay redirect, asi que no se postea
    el header de '<@user> pregunto:' — el speaker esta ahi mismo en el
    transcript channel viendo su propio mensaje del bot."""
    import config
    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="sin header"))

    transcript_chan = _fake_target_channel(channel_id=555, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=transcript_chan)
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=transcript_chan)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="che indio que onda", speaker_name="Tobi",
        from_voice=True,
    )
    await _drain()

    chan_text = "\n".join(m or "" for m in transcript_chan.sent_messages)
    assert "<@42>" not in chan_text, (
        f"unexpected user-mention header in voice path: {chan_text!r}"
    )
    assert "preguntó" not in chan_text, (
        f"unexpected 'preguntó' header in voice path: {chan_text!r}"
    )
    assert "sin header" in chan_text


async def test_indioFromVoice_no_dm_when_from_voice(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word de voz no manda DM al user — la respuesta ya esta en el
    transcript channel donde el userbot publica las transcripciones, no hay
    nada que avisar."""
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    patch_generate(reply=reply_factory(text="no DM"))

    dm_calls: list = []

    async def _fake_dm(user_id, content):
        dm_calls.append((user_id, content))
        return True

    monkeypatch.setattr(gc, "_relay_dm_user", _fake_dm)

    transcript_chan = _fake_target_channel(channel_id=555, guild_id=100)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=transcript_chan)
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=transcript_chan)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="che indio", speaker_name="Tobi",
        from_voice=True,
    )
    await _drain()

    assert dm_calls == [], f"unexpected DM in voice path: {dm_calls!r}"


async def test_indioFromVoice_no_source_delete_when_from_voice(
        indio, patch_generate, reply_factory, monkeypatch):
    """Wake-word de voz no borra el mensaje fuente — el userbot solo manda
    transcript_message_id en el dispatch, source_message_id queda None, y
    aunque viniera seteado tampoco se debe borrar (no hay redirect)."""
    import config
    import geminiCommand as gc

    monkeypatch.setattr(config, "INDIO_REPLY_CHANNEL_ID", 9999, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    monkeypatch.setattr(gc, "_relay_dm_user", AsyncMock(return_value=True))
    patch_generate(reply=reply_factory(text="no delete"))

    deleted_ids: list[int] = []

    class _PartialMsg:
        def __init__(self, mid):
            self.id = mid

        async def delete(self):
            deleted_ids.append(self.id)

    transcript_chan = _fake_target_channel(channel_id=555, guild_id=100)
    transcript_chan.get_partial_message = MagicMock(
        side_effect=lambda mid: _PartialMsg(mid))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=transcript_chan)
    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=transcript_chan)
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi"))
    guild.text_channels = []
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]

    await indioFromVoice(
        bot, user_id=42, guild_id=100, channel_id=555,
        pregunta="che indio", speaker_name="Tobi",
        source_message_id=12345,
        from_voice=True,
    )
    await _drain()

    assert deleted_ids == [], (
        f"voice path must not delete source message, deleted: {deleted_ids!r}"
    )

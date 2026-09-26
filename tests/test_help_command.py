"""Behavioral tests for /help slash command.

Verifies:
- Embed response contains all category fields.
- Embed response contains copy-pasteable slash command options syntax.
- All non-internal slash commands registered in bot.py are listed.
"""


async def test_help_command_sends_ephemeral_embed(ctx_factory):
    ctx = ctx_factory()
    from bot import help_cmd

    await help_cmd(ctx)

    ctx.followup.send.assert_called_once()
    _, kwargs = ctx.followup.send.call_args
    assert kwargs.get("ephemeral") is True, "help response must be ephemeral"
    assert "embed" in kwargs, "expected an embed in followup.send"


async def test_help_embed_contains_categories_and_copy_paste_commands(ctx_factory):
    ctx = ctx_factory()
    from bot import help_cmd

    await help_cmd(ctx)

    ctx.followup.send.assert_called_once()
    _, kwargs = ctx.followup.send.call_args
    embed = kwargs.get("embed")
    assert embed is not None, "expected an embed response"

    field_names = [f.name for f in embed.fields]
    assert any("Música" in name for name in field_names)
    assert any("Voz y Go Live" in name for name in field_names)
    assert any("IA e Imágenes" in name for name in field_names)
    assert any("Juegos y Mascota" in name for name in field_names)
    assert any("Estadísticas e Historial" in name for name in field_names)
    assert any("Otros" in name for name in field_names)
    assert not any("Instagram" in name for name in field_names)

    all_text = " ".join(f.value for f in embed.fields)

    # Check copy-pasteable slash command syntax format (e.g. /stream opcion: stremio)
    assert "/stream opcion: stremio" in all_text
    assert "/play query:" in all_text
    assert "/clip duracion:" in all_text
    assert "/vapls pregunta:" in all_text
    assert "/indio charla:" in all_text
    assert "/imagen prompt:" in all_text
    assert "/adivinador usuario:" in all_text
    assert "/mascota accion:" in all_text
    assert "/sensibilidad preset:" in all_text
    assert "/sugerencias idea:" in all_text
    assert "/sacudir usuario:" in all_text
    assert "/transferir dias:" in all_text
    assert "/historial usuario:" in all_text
    assert "/estadisticas usuario:" in all_text


async def test_all_user_facing_slash_commands_are_in_help(ctx_factory):
    ctx = ctx_factory()
    from bot import help_cmd

    await help_cmd(ctx)

    ctx.followup.send.assert_called_once()
    _, kwargs = ctx.followup.send.call_args
    embed = kwargs.get("embed")

    assert embed is not None
    all_text = " ".join(f.value for f in embed.fields)

    expected_commands = [
        "play",
        "queue",
        "soundpad",
        "parar",
        "quit",
        "entraindio",
        "clip",
        "verstream",
        "stream",
        "stopstream",
        "sensibilidad",
        "huh",
        "vapls",
        "indio",
        "imagen",
        "adivinador",
        "mascota",
        "spacewar",
        "estadisticas",
        "ranking",
        "historial",
        "actividad",
        "sugerencias",
        "sugerencias-ver",
        "transferir",
        "sacudir",
        "help",
    ]

    for cmd in expected_commands:
        assert f"/{cmd}" in all_text, f"command /{cmd} should be listed in /help"

"""Global slash-command error handler.

Red de seguridad: si un comando levanta una excepción no atrapada, py-cord
loggea pero el usuario se queda sin respuesta. Este módulo traduce la
excepción a un mensaje semántico y la responde como ephemeral.

Los catches locales en `playCommand` y `geminiCommand` siguen corriendo
primero — este handler solo se dispara para errores realmente huérfanos.
"""
import asyncio
import logging

import aiohttp
import discord

import analytics

log = logging.getLogger("bot.errors")


def _classify(original):
    if isinstance(original, (asyncio.TimeoutError, aiohttp.ClientError)):
        return "network", "No pude conectarme a un servicio externo. Probá de nuevo en un rato."
    if isinstance(original, discord.Forbidden):
        return "forbidden", "No tengo permisos para hacer eso en este canal."
    if isinstance(original, discord.NotFound):
        return "not_found", "El recurso de Discord ya no existe (mensaje/canal borrado)."
    return "unhandled", "Algo salió mal procesando el comando. Ya quedó registrado."


async def handle(ctx, error):
    original = getattr(error, "original", error)
    kind, user_msg = _classify(original)

    command_name = getattr(getattr(ctx, "command", None), "name", "?")
    log.exception("Unhandled error in /%s: %s", command_name, original, exc_info=original)

    try:
        analytics.capture_exception(
            original,
            user=getattr(ctx, "author", None),
            guild=getattr(ctx, "guild", None),
            properties={"command": command_name, "error_kind": kind},
        )
    except Exception:
        pass

    try:
        already_responded = False
        response = getattr(ctx, "response", None)
        if response is not None:
            try:
                already_responded = response.is_done()
            except Exception:
                already_responded = False
        if already_responded:
            await ctx.followup.send(user_msg, ephemeral=True)
        else:
            await ctx.respond(user_msg, ephemeral=True)
    except Exception:
        pass

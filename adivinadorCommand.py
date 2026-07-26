"""Slash command and game logic for Headbanz/Adivinador.

Handles dynamic character selection, PIL canvas compositing, font caching,
text-only DM challenge workflow ("si" / "no"), text-only DM guessing,
GoLive streaming, MMR tracking with a single public winner announcement,
and full AI opponent support for the Indio userbot using Gemini.
"""

import asyncio
import io
import json
import logging
import os
import random
import time
import unicodedata
from urllib.parse import urljoin

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger(__name__)

FONT_DIR = "data/fonts"
FONT_PATH = os.path.join(FONT_DIR, "Outfit-Bold.ttf")
CHARS_FILE = "data/adivinador_characters.json"
INDIO_USER_ID = getattr(config, "USERBOT_USER_ID", 0) or 519594605520486428


async def ensure_font_downloaded() -> None:
    """Ensure Outfit font is downloaded and cached locally."""
    if os.path.exists(FONT_PATH):
        return
    os.makedirs(FONT_DIR, exist_ok=True)
    url = "https://raw.githubusercontent.com/google/fonts/main/ofl/outfit/Outfit%5Bwght%5D.ttf"
    try:
        log.info("[HEADBANZ] Downloading Outfit font...")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10.0) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(FONT_PATH, "wb") as f:
                        f.write(data)
                    log.info("[HEADBANZ] Outfit font downloaded successfully.")
                else:
                    log.warning(
                        "[HEADBANZ] Font download failed with HTTP %d",
                        resp.status,
                    )
    except Exception as e:
        log.warning("[HEADBANZ] Font download failed: %s", e)


async def respond(ctx, *args, **kwargs):
    """Safely respond to a Discord context, adapting to test mocks if needed."""
    try:
        if hasattr(ctx, "followup"):
            return await ctx.followup.send(*args, **kwargs)
        elif hasattr(ctx, "response") and not getattr(getattr(ctx, "response", None), "is_done", lambda: True)():
            return await ctx.respond(*args, **kwargs)
        else:
            return await ctx.followup.send(*args, **kwargs)
    except Exception:
        try:
            return await ctx.followup.send(*args, **kwargs)
        except Exception:
            pass


async def generate_headbanz_image(
    user1_name: str,
    char1_name: str,
    char1_source: str,
    char1_img_url: str,
    user2_name: str,
    char2_name: str,
    char2_source: str,
    char2_img_url: str,
    output_path: str,
) -> None:
    """Generate a high-quality 1920x1080 composited image for the GoLive stream."""
    await ensure_font_downloaded()

    width, height = 1920, 1080
    base = Image.new("RGBA", (width, height), (26, 26, 46, 255))
    draw = ImageDraw.Draw(base)

    # Draw gradient background (from #1a1a2e to #16213e)
    for y in range(height):
        r = int(26 + (y / height) * (22 - 26))
        g = int(26 + (y / height) * (33 - 26))
        b = int(46 + (y / height) * (62 - 46))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # Glow border
    draw.rectangle(
        [20, 20, width - 20, height - 20],
        outline=(233, 69, 96, 50),
        width=3,
    )

    # Font helper
    def get_font(size: int):
        if os.path.exists(FONT_PATH):
            try:
                return ImageFont.truetype(FONT_PATH, size)
            except Exception:
                pass
        return ImageFont.load_default()

    # Title
    title_font = get_font(80)
    title_text = "HEADBANZ"
    draw.text(
        (width // 2, 80),
        title_text,
        fill=(233, 69, 96, 255),
        anchor="mm",
        font=title_font,
    )
    draw.line(
        [(width // 2 - 200, 130), (width // 2 + 200, 130)],
        fill=(233, 69, 96, 255),
        width=4,
    )

    # Warning watermark at bottom
    warn_font = get_font(30)
    draw.text(
        (width // 2, height - 60),
        "⚠️ ¡NO MIRES LA TRANSMISIÓN SI ESTÁS JUGANDO! ⚠️",
        fill=(255, 200, 0, 220),
        anchor="mm",
        font=warn_font,
    )

    # Download character image helper
    async def get_char_image(url: str) -> Image.Image | None:
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5.0) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        img = Image.open(io.BytesIO(data))
                        img.thumbnail((400, 400))
                        return img
        except Exception:
            pass
        return None

    img1 = await get_char_image(char1_img_url)
    img2 = await get_char_image(char2_img_url)

    card_width, card_height = 650, 750
    card_y = 200

    def draw_player_card(
        x_center: int,
        user_name: str,
        char_name: str,
        char_source: str,
        img: Image.Image | None,
    ) -> None:
        card_x1 = x_center - card_width // 2
        card_x2 = x_center + card_width // 2
        card_y1 = card_y
        card_y2 = card_y + card_height

        # Rounded card background
        draw.rounded_rectangle(
            [card_x1, card_y1, card_x2, card_y2],
            radius=20,
            fill=(15, 15, 26, 220),
            outline=(233, 69, 96, 120),
            width=2,
        )

        # Player Username
        user_font = get_font(40)
        draw.text(
            (x_center, card_y1 + 50),
            user_name,
            fill=(0, 210, 255, 255),
            anchor="mm",
            font=user_font,
        )
        draw.line(
            [(x_center - 150, card_y1 + 80), (x_center + 150, card_y1 + 80)],
            fill=(0, 210, 255, 80),
            width=2,
        )

        # Character Image Box
        img_box_size = 400
        img_x = x_center - img_box_size // 2
        img_y = card_y1 + 120

        if img:
            img_w, img_h = img.size
            paste_x = x_center - img_w // 2
            paste_y = img_y + (img_box_size - img_h) // 2
            if img.mode == "RGBA":
                base.paste(img, (paste_x, paste_y), img)
            else:
                base.paste(img, (paste_x, paste_y))
        else:
            draw.rectangle(
                [img_x, img_y, img_x + img_box_size, img_y + img_box_size],
                fill=(30, 30, 46, 255),
                outline=(233, 69, 96, 60),
                width=1,
            )
            qm_font = get_font(180)
            draw.text(
                (x_center, img_y + 180),
                "?",
                fill=(233, 69, 96, 150),
                anchor="mm",
                font=qm_font,
            )

        # Character metadata
        char_font = get_font(42)
        source_font = get_font(28)

        draw.text(
            (x_center, card_y2 - 100),
            char_name,
            fill=(255, 255, 255, 255),
            anchor="mm",
            font=char_font,
        )
        draw.text(
            (x_center, card_y2 - 50),
            char_source,
            fill=(150, 150, 150, 255),
            anchor="mm",
            font=source_font,
        )

    draw_player_card(width // 4, user1_name, char1_name, char1_source, img1)
    draw_player_card(3 * width // 4, user2_name, char2_name, char2_source, img2)

    base.convert("RGB").save(output_path, "PNG")


async def send_character_dm(
    user: discord.User | discord.Member,
    opponent_name: str,
    char_name: str,
    char_source: str,
    char_img_url: str,
) -> bool:
    """Send a DM to the player detailing their opponent's character card and instructions to guess."""
    if getattr(user, "id", None) == INDIO_USER_ID:
        return True  # Internal virtual DM for Indio AI

    embed = discord.Embed(
        title=f"🎭 PERSONAJE DEL RIVAL ({opponent_name})",
        description=(
            f"# ✨ {char_name}\n"
            f"**Origen / Serie:** {char_source}\n\n"
            f"📌 **Tu misión:** Darle pistas a **{opponent_name}** por voz o chat para que logre adivinar a **{char_name}**.\n\n"
            f"⚠️ **REGLA:** ¡NO abras la transmisión GoLive en el canal de voz! (Ahí se ve la carta de TU personaje secreto).\n\n"
            f"💡 **¿Cómo adivinar tu propio personaje?**\n"
            f"Escribime el nombre directo a este mensaje privado (DM)."
        ),
        color=0x00D2FF,
    )
    if char_img_url and char_img_url.startswith(("http://", "https://")):
        embed.set_image(url=char_img_url)

    try:
        await user.send(embed=embed)
        return True
    except Exception as e:
        log.warning(
            "[HEADBANZ] Failed to send DM to %s: %s",
            getattr(user, "display_name", user.name),
            e,
        )
        return False


async def stop_golive_stream(guild_id: int) -> bool:
    """Stop the active stream by calling the GoLive userbot relay."""
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        return False
    url = urljoin(config.GOLIVE_RELAY_URL, "/stopstream")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {"guild_id": guild_id}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                return resp.status < 400
    except Exception:
        return False


class HeadbanzChallenge:
    """Represents an active pending challenge sent via DM."""

    def __init__(
        self,
        guild_id: int,
        text_channel_id: int,
        player1: discord.Member | discord.User,
        player2: discord.Member | discord.User,
    ) -> None:
        self.guild_id = guild_id
        self.text_channel_id = text_channel_id
        self.player1 = player1  # Challenger
        self.player2 = player2  # Challenged
        self.created_at = time.time()


class HeadbanzSession:
    """Represents an active Headbanz game session between two players."""

    def __init__(
        self,
        guild_id: int,
        text_channel_id: int,
        player1: discord.Member | discord.User,
        player2: discord.Member | discord.User,
        char1: dict,
        char2: dict,
        image_path: str,
        is_vs_indio: bool = False,
    ) -> None:
        self.guild_id = guild_id
        self.text_channel_id = text_channel_id
        self.player1 = player1  # Assigned char1 (tries to guess char1['name'])
        self.player2 = player2  # Assigned char2 (tries to guess char2['name'])
        self.char1 = char1
        self.char2 = char2
        self.image_path = image_path
        self.is_vs_indio = is_vs_indio
        self.indio_clues: list[str] = []
        self.created_at = time.time()


_pending_challenges: dict[int, HeadbanzChallenge] = {}  # player2.id -> HeadbanzChallenge
_active_games: dict[int, HeadbanzSession] = {}  # user_id -> session


def _normalize(text: str) -> str:
    """Normalize text by stripping accents, symbols, and lowercasing."""
    s = unicodedata.normalize("NFD", text.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(c for c in s if c.isalnum())


# --- INDIO AI INTEGRATION --------------------------------------------------

async def _indio_answer_human_question(session: HeadbanzSession, question: str) -> str:
    """Use Gemini to answer the human's question about their assigned secret character."""
    import geminiClient

    human_name = getattr(session.player1, "display_name", session.player1.name)
    target_char = session.char1["name"]
    target_source = session.char1["source"]

    prompt = (
        f"Sos el Indio, la IA del servidor de Discord. Estás jugando a Headbanz (Adivinador) contra {human_name}.\n"
        f"El personaje secreto que {human_name} tiene que adivinar es: '{target_char}' (de '{target_source}').\n"
        f"{human_name} te hizo esta pregunta: '{question}'.\n"
        f"Respondé la pregunta de forma honesta (diciendo Sí o No claramente, o dando un dato preciso sobre '{target_char}'), "
        f"con el tono característico del Indio (español argentino, picante, informal, rioplatense). "
        f"Sé breve (máximo 2 oraciones). ¡NO reveles el nombre exacto del personaje!"
    )
    try:
        reply = await geminiClient.generate(prompt=prompt)
        if reply and reply.text:
            return reply.text.strip()
    except Exception as e:
        log.warning("[HEADBANZ] Gemini generate answer failed: %s", e)
    return "Sí che, dale para adelante."


async def _run_indio_ai_turn(session: HeadbanzSession) -> None:
    """Indio's AI turn: analyze accumulated clues, ask a smart Yes/No question, or make a victory GUESS."""
    import geminiClient

    if session.player1.id not in _active_games:
        return

    human = session.player1
    human_name = getattr(human, "display_name", human.name)
    clues_summary = ", ".join(session.indio_clues) if session.indio_clues else "Ninguna pista todavía"

    prompt = (
        f"Sos el Indio jugando a Headbanz (Adivinador) contra {human_name}.\n"
        f"Tenés que adivinar tu propio personaje secreto (que no sabés cuál es).\n"
        f"Hasta ahora acumulaste estas respuestas/pistas sobre tu personaje: [{clues_summary}].\n\n"
        f"Opciones:\n"
        f"1. Si ya tenés más del 90% de certeza sobre tu personaje, escribí ÚNICAMENTE: GUESS: <NombreExactoDelPersonaje>\n"
        f"2. Si todavía no sabés, hacé UNA sola pregunta inteligente de Sí/No para achicar las opciones (ej. '¿Mi personaje es un superhéroe?'). "
        f"Escribí únicamente la pregunta con el tono del Indio (argentino, informal, lunfardo breve)."
    )

    try:
        reply = await geminiClient.generate(prompt=prompt)
        if reply and reply.text:
            text = reply.text.strip()
            if text.startswith("GUESS:") or "GUESS:" in text:
                guess = text.split("GUESS:")[-1].strip().split("\n")[0].strip()
                log.info("[HEADBANZ] Indio AI guesses: %s", guess)
                await _resolve_victory(session, winner=session.player2, loser=session.player1, target_char=session.char2)
            else:
                try:
                    await human.send(f"🎙️ **El Indio te pregunta por DM:** {text}")
                except Exception:
                    pass
    except Exception as e:
        log.warning("[HEADBANZ] Indio AI turn failed: %s", e)


async def _resolve_victory(
    session: HeadbanzSession,
    winner: discord.Member | discord.User,
    loser: discord.Member | discord.User,
    target_char: dict,
) -> None:
    """Execute standard victory resolution: DMs, single public text channel announcement, MMR logging, and cleanup."""
    _active_games.pop(session.player1.id, None)
    _active_games.pop(session.player2.id, None)

    winner_name = getattr(winner, "display_name", winner.name)
    loser_name = getattr(loser, "display_name", loser.name)

    # DM confirmations
    try:
        if winner.id != INDIO_USER_ID:
            await winner.send(
                f"🎉 **¡CORRECTO!** Adivinaste a tu personaje (**{target_char['name']}**) y ganaste la partida."
            )
    except Exception:
        pass

    try:
        if loser.id != INDIO_USER_ID:
            await loser.send(
                f"💀 **{winner_name}** adivinó a su personaje (**{target_char['name']}**) y ganó la partida."
            )
    except Exception:
        pass

    # THE ONLY PUBLIC MESSAGE IN THE SERVER CHANNEL (identical template for all players, including Indio)
    try:
        from bot import bot

        text_channel = bot.get_channel(session.text_channel_id)
        if not text_channel:
            text_channel = await bot.fetch_channel(session.text_channel_id)
        if text_channel:
            await text_channel.send(
                f"🏆 **{winner_name}** le ganó a **{loser_name}** en `/adivinador`."
            )
    except Exception as e:
        log.warning("[HEADBANZ] Failed to send public victory announcement: %s", e)

    # Log Glicko-1 MMR updates
    from bot import _log_activity

    await _log_activity(
        winner.id,
        session.guild_id,
        "game_win",
        quality_score=1.0,
        display_name=winner_name,
    )
    await _log_activity(
        loser.id,
        session.guild_id,
        "game_lose",
        quality_score=0.0,
        display_name=loser_name,
    )

    # Stop GoLive stream and cleanup
    await stop_golive_stream(session.guild_id)
    try:
        if os.path.exists(session.image_path):
            os.remove(session.image_path)
    except Exception:
        pass


# --- GAME WORKFLOW AND INTERCEPTION ----------------------------------------

async def _handle_challenge_dm_response(message: discord.Message, challenge: HeadbanzChallenge) -> bool:
    """Handle text DM response ('si' or 'no') to a pending Headbanz challenge."""
    user_id = message.author.id
    content = (message.content or "").strip()
    norm = _normalize(content)

    ACCEPT_WORDS = ("si", "me la banco", "acepto", "banco", "dale", "obvio", "ok", "yes")
    REJECT_WORDS = ("no", "no me da", "rechazo", "cancelar", "paso", "cagon")

    is_accept = norm in ACCEPT_WORDS or any(w in norm for w in ("melabanco", "acepto", "banco"))
    is_reject = norm in REJECT_WORDS or any(w in norm for w in ("nomeda", "rechazo", "cancelar"))

    p1 = challenge.player1
    p2 = challenge.player2
    p1_name = getattr(p1, "display_name", p1.name)
    p2_name = getattr(p2, "display_name", p2.name)

    if is_reject:
        _pending_challenges.pop(user_id, None)
        try:
            await message.channel.send(f"🐔 **Rechazaste el desafío de {p1_name}.**")
        except Exception:
            pass
        try:
            await p1.send(f"🐔 **{p2_name} no se la bancó y rechazó tu desafío de Headbanz.**")
        except Exception:
            pass
        return True

    if is_accept:
        return await start_active_game_session(challenge)

    # Neither accept nor reject
    await message.channel.send("❓ Respondé a este DM con **'si'** para aceptar el desafío o **'no'** para rechazarlo.")
    return True


async def start_active_game_session(challenge: HeadbanzChallenge) -> bool:
    """Start the active Headbanz game session between two players."""
    p1 = challenge.player1
    p2 = challenge.player2
    p1_name = getattr(p1, "display_name", p1.name)
    p2_name = getattr(p2, "display_name", p2.name)

    # Voice validation
    p1_voice = getattr(p1, "voice", None)
    p2_voice = getattr(p2, "voice", None)

    if not p1_voice or not p1_voice.channel:
        try:
            if p2.id != INDIO_USER_ID:
                await p2.send(f"❌ La partida no pudo iniciar porque **{p1_name}** ya no está en un canal de voz.")
        except Exception:
            pass
        _pending_challenges.pop(p2.id, None)
        return True

    is_vs_indio = (p2.id == INDIO_USER_ID)

    if not is_vs_indio:
        if not p2_voice or not p2_voice.channel or p2_voice.channel.id != p1_voice.channel.id:
            try:
                await p2.send(f"❌ Tenés que estar en el mismo canal de voz que **{p1_name}** para iniciar el juego.")
            except Exception:
                pass
            return True

    _pending_challenges.pop(p2.id, None)

    # Load characters
    if not os.path.exists(CHARS_FILE):
        log.error("[HEADBANZ] Characters file missing: %s", CHARS_FILE)
        return True

    try:
        with open(CHARS_FILE, "r", encoding="utf-8") as f:
            characters = json.load(f)
    except Exception as e:
        log.exception("[HEADBANZ] Error loading characters: %s", e)
        return True

    if len(characters) < 2:
        return True

    char1, char2 = random.sample(characters, 2)
    image_path = f"/tmp/headbanz_{challenge.guild_id}.png"

    try:
        await generate_headbanz_image(
            p1_name,
            char1["name"],
            char1["source"],
            char1["image_url"],
            p2_name,
            char2["name"],
            char2["source"],
            char2["image_url"],
            image_path,
        )
    except Exception as e:
        log.exception("[HEADBANZ] Canvas generation failed")
        return True

    # Send crossed DMs
    dm_p1 = await send_character_dm(p1, p2_name, char2["name"], char2["source"], char2["image_url"])
    dm_p2 = await send_character_dm(p2, p1_name, char1["name"], char1["source"], char1["image_url"])

    if not dm_p1 or not dm_p2:
        try:
            os.remove(image_path)
        except Exception:
            pass
        return True

    # Call GoLive relay
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        try:
            os.remove(image_path)
        except Exception:
            pass
        return True

    url = urljoin(config.GOLIVE_RELAY_URL, "/headbanz")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {
        "guild_id": challenge.guild_id,
        "channel_id": p1_voice.channel.id,
        "image_path": image_path,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
        ) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass
                    return True
    except Exception as e:
        try:
            os.remove(image_path)
        except Exception:
            pass
        return True

    # Register active session
    session = HeadbanzSession(
        challenge.guild_id,
        challenge.text_channel_id,
        p1,
        p2,
        char1,
        char2,
        image_path,
        is_vs_indio=is_vs_indio,
    )
    _active_games[p1.id] = session
    _active_games[p2.id] = session

    if is_vs_indio:
        try:
            await p1.send(
                f"⚔️ **¡El Indio aceptó tu desafío!** El juego comenzó y la carta del rival se está transmitiendo por GoLive en tu canal de voz. ¡Revisá tus DMs para adivinar!"
            )
        except Exception:
            pass
        asyncio.create_task(_run_indio_ai_turn(session))
    else:
        try:
            await p2.send("⚔️ **¡Aceptaste el desafío! El juego comenzó. Revisá tus DMs.**")
        except Exception:
            pass
        try:
            await p1.send(
                f"⚔️ **{p2_name} aceptó tu desafío.** El juego comenzó y la carta del rival se está transmitiendo por GoLive. ¡Revisá tus DMs para adivinar!"
            )
        except Exception:
            pass
    return True


async def handle_dm_guess(message: discord.Message) -> bool:
    """Handle incoming DMs to the bot for pending challenge responses and active game guesses."""
    user_id = message.author.id

    # 1. Check pending challenge response first
    challenge = _pending_challenges.get(user_id)
    if challenge:
        return await _handle_challenge_dm_response(message, challenge)

    # 2. Check active game guess
    session = _active_games.get(user_id)
    if not session:
        return False

    content = (message.content or "").strip()
    if not content:
        return False

    if user_id == session.player1.id:
        guesser = session.player1
        opponent = session.player2
        target_char = session.char1
    else:
        guesser = session.player2
        opponent = session.player1
        target_char = session.char2

    guess_norm = _normalize(content)
    target_norm = _normalize(target_char["name"])

    # Match criteria: exact match or strong substring match (min length 3)
    is_correct = (guess_norm == target_norm) or (
        len(guess_norm) >= 3 and guess_norm in target_norm
    )

    if is_correct:
        await _resolve_victory(session, winner=guesser, loser=opponent, target_char=target_char)
        return True
    else:
        # If playing vs Indio, process AI interaction
        if session.is_vs_indio and user_id == session.player1.id:
            # Human sent an answer or question to Indio
            session.indio_clues.append(content)
            
            # Indio answers human question as AI
            answer = await _indio_answer_human_question(session, content)
            try:
                await message.channel.send(f"🤖 **Indio:** {answer}")
            except Exception:
                pass

            # Trigger next turn for Indio
            asyncio.create_task(_run_indio_ai_turn(session))
            return True

        # Regular incorrect guess response
        try:
            await message.channel.send(
                f"❌ **{content}** no es tu personaje. ¡Seguí probando!"
            )
        except Exception:
            pass
        return True


async def start_headbanz_game(
    ctx,
    player1: discord.Member,
    player2: discord.Member,
) -> None:
    """Core logic: send text DM challenge or auto-accept if challenged user is Indio AI."""
    guild_id = ctx.guild.id if ctx.guild else None
    if guild_id is None:
        await respond(
            ctx,
            "❌ Este comando solo funciona en servidores.",
            ephemeral=True,
        )
        return

    if player1.id in _active_games or player2.id in _active_games:
        await respond(
            ctx,
            "❌ Uno de los jugadores ya tiene una partida activa de Headbanz en curso.",
            ephemeral=True,
        )
        return

    if player2.id in _pending_challenges:
        await respond(
            ctx,
            f"❌ **{getattr(player2, 'display_name', player2.name)}** ya tiene un desafío pendiente.",
            ephemeral=True,
        )
        return

    p1_voice = getattr(player1, "voice", None)
    if not p1_voice or not p1_voice.channel:
        await respond(
            ctx,
            f"❌ **{getattr(player1, 'display_name', player1.name)}** tiene que estar en un canal de voz para desafiar.",
            ephemeral=True,
        )
        return

    p1_name = getattr(player1, "display_name", player1.name)
    p2_name = getattr(player2, "display_name", player2.name)
    guild_name = getattr(ctx.guild, "name", "el servidor")

    # Save pending challenge
    challenge = HeadbanzChallenge(guild_id, ctx.channel_id, player1, player2)

    # AUTO-ACCEPT IF CHALLENGED USER IS INDIO
    if player2.id == INDIO_USER_ID:
        await start_active_game_session(challenge)
        await respond(
            ctx,
            f"🤖 **¡El Indio aceptó tu desafío!** La partida comenzó por DM.",
            ephemeral=True,
        )
        return

    _pending_challenges[player2.id] = challenge

    embed = discord.Embed(
        title="🎮 ¡Desafío de Headbanz / Adivinador!",
        description=(
            f"**{p1_name}** te desafió a jugar a Headbanz en **{guild_name}**.\n\n"
            f"El juego consiste en adivinar tu personaje secreto haciendo preguntas en el canal de voz mientras el bot transmite la carta del rival en GoLive.\n\n"
            f"**¿Te la bancás o no te da?**\n\n"
            f"👉 **Respondé a este mensaje por DM** con **'si'** para aceptar o **'no'** para rechazar."
        ),
        color=0xE94560,
    )

    try:
        await player2.send(embed=embed)
    except Exception as e:
        log.warning("[HEADBANZ] Failed to send challenge DM to %s: %s", p2_name, e)
        _pending_challenges.pop(player2.id, None)
        await respond(
            ctx,
            f"❌ No se pudo enviar el desafío a **{p2_name}** porque tiene los DMs cerrados.",
            ephemeral=True,
        )
        return

    # Always respond EPHEMERAL to requester
    await respond(
        ctx,
        f"📩 Desafío enviado a **{p2_name}** por DM. Esperando su respuesta...",
        ephemeral=True,
    )

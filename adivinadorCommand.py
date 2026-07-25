"""Slash command and game logic for Headbanz/Adivinador.

Handles dynamic character selection, PIL canvas compositing, font cacheing,
DMs dispatching, GoLive streaming, and MMR tracking on game completion.
"""

import asyncio
import io
import json
import logging
import os
import random
import time
from urllib.parse import urljoin

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger(__name__)

FONT_DIR = "data/fonts"
FONT_PATH = os.path.join(FONT_DIR, "Outfit-Bold.ttf")
CHARS_FILE = "data/adivinador_characters.json"


async def ensure_font_downloaded() -> None:
    """Ensure Outfit-Bold.ttf is downloaded and cached locally."""
    if os.path.exists(FONT_PATH):
        return
    os.makedirs(FONT_DIR, exist_ok=True)
    url = "https://github.com/google/fonts/raw/main/ofl/outfit/static/Outfit-Bold.ttf"
    try:
        log.info("[HEADBANZ] Downloading Outfit-Bold font...")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10.0) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(FONT_PATH, "wb") as f:
                        f.write(data)
                    log.info("[HEADBANZ] Outfit-Bold font downloaded successfully.")
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
        if hasattr(ctx, "response") and not getattr(getattr(ctx, "response", None), "is_done", lambda: True)():
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
    """Send a DM to the player detailing their opponent's character card."""
    embed = discord.Embed(
        title="🎮 ¡Empezó Headbanz / Adivinador!",
        description=(
            f"Estás jugando contra **{opponent_name}**.\n\n"
            f"**Tu personaje es secreto para vos.**\n"
            f"El personaje de **{opponent_name}** que tenés que ayudarle a adivinar es:\n"
            f"✨ **{char_name}** (de *{char_source}*)\n\n"
            f"⚠️ **¡NO abras la transmisión del canal de voz!** Si mirás la transmisión, verás tu propio personaje y perderás la partida."
        ),
        color=0xE94560,
    )
    if char_img_url:
        embed.set_image(url=char_img_url)
    try:
        await user.send(embed=embed)
        return True
    except Exception as e:
        log.warning(
            "[HEADBANZ] Failed to send DM to %s: %s",
            user.display_name,
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


class HeadbanzControlView(discord.ui.View):
    """View to end the Headbanz game and log Glicko-1 MMR updates."""

    def __init__(
        self,
        guild_id: int,
        player1: discord.Member,
        player2: discord.Member,
        image_path: str,
    ) -> None:
        super().__init__(timeout=1800)  # 30 minutes timeout
        self.guild_id = guild_id
        self.player1 = player1
        self.player2 = player2
        self.image_path = image_path
        self.ended = False

        # Add buttons dynamically with names
        self.btn_p1 = discord.ui.Button(
            label=f"Ganó {player1.display_name[:15]}",
            style=discord.ButtonStyle.success,
            custom_id="hb_win_p1",
        )
        self.btn_p2 = discord.ui.Button(
            label=f"Ganó {player2.display_name[:15]}",
            style=discord.ButtonStyle.success,
            custom_id="hb_win_p2",
        )
        self.btn_cancel = discord.ui.Button(
            label="Empate / Cancelar",
            style=discord.ButtonStyle.danger,
            custom_id="hb_cancel",
        )

        self.btn_p1.callback = self.on_p1_win
        self.btn_p2.callback = self.on_p2_win
        self.btn_cancel.callback = self.on_cancel

        self.add_item(self.btn_p1)
        self.add_item(self.btn_p2)
        self.add_item(self.btn_cancel)

    def _is_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (self.player1.id, self.player2.id):
            return True
        perms = interaction.permissions
        if perms and perms.manage_guild:
            return True
        return False

    async def _cleanup(self) -> None:
        # Stop stream
        await stop_golive_stream(self.guild_id)
        # Remove temp image
        try:
            if os.path.exists(self.image_path):
                os.remove(self.image_path)
        except Exception:
            pass

    async def on_p1_win(self, interaction: discord.Interaction) -> None:
        if not self._is_allowed(interaction):
            await interaction.response.send_message(
                "❌ No sos participante de esta partida ni administrador.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.ended = True
        self.disable_all_items()

        # Log MMR: P1 wins, P2 loses
        from bot import _log_activity

        await _log_activity(
            self.player1.id,
            self.guild_id,
            "game_win",
            quality_score=1.0,
            display_name=self.player1.display_name,
        )
        await _log_activity(
            self.player2.id,
            self.guild_id,
            "game_lose",
            quality_score=0.0,
            display_name=self.player2.display_name,
        )

        await self._cleanup()

        embed = discord.Embed(
            title="🏆 ¡Partida Finalizada!",
            description=(
                f"**Ganador:** {self.player1.mention}\n"
                f"💀 **Derrotado:** {self.player2.mention}\n\n"
                f"La transmisión de GoLive ha finalizado y se registraron los ratings MMR."
            ),
            color=0x00FF00,
        )
        await interaction.edit_original_response(embed=embed, view=None)
        self.stop()

    async def on_p2_win(self, interaction: discord.Interaction) -> None:
        if not self._is_allowed(interaction):
            await interaction.response.send_message(
                "❌ No sos participante de esta partida ni administrador.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.ended = True
        self.disable_all_items()

        # Log MMR: P2 wins, P1 loses
        from bot import _log_activity

        await _log_activity(
            self.player2.id,
            self.guild_id,
            "game_win",
            quality_score=1.0,
            display_name=self.player2.display_name,
        )
        await _log_activity(
            self.player1.id,
            self.guild_id,
            "game_lose",
            quality_score=0.0,
            display_name=self.player1.display_name,
        )

        await self._cleanup()

        embed = discord.Embed(
            title="🏆 ¡Partida Finalizada!",
            description=(
                f"**Ganador:** {self.player2.mention}\n"
                f"💀 **Derrotado:** {self.player1.mention}\n\n"
                f"La transmisión de GoLive ha finalizado y se registraron los ratings MMR."
            ),
            color=0x00FF00,
        )
        await interaction.edit_original_response(embed=embed, view=None)
        self.stop()

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        if not self._is_allowed(interaction):
            await interaction.response.send_message(
                "❌ No sos participante de esta partida ni administrador.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.ended = True
        self.disable_all_items()

        await self._cleanup()

        embed = discord.Embed(
            title="🛑 Partida Cancelada",
            description="La partida fue cancelada o finalizó en empate. No se registraron cambios de MMR.",
            color=0xFF0000,
        )
        await interaction.edit_original_response(embed=embed, view=None)
        self.stop()

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


async def start_headbanz_game(
    ctx,
    player1: discord.Member,
    player2: discord.Member,
) -> None:
    """Core logic to set up characters, generate composite image, DM players, and call GoLive relay."""
    guild_id = ctx.guild.id if ctx.guild else None
    if guild_id is None:
        await respond(
            ctx,
            "❌ Este comando solo funciona en servidores.",
            ephemeral=True,
        )
        return

    # 1. Validation: Voice connection checks
    p1_voice = getattr(player1, "voice", None)
    p2_voice = getattr(player2, "voice", None)
    if not p1_voice or not p1_voice.channel:
        await respond(
            ctx,
            f"❌ **{player1.display_name}** tiene que estar en un canal de voz.",
            ephemeral=True,
        )
        return
    if not p2_voice or not p2_voice.channel or p2_voice.channel.id != p1_voice.channel.id:
        ch_name = getattr(p1_voice.channel, "name", "canal de voz")
        await respond(
            ctx,
            f"❌ Ambos jugadores deben estar conectados al mismo canal de voz (**{ch_name}**).",
            ephemeral=True,
        )
        return

    # Defer response to handle image processing and network delays
    try:
        await ctx.interaction.response.defer()
    except (TypeError, AttributeError):
        pass
    except Exception:
        pass

    # 2. Characters database loading
    if not os.path.exists(CHARS_FILE):
        await respond(
            ctx,
            f"❌ No se encontró la base de datos de personajes en {CHARS_FILE}",
        )
        return

    try:
        with open(CHARS_FILE, "r", encoding="utf-8") as f:
            characters = json.load(f)
    except Exception as e:
        log.exception("[HEADBANZ] Failed to parse characters file")
        await respond(
            ctx,
            f"❌ Error leyendo base de datos de personajes: {e}",
        )
        return

    if len(characters) < 2:
        await respond(
            ctx,
            "❌ La base de datos debe contener al menos 2 personajes.",
        )
        return

    char1, char2 = random.sample(characters, 2)

    # 3. Generate composited image
    image_path = f"/tmp/headbanz_{guild_id}.png"
    try:
        await generate_headbanz_image(
            player1.display_name,
            char1["name"],
            char1["source"],
            char1["image_url"],
            player2.display_name,
            char2["name"],
            char2["source"],
            char2["image_url"],
            image_path,
        )
    except Exception as e:
        log.exception("[HEADBANZ] Canvas generation failed")
        await respond(
            ctx,
            f"❌ Error generando la composición visual: {e}",
        )
        return

    # 4. Dispatch cruzado DMs
    dm_p1 = await send_character_dm(
        player1,
        player2.display_name,
        char2["name"],
        char2["source"],
        char2["image_url"],
    )
    dm_p2 = await send_character_dm(
        player2,
        player1.display_name,
        char1["name"],
        char1["source"],
        char1["image_url"],
    )

    if not dm_p1 or not dm_p2:
        # If DMs fail, clean up image and warn
        try:
            os.remove(image_path)
        except Exception:
            pass
        await respond(
            ctx,
            "❌ No se pudo iniciar el juego porque uno o ambos jugadores tienen los DMs cerrados.",
        )
        return

    # 5. Call GoLive relay POST /headbanz
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        try:
            os.remove(image_path)
        except Exception:
            pass
        await respond(
            ctx,
            "❌ El relay GoLive no está configurado en las variables del bot.",
        )
        return

    url = urljoin(config.GOLIVE_RELAY_URL, "/headbanz")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {
        "guild_id": guild_id,
        "channel_id": p1_voice.channel.id,
        "image_path": image_path,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
        ) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(
                        "[HEADBANZ] relay HTTP %s: %s",
                        resp.status,
                        body[:200],
                    )
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass
                    await respond(
                        ctx,
                        f"❌ El relay GoLive no pudo iniciar el stream (HTTP {resp.status}).",
                    )
                    return
    except Exception as e:
        log.exception("[HEADBANZ] relay call failed")
        try:
            os.remove(image_path)
        except Exception:
            pass
        await respond(
            ctx,
            f"❌ Error de comunicación con el relay de GoLive: {e}",
        )
        return

    p1_mention = getattr(player1, "mention", player1.display_name)
    p2_mention = getattr(player2, "mention", player2.display_name)
    ch_mention = getattr(p1_voice.channel, "mention", f"#{getattr(p1_voice.channel, 'name', 'canal')}")

    view = HeadbanzControlView(guild_id, player1, player2, image_path)
    embed = discord.Embed(
        title="🎮 ¡Juego Headbanz Iniciado!",
        description=(
            f"👥 **Participantes:** {p1_mention} vs {p2_mention}\n"
            f"🔊 **Canal de voz:** {ch_mention}\n\n"
            f"El userbot GoLive está transmitiendo las imágenes correspondientes.\n"
            f"**Espectadores:** Pueden unirse a ver la transmisión del bot en el canal de voz.\n\n"
            f"⚠️ **IMPORTANTE:** Los jugadores recibieron sus personajes rivales por DM. **No deben abrir la transmisión de video** o quedarán descalificados por ver su propia carta.\n\n"
            f"Una vez finalizado, seleccionen al ganador con los botones de abajo:"
        ),
        color=0xE94560,
    )
    await respond(ctx, embed=embed, view=view)

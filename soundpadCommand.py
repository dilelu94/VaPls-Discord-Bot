"""Soundpad slash command and UI for playing custom audio clips."""

import os
import asyncio
import difflib
import logging
import discord
import config
import analytics
import geminiKeys
from greeting import set_pending_trigger

_log = logging.getLogger("bot.soundpad")

_AUDIO_EXTS = {".opus", ".mp3", ".wav", ".ogg", ".m4a"}

# Per-guild registry of "live" soundpad panel views. The idle watchdog reads
# this so it can keep the bot in voice while the panel is still clickable —
# even if no clip is currently playing. We use a list (not a counter) so that
# on disconnect we can iterate over the views and disable their buttons.
_active_panels: dict[int, list["SoundpadView"]] = {}


def _register_panel(view: "SoundpadView") -> None:
    """Mark a soundpad panel as live for this guild."""
    gid = view.guild_id
    if gid is None:
        return
    _active_panels.setdefault(gid, []).append(view)


def _unregister_panel(guild_id: int, view: "SoundpadView") -> None:
    """Mark a soundpad panel as no longer live. Idempotent."""
    if guild_id not in _active_panels:
        return
    try:
        _active_panels[guild_id].remove(view)
    except ValueError:
        pass
    if not _active_panels[guild_id]:
        _active_panels.pop(guild_id, None)


def has_active_panel(guild_id: int) -> bool:
    """Return True iff at least one soundpad panel is live for this guild.

    Read by ``idleWatchdog._is_active`` so the bot doesn't disconnect from
    voice while a user can still click a button on the panel.
    """
    return len(_active_panels.get(guild_id, [])) > 0


async def disable_panels(guild_id: int) -> None:
    """Disable all live soundpad panels for a guild and unregister them.

    Called by /parar and the idle watchdog before disconnecting, so the
    panel message doesn't stay visible with zombie buttons that silently
    fail.
    """
    views = _active_panels.pop(guild_id, [])
    for v in views:
        for item in v.children:
            item.disabled = True
        if v.message is not None:
            try:
                await v.message.edit(view=v)
            except Exception:
                pass


def _normalize_clip_name(text: str) -> str:
    """Normalize a string for fuzzy matching against clip names."""
    return text.replace("_", " ").replace("-", " ").lower().strip()


def iter_clips(output_dir: str):
    """Yield ``(absolute_path, display_name)`` for every audio clip under ``output_dir``.

    Walks all category folders (and their subfolders) and produces a normalized
    display name suitable for fuzzy matching (lowercase, underscores/dashes
    replaced with spaces).
    """
    if not os.path.isdir(output_dir):
        return
    for category in sorted(os.listdir(output_dir)):
        cat_dir = os.path.join(output_dir, category)
        if not os.path.isdir(cat_dir) or category.startswith("."):
            continue
        for root, dirs, files in os.walk(cat_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                _, ext = os.path.splitext(f)
                if ext.lower() not in _AUDIO_EXTS:
                    continue
                abs_path = os.path.join(root, f)
                stem = os.path.splitext(f)[0]
                yield abs_path, _normalize_clip_name(stem)


def find_best_match(query: str, output_dir: str, cutoff: float = 0.4):
    """Return the absolute path of the clip whose name is most similar to ``query``.

    Args:
        query: Free-form search string.
        output_dir: Soundpad root (same shape as ``CUSTOM_AUDIO_PATH``).
        cutoff: Minimum similarity ratio (0..1). Below this no result is returned.

    Returns:
        Absolute path to the best-matching clip, or ``None`` if no clip exists or
        no name is similar enough.
    """
    clips = list(iter_clips(output_dir))
    if not clips:
        return None
    normalized_query = _normalize_clip_name(query)
    if not normalized_query:
        return None
    names = [name for _, name in clips]
    matches = difflib.get_close_matches(normalized_query, names, n=1, cutoff=cutoff)
    if matches:
        best = matches[0]
        for path, name in clips:
            if name == best:
                return path

    for path, name in clips:
        if normalized_query in name:
            return path

    return None


# Autocomplete index for the /soundpad `query` parameter. Keyed by
# ``output_dir`` so a config swap doesn't poison results. The fingerprint
# is a tuple of (st_mtime_ns) values for the root and every category
# directory — lsyncd touching any file under a category bumps that
# category's mtime, so the cache invalidates itself without external hooks.
_AUTOCOMPLETE_CACHE: dict[str, tuple[tuple, list[tuple[str, str]]]] = {}


def _dirs_fingerprint(output_dir: str) -> tuple:
    """Return a cheap signature of the soundpad tree's structure.

    Reads ``st_mtime_ns`` of the root and every category subdir. When the
    file tree changes (lsyncd create/move/delete), the parent directory's
    mtime changes immediately, so a single fingerprint comparison detects
    all relevant changes without walking files.
    """
    try:
        root_mtime = os.stat(output_dir).st_mtime_ns
    except OSError:
        return ()
    parts = [("__root__", root_mtime)]
    try:
        entries = sorted(os.listdir(output_dir))
    except OSError:
        return tuple(parts)
    for name in entries:
        if name.startswith("."):
            continue
        sub = os.path.join(output_dir, name)
        try:
            st = os.stat(sub)
        except OSError:
            continue
        if not os.path.isdir(sub):
            continue
        parts.append((name, st.st_mtime_ns))
    return tuple(parts)


def _get_clip_index(output_dir: str) -> list[tuple[str, str]]:
    """Return cached ``(normalized_name, display_name)`` for every clip.

    Cache hit cost: O(N categories) stat() calls. Cache miss cost: a full
    ``iter_clips`` walk. Invalidates automatically when the directory tree
    changes.
    """
    fp = _dirs_fingerprint(output_dir)
    entry = _AUTOCOMPLETE_CACHE.get(output_dir)
    if entry is not None and entry[0] == fp:
        return entry[1]
    clips: list[tuple[str, str]] = []
    for path, _ in iter_clips(output_dir):
        stem = os.path.splitext(os.path.basename(path))[0]
        clips.append((_normalize_clip_name(stem), stem))
    _AUTOCOMPLETE_CACHE[output_dir] = (fp, clips)
    return clips


def _truncate_choice(display_name: str) -> str:
    """Clip a choice label to Discord's 100-char autocomplete limit.

    Discord rejects the *entire* autocomplete response with 400 when any
    single choice name exceeds 100 chars, so one absurdly-long filename
    would silently kill the suggestion list for every keystroke.
    """
    if len(display_name) <= 100:
        return display_name
    return display_name[:99] + "…"


async def soundpad_query_autocomplete(ctx):
    """py-cord autocomplete callback for ``/soundpad``'s ``query`` parameter.

    Returns up to 25 suggestions (Discord's hard cap) whose normalized
    clip name contains the user's partial input as a substring.
    """
    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    partial = _normalize_clip_name(getattr(ctx, "value", "") or "")
    clips = _get_clip_index(output_dir)
    if not partial:
        pool = clips
    else:
        pool = [(n, d) for n, d in clips if partial in n]
    return [_truncate_choice(display) for _, display in pool[:25]]


def _pick_populated_voice_channel(guild: discord.Guild):
    """Return the voice channel in ``guild`` with the most non-bot members, or None."""
    candidates = [
        (ch, sum(1 for m in ch.members if not m.bot))
        for ch in getattr(guild, "voice_channels", [])
    ]
    candidates = [c for c in candidates if c[1] > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


async def play_clip_by_query(
    bot,
    guild: discord.Guild,
    query: str,
    voice_channel: "discord.VoiceChannel | None" = None,
    cutoff: float = 0.4,
):
    """Find the soundpad clip most similar to ``query`` and play it in ``guild``.

    If the bot is not already in a voice channel, it joins ``voice_channel`` when
    provided or the most-populated one otherwise, plays the clip, and disconnects
    once playback finishes. If the bot was already connected, it plays in place
    and stays connected.

    Args:
        bot: Discord client (used to look up an existing ``voice_client``).
        guild: Guild where playback should happen.
        query: Free-form search string matched against clip display names.
        voice_channel: Optional explicit channel. Falls back to auto-pick.
        cutoff: difflib similarity cutoff (0..1).

    Returns:
        Absolute path of the clip that was played, or ``None`` if no clip
        matched, no voice channel was usable, or playback could not start.

    Side Effects:
        Connects/disconnects from voice, plays audio.
    """
    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    path = find_best_match(query, output_dir, cutoff=cutoff)
    if path is None:
        return None

    vc = discord.utils.get(bot.voice_clients, guild=guild) if bot is not None else None
    had_to_connect = False

    if vc is None or not vc.is_connected():
        target = voice_channel or _pick_populated_voice_channel(guild)
        if target is None:
            return None
        try:
            vc = await target.connect(reconnect=True, timeout=10.0)
            had_to_connect = True
        except Exception as e:
            _log.warning("Failed to connect to voice channel %s: %s", target, e)
            analytics.capture_exception(
                e, properties={"action": "soundpad_connect", "guild_id": guild.id}
            )
            return None
    elif (
        voice_channel is not None
        and getattr(vc.channel, "id", None) != voice_channel.id
    ):
        try:
            await vc.move_to(voice_channel)
        except Exception as e:
            _log.warning("Failed to move to voice channel %s: %s", voice_channel, e)
            analytics.capture_exception(
                e, properties={"action": "soundpad_move_to", "guild_id": guild.id}
            )

    try:
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.2)
    except Exception:
        pass

    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _after(_err):
        try:
            loop.call_soon_threadsafe(done.set)
        except Exception:
            pass

    try:
        vc.play(
            discord.FFmpegOpusAudio(path, options='-af "dynaudnorm=p=0.95:f=200"'),
            after=_after,
        )
    except Exception:
        if had_to_connect:
            try:
                await vc.disconnect()
            except Exception:
                pass
        return None

    await done.wait()

    if had_to_connect:
        try:
            await vc.disconnect()
        except Exception:
            pass

    return path


class SoundpadView(discord.ui.View):
    """Interactive UI for browsing and playing soundpad audio files."""

    def __init__(self, output_dir: str, guild_id: "int | None" = None):
        """Initialize the soundpad view.

        Args:
            output_dir: Base directory containing category folders with audio.
            guild_id: Discord guild this panel belongs to. When provided, the
                panel registers itself with the active-panel registry so the
                idle watchdog keeps the bot in voice while the panel is open,
                and unregisters when the View times out.

        Raises:
            ValueError: If the directory is missing or empty.
        """
        super().__init__(timeout=60)
        self.output_dir = output_dir
        self.message = None
        self.guild_id = guild_id
        self.is_playing = False

        if not os.path.exists(output_dir):
            raise ValueError(f"La ruta de audios no existe: {output_dir}")

        self.categories = sorted(
            [
                d
                for d in os.listdir(output_dir)
                if os.path.isdir(os.path.join(output_dir, d)) and not d.startswith(".")
            ]
        )

        if not self.categories:
            raise ValueError(
                "No se encontraron carpetas (categorías) en la ruta de audios."
            )

        self.selected_category = self.categories[0]
        self.selected_subfolder = "/"
        self.current_page = 0
        self.selected_file = None
        self.setup_components()

        if self.guild_id is not None:
            _register_panel(self)

    async def on_timeout(self):
        """Remove this panel from the active-panel registry when it expires."""
        if self.guild_id is not None:
            _unregister_panel(self.guild_id, self)

    def get_subfolders(self, category: str):
        """Return the list of subfolders for a category.

        Args:
            category: Category folder name.

        Returns:
            Sorted list of subfolder paths (including "/").
        """
        cat_dir = os.path.join(self.output_dir, category)
        if not os.path.exists(cat_dir):
            return ["/"]
        subfolders = ["/"]
        for root, dirs, _ in os.walk(cat_dir):
            for d in dirs:
                if not d.startswith("."):
                    full_path = os.path.join(root, d)
                    rel_path = os.path.relpath(full_path, cat_dir)
                    subfolders.append(rel_path)
        return sorted(subfolders)

    def get_folder_files(self, category: str, subfolder: str):
        """List audio files in the selected category/subfolder.

        Args:
            category: Category folder name.
            subfolder: Subfolder path ("/" for root).

        Returns:
            Sorted list of audio file paths relative to the category.
        """
        if subfolder == "/":
            target_dir = os.path.join(self.output_dir, category)
        else:
            target_dir = os.path.join(self.output_dir, category, subfolder)

        if not os.path.exists(target_dir):
            return []

        files = []
        for f in os.listdir(target_dir):
            full_path = os.path.join(target_dir, f)
            if os.path.isfile(full_path):
                _, ext = os.path.splitext(f)
                if ext.lower() in {".opus", ".mp3", ".wav", ".ogg", ".m4a"}:
                    if subfolder == "/":
                        files.append(f)
                    else:
                        files.append(os.path.join(subfolder, f))
        return sorted(files)

    def setup_components(self):
        """Build the select menus and buttons for the current state.

        Side Effects:
            Mutates the view's UI components.
        """
        self.clear_items()

        # 1. Category Select (Row 0)
        category_options = [
            discord.SelectOption(
                label=cat, value=cat, default=(cat == self.selected_category)
            )
            for cat in self.categories[:25]
        ]
        category_select = discord.ui.Select(
            placeholder="📁 Selecciona una categoría...",
            options=category_options,
            row=0,
            custom_id="sp_category_select",
        )
        category_select.callback = self.on_category_select
        self.add_item(category_select)

        # 2. Subfolder Select (Row 1, if any exist)
        subfolders = self.get_subfolders(self.selected_category)
        has_subfolders = len(subfolders) > 1

        row_offset = 0
        if has_subfolders:
            subfolder_options = [
                discord.SelectOption(
                    label="Root (/)"
                    if sub == "/"
                    else sub.replace("/", " ➔ ")
                    .replace("\\", " ➔ ")
                    .replace("_", " ")
                    .replace("-", " ")[:100],
                    value=sub,
                    default=(sub == self.selected_subfolder),
                )
                for sub in subfolders[:25]
            ]
            subfolder_select = discord.ui.Select(
                placeholder="📂 Selecciona una subcarpeta...",
                options=subfolder_options,
                row=1,
                custom_id="sp_subfolder_select",
            )
            subfolder_select.callback = self.on_subfolder_select
            self.add_item(subfolder_select)
            row_offset = 1

        # 3. Audio Select (Row 1 or Row 2)
        files = self.get_folder_files(self.selected_category, self.selected_subfolder)

        page_size = 25
        self.total_pages = max(1, (len(files) + page_size - 1) // page_size)
        if self.current_page >= self.total_pages:
            self.current_page = self.total_pages - 1
        if self.current_page < 0:
            self.current_page = 0

        page_files = files[
            self.current_page * page_size : (self.current_page + 1) * page_size
        ]
        self.files_by_index = {str(i): f for i, f in enumerate(page_files)}

        if page_files:
            if not self.selected_file or self.selected_file not in files:
                self.selected_file = page_files[0]
            elif self.selected_file not in page_files:
                self.selected_file = page_files[0]

            audio_options = []
            for i, f in enumerate(page_files):
                base_name = os.path.basename(f)
                label = (
                    os.path.splitext(base_name)[0].replace("_", " ").replace("-", " ")
                )
                label = label[:100]
                audio_options.append(
                    discord.SelectOption(
                        label=label, value=str(i), default=(f == self.selected_file)
                    )
                )
            audio_select = discord.ui.Select(
                placeholder="🔊 Selecciona un sonido...",
                options=audio_options,
                row=row_offset + 1,
                custom_id="sp_audio_select",
            )
            audio_select.callback = self.on_audio_select
            self.add_item(audio_select)
        else:
            self.selected_file = None
            self.files_by_index = {}
            self.add_item(
                discord.ui.Select(
                    placeholder="⚠️ No hay audios en este directorio",
                    options=[discord.SelectOption(label="Vacío", value="none")],
                    disabled=True,
                    row=row_offset + 1,
                    custom_id="sp_audio_select",
                )
            )

        # 4. Action Row (Row 2 or Row 3)
        prev_btn = discord.ui.Button(
            label="◀️",
            style=discord.ButtonStyle.secondary,
            row=row_offset + 2,
            custom_id="btn_sp_prev",
            disabled=(self.current_page == 0),
        )
        prev_btn.callback = self.on_prev_click
        self.add_item(prev_btn)

        play_btn = discord.ui.Button(
            label="🔄 Reproducir",
            style=discord.ButtonStyle.success,
            row=row_offset + 2,
            custom_id="btn_sp_play",
        )
        play_btn.callback = self.on_play_click
        self.add_item(play_btn)

        if self.is_playing:
            stop_btn = discord.ui.Button(
                label="⏹️ Detener",
                style=discord.ButtonStyle.danger,
                row=row_offset + 2,
                custom_id="btn_sp_stop",
            )
            stop_btn.callback = self.on_stop_click
            self.add_item(stop_btn)

        next_btn = discord.ui.Button(
            label="▶️",
            style=discord.ButtonStyle.secondary,
            row=row_offset + 2,
            custom_id="btn_sp_next",
            disabled=(self.current_page == self.total_pages - 1),
        )
        next_btn.callback = self.on_next_click
        self.add_item(next_btn)

    async def update_message(
        self, interaction: discord.Interaction, status_text: str = None
    ):
        """Render the embed and edit the interaction message.

        Args:
            interaction: Interaction to update.
            status_text: Optional status line shown in the embed.

        Side Effects:
            Edits the original interaction response.

        Async:
            This function is a coroutine and must be awaited.
        """
        embed = discord.Embed(title="🎛️ Soundpad Panel", color=discord.Color.blurple())
        embed.add_field(name="📁 Categoría", value=self.selected_category, inline=True)

        sub_label = (
            "Root (/)"
            if self.selected_subfolder == "/"
            else self.selected_subfolder.replace("/", " ➔ ").replace("\\", " ➔ ")
        )
        embed.add_field(name="📂 Subcarpeta", value=sub_label, inline=True)

        if self.selected_file:
            clean_file = os.path.basename(self.selected_file)
            clean_file = (
                os.path.splitext(clean_file)[0].replace("_", " ").replace("-", " ")
            )
        else:
            clean_file = "Ninguno"
        embed.add_field(name="🔊 Sonido", value=clean_file, inline=True)

        embed.set_footer(text=f"Página {self.current_page + 1} de {self.total_pages}")

        if status_text:
            embed.add_field(name="⚡ Estado", value=status_text, inline=False)

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            pass

    async def _force_reconnect(self, interaction: discord.Interaction):
        """Reconnect the bot to the user's voice channel if needed.

        Args:
            interaction: Discord interaction with user voice state.

        Returns:
            Tuple of (voice_client, error_message). If error_message is not None
            then reconnection failed.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not interaction.user.voice:
            return None, "No estás en un canal de voz."
        for stale in list(interaction.client.voice_clients):
            if stale.guild.id == interaction.guild.id:
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    pass
        try:
            set_pending_trigger(interaction.user.voice.channel.id, interaction.user.id)
            vc = await interaction.user.voice.channel.connect(
                reconnect=True, timeout=10.0
            )
        except Exception as e:
            return None, f"Error al reconectar: {e}"
        return vc, None

    async def play_sound(self, interaction: discord.Interaction):
        """Play the currently selected soundpad audio file.

        Args:
            interaction: Interaction that requested playback.

        Side Effects:
            Connects to voice, plays audio, and emits analytics events.

        Async:
            This function is a coroutine and must be awaited.
        """
        from playCommand import guildPlayers

        if interaction.guild.id in guildPlayers:
            player = guildPlayers[interaction.guild.id]
            if player.currentSong:
                _log.info(
                    "soundpad UI gated: music playing (guild %s)",
                    interaction.guild.id,
                )
                return await interaction.followup.send(
                    "⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.",
                    ephemeral=True,
                )

        guild = interaction.guild
        vc = guild.voice_client

        if not vc or not vc.is_connected():
            for _ in range(5):
                await asyncio.sleep(0.3)
                vc = guild.voice_client
                if vc and vc.is_connected():
                    break
            if not vc or not vc.is_connected():
                vc, err = await self._force_reconnect(interaction)
                if err:
                    return await interaction.followup.send(f"❌ {err}", ephemeral=True)

        filepath = os.path.join(
            self.output_dir, self.selected_category, self.selected_file
        )
        if not os.path.exists(filepath):
            return await interaction.followup.send(
                f"❌ No encuentro el archivo: {self.selected_file}", ephemeral=True
            )

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception as e:
            _log.warning("Failed to stop existing playback: %s", e)

        loop = asyncio.get_running_loop()

        def _after(_err):
            self.is_playing = False
            self.setup_components()
            try:
                asyncio.run_coroutine_threadsafe(
                    self.update_message(interaction, status_text="⏹️ Terminado"), loop
                )
            except Exception as e:
                _log.warning("Error updating soundpad UI after playback: %s", e)

        self.is_playing = True
        self.setup_components()

        try:
            vc.play(
                discord.FFmpegOpusAudio(
                    filepath, options='-af "dynaudnorm=p=0.95:f=200"'
                ),
                after=_after,
            )
            analytics.capture(
                "soundpad audio played",
                user=interaction.user,
                guild=interaction.guild,
                properties={
                    "category": self.selected_category,
                    "audio_file": self.selected_file,
                    "after_reconnect": False,
                },
            )
            return await self.update_message(
                interaction,
                status_text=f"▶️ Reproduciendo: {os.path.basename(self.selected_file)} (por {interaction.user.display_name})",
            )
        except discord.ClientException as e:
            _log.warning("ClientException on play (%s); forcing reconnect...", e)
            analytics.capture(
                "soundpad reconnect attempted",
                user=interaction.user,
                guild=interaction.guild,
                properties={
                    "reason": str(e),
                    "category": self.selected_category,
                    "audio_file": self.selected_file,
                },
            )
            vc, err = await self._force_reconnect(interaction)
            if err:
                analytics.capture(
                    "soundpad playback failed",
                    user=interaction.user,
                    guild=interaction.guild,
                    properties={
                        "stage": "reconnect",
                        "error": err,
                        "category": self.selected_category,
                        "audio_file": self.selected_file,
                    },
                )
                return await interaction.followup.send(
                    f"❌ Reconexión falló: {err}", ephemeral=True
                )
            try:
                vc.play(
                    discord.FFmpegOpusAudio(
                        filepath, options='-af "dynaudnorm=p=0.95:f=200"'
                    ),
                    after=_after,
                )
                analytics.capture(
                    "soundpad audio played",
                    user=interaction.user,
                    guild=interaction.guild,
                    properties={
                        "category": self.selected_category,
                        "audio_file": self.selected_file,
                        "after_reconnect": True,
                    },
                )
                await self.update_message(
                    interaction,
                    status_text=f"▶️ Reproduciendo (tras reconectar): {os.path.basename(self.selected_file)} (por {interaction.user.display_name})",
                )
            except Exception as e2:
                _log.warning("Retry failed: %s", e2)
                analytics.capture_exception(
                    e2,
                    user=interaction.user,
                    guild=interaction.guild,
                    properties={
                        "action": "soundpad_retry_play",
                        "category": self.selected_category,
                        "audio_file": self.selected_file,
                    },
                )
                await interaction.followup.send(
                    f"❌ Error tras reconectar: {e2}", ephemeral=True
                )
        except Exception as e:
            _log.warning("Playback failed: %s", e)
            analytics.capture_exception(
                e,
                user=interaction.user,
                guild=interaction.guild,
                properties={
                    "action": "soundpad_play",
                    "category": self.selected_category,
                    "audio_file": self.selected_file,
                },
            )
            await interaction.followup.send(
                f"❌ Error de reproducción: {e}", ephemeral=True
            )

    async def on_category_select(self, interaction: discord.Interaction):
        """Handle category selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Resets pagination state and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.selected_category = interaction.data["values"][0]
        self.selected_subfolder = "/"
        self.current_page = 0
        self.selected_file = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_subfolder_select(self, interaction: discord.Interaction):
        """Handle subfolder selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Resets pagination state and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.selected_subfolder = interaction.data["values"][0]
        self.current_page = 0
        self.selected_file = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_audio_select(self, interaction: discord.Interaction):
        """Handle audio selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Updates selected file and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        selected_index = interaction.data["values"][0]
        self.selected_file = self.files_by_index.get(selected_index)
        self.setup_components()
        await self.update_message(interaction)

    async def on_play_click(self, interaction: discord.Interaction):
        """Handle the Play button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Plays the selected audio file.

        Async:
            This function is a coroutine and must be awaited.
        """
        await interaction.response.defer()
        await self.play_sound(interaction)

    async def on_stop_click(self, interaction: discord.Interaction):
        """Handle the Stop button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Stops current playback and updates the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
        self.is_playing = False
        self.setup_components()
        analytics.capture(
            "soundpad audio stopped",
            user=interaction.user,
            guild=interaction.guild,
            properties={
                "category": self.selected_category,
                "audio_file": self.selected_file,
            },
        )
        await self.update_message(interaction, status_text="⏹️ Detenido")

    async def on_prev_click(self, interaction: discord.Interaction):
        """Handle the Previous page button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Moves pagination backward and re-renders the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.current_page > 0:
            self.current_page -= 1
            self.selected_file = None
            self.setup_components()
            await self.update_message(interaction)
        else:
            await interaction.response.defer()

    async def on_next_click(self, interaction: discord.Interaction):
        """Handle the Next page button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Moves pagination forward and re-renders the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.selected_file = None
            self.setup_components()
            await self.update_message(interaction)
        else:
            await interaction.response.defer()


class SoundpadStopView(discord.ui.View):
    """One-button view shown while a /soundpad query-mode clip is playing.

    Pressing the button stops playback. ``play_clip_by_query`` is awaiting the
    voice client's ``after`` callback, so stopping triggers the same teardown
    (and disconnect, when the bot had to connect itself) as natural completion.
    """

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=600)
        self.guild = guild
        self.message = None

    @discord.ui.button(
        label="⏹️ Parar", style=discord.ButtonStyle.danger, custom_id="sp_query_stop"
    )
    async def on_stop(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            try:
                vc.stop()
            except Exception:
                pass
        for item in self.children:
            item.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass


async def soundpadLogic(
    ctx: discord.ApplicationContext,
    query: "str | None" = None,
    redirect_channel: "discord.TextChannel | None" = None,
):
    """Handle the /soundpad slash command.

    Args:
        ctx: Discord application context.
        query: Optional fuzzy search. When provided, finds the most similar
            clip and plays it directly instead of opening the UI panel.
        redirect_channel: When set, public output (panel message or query-mode
            confirmation) is posted here instead of via ctx.followup. Used
            when /soundpad is invoked outside the designated audio channel.

    Returns:
        None.

    Side Effects:
        Connects to voice, sends a UI message or plays a clip, and emits
        analytics events.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not ctx.response.is_done():
        try:
            await ctx.defer()
        except Exception:
            pass

    if not geminiKeys.has_user_key(ctx.author.id):
        contributors = geminiKeys.format_contributors_line()
        msg = (
            "🔒 Para usar **/soundpad** necesitás aportar una API key de Gemini al pool del bot.\n\n"
            f"**Cómo conseguirla:** entrá a {config.GEMINI_KEYS_DONATION_URL}, "
            "clickeá *Create API key* (es gratis con una cuenta de Google) "
            "y mandámela por DM al bot. Apenas la sumo al pool podés usar el comando."
        )
        if contributors:
            msg = f"{msg}\n\n{contributors}"
        analytics.capture(
            "soundpad gated",
            user=ctx.author,
            guild=ctx.guild,
            properties={"reason": "no_user_key"},
        )
        return await ctx.followup.send(msg, ephemeral=True)

    # Block while a music vote is open: a soundpad clip stomping on top of an
    # in-progress music selection makes the bot feel hyperactive (and the
    # /play queue ends up muted by the clip). Decide the song first.
    import playCommand

    if (
        ctx.guild is not None
        and playCommand.get_active_vote(int(ctx.guild.id)) is not None
    ):
        analytics.capture(
            "soundpad gated",
            user=ctx.author,
            guild=ctx.guild,
            properties={"reason": "music_vote_open"},
        )
        return await ctx.followup.send(
            "che, hay una votación de música abierta — decidí primero "
            "(o esperá que cierre).",
            ephemeral=True,
        )

    from playCommand import guildPlayers

    if ctx.guild.id in guildPlayers:
        player = guildPlayers[ctx.guild.id]
        if player.currentSong:
            if redirect_channel is not None:
                _log.info(
                    "soundpad gated: music playing (redirect from %s)",
                    ctx.channel_id,
                )
                try:
                    await ctx.interaction.edit_original_response(
                        content="❌ No se puede, hay música sonando"
                    )
                except Exception:
                    pass
                return
            _log.info("soundpad gated: music playing (direct in play channel)")
            return await ctx.followup.send(
                "⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.",
                ephemeral=True,
            )

    if not ctx.author.voice:
        return await ctx.followup.send(
            "❌ Debes estar en un canal de voz.", ephemeral=True
        )

    if query:
        output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
        match_path = find_best_match(query, output_dir)
        if match_path is None:
            analytics.capture(
                "soundpad query miss",
                user=ctx.author,
                guild=ctx.guild,
                properties={"query": query},
            )
            return await ctx.followup.send(
                f"🔎 No encontré ningún clip parecido a `{query}`.",
                ephemeral=True,
            )

        display = _normalize_clip_name(
            os.path.splitext(os.path.basename(match_path))[0]
        )
        view = SoundpadStopView(ctx.guild)
        if redirect_channel is not None:
            message = await redirect_channel.send(
                f"▶️ Reproduciendo: **{display}** (por {ctx.author.display_name})",
                view=view,
            )
        else:
            message = await ctx.followup.send(
                f"▶️ Reproduciendo: **{display}** (por {ctx.author.display_name})",
                view=view,
            )
        view.message = message
        analytics.capture(
            "soundpad query played",
            user=ctx.author,
            guild=ctx.guild,
            properties={"query": query, "audio_file": os.path.basename(match_path)},
        )

        await play_clip_by_query(
            ctx.bot,
            ctx.guild,
            query,
            voice_channel=ctx.author.voice.channel,
        )

        try:
            await message.edit(view=None)
        except Exception:
            pass
        return

    vc = ctx.guild.voice_client
    if not vc:
        try:
            set_pending_trigger(ctx.author.voice.channel.id, ctx.author.id)
            vc = await ctx.author.voice.channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            return await ctx.followup.send(f"❌ Error al conectar: {e}", ephemeral=True)
        # trigger_soundboard_entry ya se dispara por on_voice_state_update
    elif vc.channel.id != ctx.author.voice.channel.id:
        try:
            set_pending_trigger(ctx.author.voice.channel.id, ctx.author.id)
            await vc.move_to(ctx.author.voice.channel)
        except Exception as e:
            _log.warning("Failed to move_to in soundpadLogic: %s", e)
            analytics.capture_exception(
                e,
                properties={
                    "action": "soundpad_logic_move_to",
                    "guild_id": ctx.guild.id,
                },
            )

    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    try:
        view = SoundpadView(output_dir, guild_id=ctx.guild.id)
        analytics.capture(
            "soundpad panel opened",
            user=ctx.author,
            guild=ctx.guild,
            properties={
                "categories_count": len(view.categories),
                "default_category": view.selected_category,
            },
        )
        if redirect_channel is not None:
            await redirect_channel.send("🎛️ Soundpad Control Panel", view=view)
        else:
            await ctx.followup.send("🎛️ Soundpad Control Panel", view=view)
    except Exception as e:
        analytics.capture_exception(
            e,
            user=ctx.author,
            guild=ctx.guild,
            properties={"action": "soundpad_panel_open"},
        )
        await ctx.followup.send(f"❌ Error: {e}", ephemeral=True)

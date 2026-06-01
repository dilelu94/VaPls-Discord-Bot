"""Music playback and queue management for the /play command.

Defines the GuildPlayer lifecycle, handles yt-dlp downloads, FFmpeg playback,
and interactive UI controls for queue management. Depends on py-cord, yt-dlp,
FFmpeg, config, analytics, and greeting triggers.
"""
import os
import asyncio
import difflib
import unicodedata
import discord
import config
import analytics
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import NamedTuple, Optional
from urllib.parse import urljoin

import aiohttp

from greeting import set_pending_trigger

# Configure a rotating logger for play command steps
playLogger = logging.getLogger("play_logger")
playLogger.setLevel(logging.INFO)
playLogPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play.log")
handler = RotatingFileHandler(playLogPath, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
playLogger.addHandler(handler)
playLogger.propagate = True

# Clean up downloads directory on load to remove stale files
_downloadsDirInit = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
if os.path.exists(_downloadsDirInit):
    for _f in os.listdir(_downloadsDirInit):
        if _f.endswith(".mp3") or _f.endswith(".webm"):
            try:
                os.remove(os.path.join(_downloadsDirInit, _f))
            except Exception:
                pass

# Global dictionary to track active player states per guild
guildPlayers = {}

# Single live music vote per guild. Both the /play picker reactions and the
# indio's chat-message reactions write into the same MusicVote. One at a time —
# opening a new one cancels the previous (e.g. someone asks for another query
# while a vote is still open).
active_votes: "dict[int, 'MusicVote']" = {}

# Sliding window from the most recent vote: a new vote resets it, and when it
# elapses with no further votes the vote resolves to the most-voted option.
_MUSIC_VOTE_WINDOW_SEC = 5.0

# Hard cap from the moment the picker is shown. If nobody votes within this
# window, the vote resolves automatically to candidates[0] (the top fuzzy match
# against the query).
_MUSIC_VOTE_MAX_SEC = 60.0

# How many search candidates to offer when /play (or the indio) finds several
# matches for a free-text query. Keeps the "¿cuál querés?" list readable.
_PLAY_CHOICE_COUNT = 5

# Above this similarity between the user's query and the title of the top
# yt-dlp hit, we skip the "¿cuál querés?" picker and just queue the top
# result. The threshold is a behavioural knob: lower = more autoplay (less
# friction, more risk of playing the wrong song); higher = more picker prompts.
_PLAY_AUTOPLAY_RATIO = 0.55

# Minimum tokens in the user query before we even consider autoplay. Short
# queries ("el infierno", "rock") are inherently ambiguous — even if the top
# hit contains them verbatim, the user is usually browsing, not asking for
# a specific track. The picker handles that case better.
_PLAY_AUTOPLAY_MIN_TOKENS = 3


def _normalize_for_match(s: str) -> str:
    """Lowercase, strip accents, drop punctuation, collapse whitespace.

    Used to compare a user's free-text query against a YouTube title without
    being thrown off by accents, capitalisation, or punctuation like " - ".
    """
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in no_accents)
    return " ".join(cleaned.lower().split())


def _query_title_ratio(query: str, title: str) -> float:
    """Similarity in [0, 1] between a query and a candidate title.

    Uses a partial-ratio: scores how well the query matches the *best aligned
    substring* of the title (the rapidfuzz ``partial_ratio`` idea, built on
    stdlib ``difflib``). This is what lets "el infierno esta encantado esta
    noche" still match a title that prefixes the artist name and suffixes
    "(Audio Oficial)" — plain ``ratio()`` would be dragged down by all that
    extra junk.
    """
    q = _normalize_for_match(query)
    t = _normalize_for_match(title)
    if not q or not t:
        return 0.0
    if len(q) >= len(t):
        return difflib.SequenceMatcher(None, q, t).ratio()
    # Slide a query-sized window across the longer title and keep the best
    # match. Anchored at each matching block found by SequenceMatcher so we
    # avoid the O(n*m) brute force while still catching the right alignment.
    sm = difflib.SequenceMatcher(None, q, t)
    best = 0.0
    for block in sm.get_matching_blocks():
        start = max(0, block.b - block.a)
        window = t[start:start + len(q)]
        score = difflib.SequenceMatcher(None, q, window).ratio()
        if score > best:
            best = score
    return best


def _should_autoplay_top(query: str, title: str,
                         threshold: float = _PLAY_AUTOPLAY_RATIO) -> bool:
    """Return True when the top search hit looks like a clear winner for the
    user's query — i.e. we can queue it directly without showing the picker.

    Two guards combined: the query must be specific enough (≥ ``_PLAY_AUTOPLAY_
    MIN_TOKENS`` tokens) AND the partial-ratio against the title must clear
    ``threshold``. Both are needed; either alone produces wrong calls (long
    vague queries, or short queries that happen to be a verbatim substring).
    """
    if len(_normalize_for_match(query).split()) < _PLAY_AUTOPLAY_MIN_TOKENS:
        return False
    return _query_title_ratio(query, title) >= threshold


# Keycap number emojis used to label the options (display only; the user still
# picks the same way). Index is 1-based via _num_emoji.
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣",
              "5️⃣", "6️⃣", "7️⃣", "8️⃣",
              "9️⃣", "\U0001f51f"]


def _num_emoji(i: int) -> str:
    """Return the keycap emoji for a 1-based position (falls back to ``N.``)."""
    return _NUM_EMOJI[i - 1] if 1 <= i <= len(_NUM_EMOJI) else f"{i}."


def emoji_to_index(emoji_str: str) -> Optional[int]:
    """Return 0-based index for a keycap emoji string, or ``None`` if it isn't
    one of the ones we use to label options. Used by the reaction listener to
    translate a 1️⃣/2️⃣/3️⃣… reaction into a vote index."""
    try:
        return _NUM_EMOJI.index(emoji_str)
    except ValueError:
        return None


class MusicVote:
    """Pure voting state. One MusicVote per guild lives in ``active_votes``.

    Reactions on the picker message (from /play and the indio's chat message)
    write votes into this object via :meth:`register_vote`. The class owns:

    - the candidate list (already fuzzy-reordered upstream so candidates[0]
      is the default if nobody picks),
    - the votes (one per user; revoting replaces),
    - the initial hard cap (``vote_max_sec``) that resolves to candidates[0]
      if nobody votes,
    - the sliding close timer (``vote_window_sec``) from the most recent vote,
    - the callback that resolves the winner — typically ``_play_chosen_song``.

    Closing is idempotent: once it fires, further votes are ignored.
    """

    def __init__(self, *, bot, guild_id: int, candidates: list[dict],
                 on_resolve, vote_window_sec: float = _MUSIC_VOTE_WINDOW_SEC,
                 vote_max_sec: float = _MUSIC_VOTE_MAX_SEC,
                 requester_id: int = 0):
        self.bot = bot
        self.guild_id = guild_id
        self.candidates = candidates
        self.vote_window_sec = vote_window_sec
        self.vote_max_sec = vote_max_sec
        self.votes: dict[int, int] = {}        # user_id -> option index (0-based)
        self._closed = False
        self._close_task: Optional[asyncio.Task] = None
        self._on_resolve = on_resolve          # async fn(MusicVote, winner_dict)
        # Discord id of the user who triggered the vote (via /play or via the
        # indio's voice/chat request). Voice votes are restricted to this id
        # so a music poll doesn't keep spawning while the requester deliberates.
        # 0 means "unknown" → no restriction (fallback to legacy behaviour).
        self.requester_id: int = int(requester_id or 0)
        # The picker message used for the reaction UI. Set by callers after
        # they post the prompt so the close cleanup can clear the reactions.
        self.reaction_message_id: Optional[int] = None
        self.reaction_channel_id: Optional[int] = None

    @property
    def closed(self) -> bool:
        return self._closed

    def start_timeout(self) -> None:
        """Arm the initial hard cap. If nobody votes within ``vote_max_sec`` the
        vote auto-resolves to candidates[0]. Cancelled and replaced by the
        sliding window as soon as the first vote arrives."""
        if self._closed:
            return
        if self._close_task is not None and not self._close_task.done():
            self._close_task.cancel()
        self._close_task = asyncio.create_task(self._close_after(self.vote_max_sec))

    def register_vote(self, user_id: int, idx: int, *,
                      close_now: bool = False) -> bool:
        """Record one user's vote. Returns True if the vote was accepted.

        ``close_now=True`` is the "voice override": the indio gets a verbal
        "ponela 4" command and we resolve immediately instead of waiting for
        the sliding window — the user has already committed, no point waiting
        for others.
        """
        if self._closed:
            return False
        if not (0 <= idx < len(self.candidates)):
            return False
        self.votes[user_id] = idx
        if close_now:
            # Cancel any pending sliding close and resolve right now.
            if self._close_task is not None and not self._close_task.done():
                self._close_task.cancel()
            self._close_task = asyncio.create_task(self._close())
        else:
            self._schedule_close()
        return True

    def _schedule_close(self) -> None:
        """(Re)start the sliding close timer at ``vote_window_sec`` from now."""
        if self._close_task is not None and not self._close_task.done():
            self._close_task.cancel()
        self._close_task = asyncio.create_task(self._close_after(self.vote_window_sec))

    async def _close_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._close()

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Pop from the active registry so a future query opens a fresh vote.
        if active_votes.get(self.guild_id) is self:
            active_votes.pop(self.guild_id, None)
        # Lift the userbot's voice-input restriction so the next utterance from
        # anyone is heard again. Fire-and-forget — main bot's defence-in-depth
        # filter catches anything that races through the closing window.
        asyncio.create_task(_notify_userbot_vote_restriction(self.guild_id, None))
        winner = self._tally_winner()
        # Best-effort: drop the seeded reactions so the closed picker doesn't
        # invite latecomers to keep tapping.
        if self.reaction_message_id and self.reaction_channel_id:
            try:
                channel = self.bot.get_channel(int(self.reaction_channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(self.reaction_channel_id))
                msg = await channel.fetch_message(int(self.reaction_message_id))
                await msg.clear_reactions()
            except Exception:
                pass
        try:
            await self._on_resolve(self, winner)
        except Exception:
            playLogger.exception("[VOTE] on_resolve failed")

    def _tally_winner(self) -> dict:
        """Most-voted option wins; ties → lowest index; no votes → first."""
        if not self.votes:
            return self.candidates[0]
        counts: dict[int, int] = {}
        for idx in self.votes.values():
            counts[idx] = counts.get(idx, 0) + 1
        best = max(counts.values())
        winner_idx = min(i for i in counts if counts[i] == best)
        return self.candidates[winner_idx]


def open_music_vote(*, bot, guild_id: int, candidates: list[dict],
                    on_resolve,
                    vote_window_sec: Optional[float] = None,
                    vote_max_sec: Optional[float] = None,
                    requester_id: int = 0) -> MusicVote:
    """Open a fresh music vote for ``guild_id``. If another vote is currently
    active in the same guild it's cancelled — only one live picker at a time.

    The ``vote_window_sec`` / ``vote_max_sec`` defaults are looked up at call
    time so tests can monkeypatch the module-level constants to speed up
    timing-sensitive scenarios.

    ``requester_id`` is the Discord id of the user who triggered the vote
    (``ctx.author.id`` for /play, the voice/chat speaker's id for indio-driven
    votes). It scopes voice voting to that user so the bot doesn't keep
    spawning new votes while the requester deliberates.
    """
    prev = active_votes.get(guild_id)
    if prev is not None and not prev._closed:
        prev._closed = True
        if prev._close_task is not None and not prev._close_task.done():
            prev._close_task.cancel()
    if vote_window_sec is None:
        vote_window_sec = _MUSIC_VOTE_WINDOW_SEC
    if vote_max_sec is None:
        vote_max_sec = _MUSIC_VOTE_MAX_SEC
    vote = MusicVote(bot=bot, guild_id=guild_id, candidates=candidates,
                     on_resolve=on_resolve, vote_window_sec=vote_window_sec,
                     vote_max_sec=vote_max_sec, requester_id=requester_id)
    active_votes[guild_id] = vote
    # Tell the userbot to only feed voice from the requester until the vote
    # closes. Fire-and-forget; if the relay is off or fails, the apiServer's
    # /indio handler still filters as defence-in-depth.
    asyncio.create_task(_notify_userbot_vote_restriction(guild_id, requester_id))
    return vote


async def _notify_userbot_vote_restriction(guild_id: int,
                                           requester_id: Optional[int]) -> None:
    """Tell the userbot which speaker is "the requester" for this guild's
    open music vote (or that no vote is active, when ``requester_id`` is None).

    Best-effort: returns silently if the relay is disabled or unreachable.
    The userbot uses this to drop VOSK feed for non-requester speakers,
    saving Whisper cycles. The main bot also filters on its own side so a
    failed relay call doesn't break correctness — only efficiency.
    """
    base = (config.INDIO_RELAY_URL or "").strip()
    secret = (config.INDIO_RELAY_SECRET or "").strip()
    if not base or not secret:
        return
    url = urljoin(base, "/restrict_speaker")
    payload = {
        "guild_id": str(int(guild_id)),
        "user_id": (str(int(requester_id))
                    if requester_id is not None and int(requester_id) > 0
                    else None),
    }
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    playLogger.warning(
                        "[VOTE-RESTRICT] userbot relay rc=%s for guild=%s requester=%s",
                        resp.status, guild_id, requester_id,
                    )
    except Exception as e:
        playLogger.warning(
            "[VOTE-RESTRICT] userbot relay failed (%s): %s", type(e).__name__, e,
        )


def get_active_vote(guild_id: int) -> Optional[MusicVote]:
    """Return the live vote for a guild, or ``None`` if none is open."""
    v = active_votes.get(guild_id)
    if v is None or v._closed:
        return None
    return v


class YtDlpDiagnosis(NamedTuple):
    """Diagnóstico estructurado de un fallo de yt-dlp.

    ``audience`` indica quién puede resolverlo (``user``, ``admin`` o ``both``).
    ``summary`` es la causa corta para logs/analytics. ``user_step`` y
    ``admin_step`` son los próximos pasos accionables; uno o los dos pueden ser
    ``None`` cuando no aplican.
    """
    audience: str
    summary: str
    user_step: Optional[str]
    admin_step: Optional[str]

    def format(self) -> str:
        parts = []
        if self.user_step:
            parts.append(f"[Usuario] {self.user_step}")
        if self.admin_step:
            parts.append(f"[Admin] {self.admin_step}")
        return " · ".join(parts) if parts else self.summary


def _diag(audience: str, summary: str, user_step: Optional[str] = None,
          admin_step: Optional[str] = None) -> YtDlpDiagnosis:
    return YtDlpDiagnosis(audience, summary, user_step, admin_step)


def _diagnoseYtDlpFailure(stderr: str, returncode: int = 0) -> YtDlpDiagnosis:
    """Mapea stderr de yt-dlp (o exception) a un diagnóstico accionable.

    El diagnóstico distingue si el problema lo arregla el usuario (elegir otro
    video, esperar) o el admin (cookies, yt-dlp viejo, red del server). Los
    casos ambiguos (ej. HTTP 403) devuelven los dos próximos pasos.
    """
    if not stderr:
        if returncode == 2 or "No such file" in str(returncode):
            return _diag("admin", "yt-dlp no está instalado en el server.",
                         admin_step="yt-dlp no está instalado en el server.")
        return _diag("admin", f"yt-dlp falló (returncode={returncode}) sin output.",
                     admin_step=f"yt-dlp falló (returncode={returncode}) sin output. Revisá play.log.")

    s = stderr.lower()
    # Discord/UX-friendly diagnostics ordered by specificity.
    if "sign in to confirm you're not a bot" in s or "confirm you're not a bot" in s:
        return _diag("both", "YouTube pide login (bot-check).",
                     user_step="YouTube pide login para este video. Probá otro link.",
                     admin_step="Bot-check de YouTube: las cookies del server pueden estar caducas — re-exportalas y subílas con upload-cookies-discord-bot.sh.")
    if "sign in to confirm your age" in s or "age-restricted" in s or "age restricted" in s:
        return _diag("both", "Video con restricción de edad.",
                     user_step="El video tiene restricción de edad. Probá otro link.",
                     admin_step="Las cookies del server no tienen login adulto que pase el gate.")
    if "members-only" in s or "members only" in s or "join this channel to get access" in s:
        return _diag("user", "Video members-only.",
                     user_step="El video es members-only del canal — no se puede descargar. Probá otro link.")
    if "private video" in s or "this video is private" in s:
        return _diag("user", "Video privado.",
                     user_step="El video es privado. Probá otro link.")
    if "video unavailable" in s or "this video is unavailable" in s:
        return _diag("user", "Video no disponible.",
                     user_step="Video no disponible (eliminado o bloqueado en tu región). Probá otro link.")
    if "premiere will begin" in s or "premieres in" in s:
        return _diag("user", "Premiere todavía no empezó.",
                     user_step="Es un premiere que todavía no empezó — esperá a que arranque.")
    if "live event will begin" in s or "this live event" in s:
        return _diag("user", "Live todavía no empezó.",
                     user_step="Es un live que todavía no empezó — esperá a que arranque.")
    if "video is no longer available" in s or "copyright" in s:
        return _diag("user", "Video bloqueado por copyright.",
                     user_step="Video bloqueado por copyright. Probá otro link.")
    if "http error 429" in s or "too many requests" in s:
        return _diag("admin", "Rate-limit (HTTP 429) de YouTube.",
                     user_step="YouTube nos rate-limiteó. Esperá unos minutos y reintentá.",
                     admin_step="HTTP 429: bajar concurrencia o esperar a que YouTube nos libere.")
    if "http error 403" in s:
        return _diag("both", "HTTP 403 de YouTube.",
                     user_step="YouTube rechazó la descarga (403). Probá otro link.",
                     admin_step="HTTP 403: probable bot-check o token de stream vencido — revisar cookies / yt-dlp.")
    if "unable to extract" in s and "player response" in s:
        return _diag("admin", "yt-dlp desactualizado.",
                     admin_step="yt-dlp no pudo parsear el video, probablemente está desactualizado. Correr `pip install --upgrade --pre yt-dlp` y reiniciar el service.")
    if "no supported javascript runtime" in s:
        return _diag("admin", "Falta deno en el server.",
                     admin_step="Falta `deno` en el server (yt-dlp lo necesita para resolver JS de YouTube).")
    if "no video formats found" in s or "requested format is not available" in s:
        return _diag("user", "Sin formato de audio disponible.",
                     user_step="Ese video no tiene formato de audio descargable. Probá otro link.")
    if "name or service not known" in s or "temporary failure in name resolution" in s:
        return _diag("admin", "DNS del server falló.",
                     admin_step="El server no resuelve DNS (problema de red).")
    if "connection refused" in s or "connection reset" in s:
        return _diag("admin", "Conexión rechazada/reseteada.",
                     admin_step="Conexión rechazada/reseteada (red del server o YouTube cayéndose). Reintentar.")
    # Fallback: última línea no vacía de stderr, recortada.
    last = next((ln.strip() for ln in reversed(stderr.splitlines()) if ln.strip()), "")
    tail = last[:300] if last else f"yt-dlp falló (returncode={returncode})."
    return _diag("both", tail,
                 user_step="Algo falló al bajar este video. Probá con otro link.",
                 admin_step=f"Revisar `play.log` para el stderr completo. Último: {tail}")

class CancelDownloadView(discord.ui.View):
    """UI view that lets a user cancel the initial yt-dlp download."""
    def __init__(self, player, videoId: str, videoTitle: str):
        """Initialize the cancel button view.

        Args:
            player: GuildPlayer instance that owns the download.
            videoId: YouTube video ID currently downloading.
            videoTitle: Display title for the cancel confirmation.
        """
        super().__init__(timeout=60)
        self.player = player
        self.videoId = videoId
        self.videoTitle = videoTitle

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, custom_id="btn_cancel_dl")
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Cancel button click.

        Args:
            button: The Discord UI button instance.
            interaction: Interaction that triggered the click.

        Side Effects:
            Aborts the download, clears the queue, and disconnects voice.

        Async:
            This function is a coroutine and must be awaited.
        """
        await interaction.response.defer()
        await self.player.cancelDownload(self.videoId, self.videoTitle, interaction)

class GuildPlayer:
    """Per-guild playback state and queue manager."""

    # How many times the auto-resume loop tries to reconnect after a transient
    # voice drop (region change, WS reset) before giving up. Class-level so
    # tests can shrink delays without touching production defaults.
    AUTO_RESUME_ATTEMPTS: int = 3
    AUTO_RESUME_DELAY_SECONDS: float = 2.0

    def __init__(self, guildId: int, bot):
        """Initialize the player state for a guild.

        Args:
            guildId: Discord guild ID.
            bot: Discord bot client.
        """
        self.guildId = guildId
        self.bot = bot
        self.queue = []         # List of {"id": str, "title": str}
        self.history = []       # List of {"id": str, "title": str}
        self.currentSong = None # {"id": str, "title": str} or None
        self.vc = None
        self.controlMessage = None
        self.textChannel = None
        self.isStopping = False
        self.isPrevious = False
        self.lastRequester = None  # discord.Member of the user who last queued songs
        self.isDownloading = False
        self.downloadingIds = set()
        self.preDownloadTask = None
        self.activeDownloadProc = None
        self.initialCtx = None
        # Playback-position tracking + interruption state. Used so the bot can
        # resume the current song from where it was after an involuntary
        # disconnect (kick, network drop, /quit) without losing the queue.
        self.playStartedAt: Optional[float] = None   # monotonic when current playback started/resumed
        self.pausedAt: Optional[float] = None        # monotonic when current pause started
        self.pausedAccumSecs: float = 0.0            # accumulated paused seconds across pauses
        self.interrupted: bool = False               # True between disconnect and next resume
        self.interruptedAtSeconds: float = 0.0       # snapshot of elapsed at interruption time
        # Deferred-connect state: when /play is invoked while the bot is NOT in
        # voice, we postpone joining the channel until the first song finishes
        # downloading. Joining immediately and sitting silent while yt-dlp runs
        # would let the idle watchdog disconnect us before we ever play a note.
        self.pendingVoiceChannel = None              # discord.VoiceChannel | None
        self.pendingTriggerUserId: Optional[int] = None  # for greeting trigger
        # Auto-resume background task: retries reconnecting to the same channel
        # after a transient voice-WS drop (typically a Discord region change),
        # using the preserved interruption snapshot. Tracked here so we can
        # de-dupe concurrent schedules and cancel cleanly on shutdown.
        self._autoResumeTask: Optional["asyncio.Task"] = None

    def _resolveMusicChannel(self, fallback=None):
        """Return the configured music text channel for this guild.

        Falls back to ``fallback`` (typically ``ctx.channel``) if the
        configured channel is missing or not sendable. Used so that the
        control panel and status messages always land in the music channel
        regardless of where ``/play`` was invoked.
        """
        if self.bot is not None:
            guild = self.bot.get_guild(self.guildId)
            if guild is not None:
                ch = guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
                if ch is not None and hasattr(ch, "send"):
                    return ch
        return fallback

    def _currentElapsedSeconds(self) -> float:
        """Return how many seconds of the current song have actually played.

        Compensates for time spent paused via ``pausedAccumSecs`` so it stays
        accurate across multiple pause/resume cycles. Returns 0 when no
        playback has started yet for the current song.
        """
        if self.playStartedAt is None:
            return 0.0
        ref = self.pausedAt if self.pausedAt is not None else time.monotonic()
        elapsed = ref - self.playStartedAt - self.pausedAccumSecs
        return max(0.0, elapsed)

    def mark_interrupted(self) -> None:
        """Mark the player as interrupted by an involuntary disconnect.

        Idempotent. Snapshots the current elapsed position so the next call
        to :meth:`resumeFromInterruption` can seek back. Does **not** clear
        ``currentSong`` or ``queue`` — that's the whole point: preserve them
        in memory for resume. Does null out ``self.vc`` because the voice
        client is dead at this point.
        """
        if self.interrupted or not self.currentSong:
            return
        try:
            self.interruptedAtSeconds = self._currentElapsedSeconds()
        except Exception:
            self.interruptedAtSeconds = 0.0
        self.interrupted = True
        self.vc = None
        playLogger.info(
            "[PLAYBACK INTERRUPTED] '%s' at %.1fs (queue=%d)",
            self.currentSong.get("title", "?"),
            self.interruptedAtSeconds,
            len(self.queue),
        )

    async def resumeFromInterruption(self, vc) -> bool:
        """Re-attach to a freshly-connected voice client and restart playback
        from the saved interruption position.

        Returns:
            True if the player had an interrupted song and resume kicked off.
            False if there was nothing to resume.
        """
        if not self.interrupted or not self.currentSong:
            return False
        self.vc = vc
        self.interrupted = False
        seek = max(0.0, self.interruptedAtSeconds)
        self.interruptedAtSeconds = 0.0
        playLogger.info(
            "[PLAYBACK RESUME-AFTER-INTERRUPTION] '%s' seeking to %.1fs",
            self.currentSong.get("title", "?"),
            seek,
        )
        await self.startPlayingCurrent(seek_seconds=seek)
        return True

    def _scheduleAutoResume(self, channel_id: int) -> None:
        """Spawn a background task that retries reconnecting to ``channel_id``
        and resumes the interrupted song.

        Called only from ``onSongFinished`` when the voice client died mid-stream
        while the bot was still meant to be in the channel (typical signature of
        a Discord region change or transient WS reset). Kicks and ``/quit`` go
        through ``on_voice_state_update`` which marks the player interrupted
        without scheduling auto-resume — reconnecting after those would be wrong.

        Idempotent: a still-running task is not replaced. Skips scheduling when
        the bot can't resolve the guild — that case is unrecoverable and the
        player just stays interrupted until ``/play`` retries manually.
        """
        existing = self._autoResumeTask
        if existing is not None and not existing.done():
            return
        guild = self.bot.get_guild(self.guildId) if self.bot is not None else None
        if guild is None or not hasattr(guild, "get_channel"):
            return
        try:
            self._autoResumeTask = asyncio.create_task(
                self._autoResumeLoop(channel_id)
            )
        except RuntimeError:
            # No running loop (rare — happens in some non-async test setups).
            self._autoResumeTask = None

    async def _autoResumeLoop(self, channel_id: int) -> None:
        """Retry reconnecting + resuming for up to ``AUTO_RESUME_ATTEMPTS``
        cycles, then give up silently and leave the player interrupted.

        Bails out early if another path resumed first (``not self.interrupted``)
        or the channel/guild disappeared from cache.
        """
        attempts = max(1, self.AUTO_RESUME_ATTEMPTS)
        delay = max(0.0, self.AUTO_RESUME_DELAY_SECONDS)
        try:
            for attempt in range(attempts):
                await asyncio.sleep(delay)
                if not self.interrupted or not self.currentSong:
                    return
                guild = self.bot.get_guild(self.guildId) if self.bot is not None else None
                channel = None
                if guild is not None:
                    try:
                        channel = guild.get_channel(channel_id)
                    except Exception:
                        channel = None
                if channel is None:
                    playLogger.info(
                        "[AUTO-RESUME] channel %s not available, giving up",
                        channel_id,
                    )
                    return
                try:
                    vc = await channel.connect(reconnect=True)
                except Exception as exc:
                    playLogger.warning(
                        "[AUTO-RESUME] reconnect attempt %d/%d failed: %s",
                        attempt + 1, attempts, exc,
                    )
                    continue
                try:
                    ok = await self.resumeFromInterruption(vc)
                except Exception as exc:
                    playLogger.warning(
                        "[AUTO-RESUME] resume after reconnect failed: %s", exc,
                    )
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
                    continue
                if ok:
                    playLogger.info(
                        "[AUTO-RESUME] reconnected and resumed on channel %s",
                        channel_id,
                    )
                    return
            playLogger.info(
                "[AUTO-RESUME] giving up after %d attempts on channel %s",
                attempts, channel_id,
            )
        finally:
            self._autoResumeTask = None

    async def _enqueueAndMaybeStart(self, songs, *, source: Optional[str] = None):
        """Shared enqueue → analytics → maybe-start-playback core.

        Used by both the /play slash entrypoint (via ``addSongs``) and
        programmatic entrypoints (``playFromIndio``). The caller is
        responsible for posting any user-facing status message; this method
        only manages internal player state.

        Args:
            songs: List of dicts with id/title metadata.
            source: Optional tag for analytics (e.g. ``"indio"``).

        Side Effects:
            Mutates queue/currentSong, fires analytics, kicks off playback
            or pre-download.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.queue.extend(songs)

        guild = self.bot.get_guild(self.guildId) if self.bot else None
        props = {
            "count": len(songs),
            "queue_length": len(self.queue),
            "first_title": songs[0]["title"] if songs else None,
        }
        if source:
            props["source"] = source
        analytics.capture("play songs queued", user=self.lastRequester,
                          guild=guild, properties=props)

        if not self.currentSong and self.queue:
            self.currentSong = self.queue.pop(0)
            await self.startPlayingCurrent()
        else:
            await self.updateControlMessage()
            self.startPreDownloading()

    async def addSongs(self, songs, ctx):
        """Add one or more songs to the queue and start playback if idle.

        Args:
            songs: List of dicts with id/title metadata.
            ctx: Discord application context.

        Returns:
            None.

        Side Effects:
            Updates queue, sends Discord messages, and may start playback.

        Async:
            This function is a coroutine and must be awaited.
        """
        from bot import safeEdit

        self.textChannel = self._resolveMusicChannel(fallback=ctx.channel)
        self.lastRequester = ctx.author

        isFirst = (not self.currentSong and len(self.queue) == 0)
        if isFirst:
            # startPlayingCurrent will delete this interaction's original
            # response once playback actually starts.
            self.initialCtx = ctx

        estimatedTime = int(time.time() + 30)
        if len(songs) > 1:
            if isFirst:
                view = CancelDownloadView(self, songs[0]["id"], f"Playlist ({len(songs)} canciones)")
                await ctx.interaction.edit_original_response(content=f"✅ Descargando playlist (se añadieron **{len(songs)}** canciones, iniciando con **{songs[0]['title']}**... <t:{estimatedTime}:R>)", view=view)
            else:
                await safeEdit(ctx, f"✅ Se añadieron **{len(songs)}** canciones a la cola.")
        else:
            if isFirst:
                view = CancelDownloadView(self, songs[0]["id"], songs[0]["title"])
                await ctx.interaction.edit_original_response(content=f"✅ Descargando: **{songs[0]['title']}**... <t:{estimatedTime}:R>", view=view)
            else:
                await safeEdit(ctx, f"✅ Se añadió **{songs[0]['title']}** a la cola.")

        await self._enqueueAndMaybeStart(songs)

    async def cancelDownload(self, videoId: str, videoTitle: str, interaction: discord.Interaction):
        """Cancel an active download and reset playback state.

        Args:
            videoId: Video ID to cancel.
            videoTitle: Display name for notifications.
            interaction: Interaction used to edit the response.

        Side Effects:
            Kills download subprocess, clears queue, and disconnects voice.

        Async:
            This function is a coroutine and must be awaited.
        """
        playLogger.info(f"[DOWNLOAD CANCEL] User cancelled download for '{videoTitle}' (ID: {videoId})")
        if self.activeDownloadProc:
            try:
                self.activeDownloadProc.kill()
            except Exception:
                pass
        self.queue.clear()
        self.currentSong = None
        self.isDownloading = False
        self.downloadingIds.discard(videoId)
        self.initialCtx = None
        # If we deferred the voice connect, drop the pending target so a later
        # /play starts from a clean slate.
        self.pendingVoiceChannel = None
        self.pendingTriggerUserId = None
        try:
            await interaction.edit_original_response(content=f"❌ Descarga cancelada: **{videoTitle}**.", view=None)
        except Exception:
            pass
        if self.vc:
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
            self.vc = None

    async def startPlayingCurrent(self, *, seek_seconds: float = 0.0):
        """Ensure the current song is downloaded and start playback.

        Args:
            seek_seconds: Position (in seconds) to seek to when launching
                FFmpeg. Used by :meth:`resumeFromInterruption` to pick up
                where playback was cut off. ``0`` (the default) plays from
                the start of the file.

        Returns:
            None.

        Side Effects:
            Downloads audio with yt-dlp, plays via FFmpeg, updates UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not self.currentSong:
            return

        videoId = self.currentSong["id"]
        videoTitle = self.currentSong["title"]

        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(downloadsDir, exist_ok=True)
        filepath = os.path.join(downloadsDir, f"{videoId}.mp3")

        # Guild is sourced from the bot (not self.vc) because we may not be
        # connected yet — the connect is deferred until after the download.
        guild = self.bot.get_guild(self.guildId) if self.bot else None

        # Wait if currently downloading in background
        if videoId in self.downloadingIds:
            playLogger.info(f"[PLAYBACK WAIT] Song '{videoTitle}' (ID: {videoId}) is downloading in background. Waiting...")
            while videoId in self.downloadingIds:
                await asyncio.sleep(0.5)

        # Download song if not already cached
        if not os.path.exists(filepath):
            self.isDownloading = True
            self.downloadingIds.add(videoId)
            if self.controlMessage is not None:
                await self.updateControlMessage()
            playLogger.info(f"[DOWNLOAD START] Downloading '{videoTitle}' (ID: {videoId})...")
            startTime = time.time()
            try:
                ytDlpPath = config.YT_DLP_PATH
                inputStr = f"https://www.youtube.com/watch?v={videoId}"
                cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
                ytDlpArgs = [ytDlpPath]
                if os.path.exists(cookiesPath):
                    ytDlpArgs += ["--cookies", cookiesPath]
                if config.YT_DLP_POT_BASE_URL:
                    ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
                ytDlpArgs += [
                    "-x",
                    "--audio-format", "mp3",
                    "--no-playlist",
                    "-o", os.path.join(downloadsDir, "%(id)s.%(ext)s"),
                    inputStr,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ytDlpArgs,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                self.activeDownloadProc = proc
                stdout, stderr = await proc.communicate()
                self.activeDownloadProc = None

                if not self.currentSong:
                    # Download was cancelled
                    return

                if proc.returncode != 0:
                    self.isDownloading = False
                    stderrStr = stderr.decode('utf-8', errors='replace')
                    errTail = stderrStr.strip().splitlines()[-5:]
                    errMsg = " | ".join(errTail)
                    diag = _diagnoseYtDlpFailure(stderrStr, proc.returncode)
                    reason = diag.format()
                    analytics.capture("play song failed", user=self.lastRequester, guild=guild,
                                      properties={"stage": "download", "video_id": videoId,
                                                  "title": videoTitle, "returncode": proc.returncode,
                                                  "stderr_tail": errMsg[:500], "reason": reason,
                                                  "audience": diag.audience, "summary": diag.summary})
                    playLogger.error(f"[DOWNLOAD FAIL] Failed to download '{videoTitle}' (ID: {videoId}) with returncode {proc.returncode}. audience: {diag.audience}. summary: {diag.summary}. stderr: {errMsg}")
                    if self.initialCtx:
                        try:
                            await self.initialCtx.interaction.edit_original_response(
                                content=f"❌ Error al descargar **{videoTitle}**: {reason}",
                                view=None
                            )
                        except Exception:
                            pass
                        self.initialCtx = None
                    if self.controlMessage is not None:
                        await self.updateControlMessage(f"❌ Error al descargar {videoTitle}: {reason}")
                    # Skip to next song
                    self.bot.loop.create_task(self.skipSong())
                    return
                else:
                    duration = time.time() - startTime
                    fileSize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    playLogger.info(f"[DOWNLOAD SUCCESS] Successfully downloaded '{videoTitle}' (ID: {videoId}) in {duration:.2f}s. File size: {fileSize} bytes ({fileSize / (1024*1024):.2f} MB)")
            except Exception as e:
                self.activeDownloadProc = None
                if not self.currentSong:
                    # Download was cancelled
                    return
                self.isDownloading = False
                if isinstance(e, FileNotFoundError):
                    diag = _diag("admin",
                                 f"yt-dlp no encontrado en {config.YT_DLP_PATH}.",
                                 admin_step=f"yt-dlp no encontrado en `{config.YT_DLP_PATH}`. Revisá YT_DLP_PATH en .env.")
                else:
                    diag = _diagnoseYtDlpFailure(str(e))
                reason = diag.format()
                print(f"[PLAYER ERROR] Download exception: {e}")
                analytics.capture_exception(e, user=self.lastRequester, guild=guild,
                                            properties={"stage": "download", "video_id": videoId,
                                                        "title": videoTitle, "reason": reason,
                                                        "audience": diag.audience, "summary": diag.summary})
                playLogger.error(f"[DOWNLOAD ERROR] Exception downloading '{videoTitle}': {e} → audience: {diag.audience}. summary: {diag.summary}")
                if self.initialCtx:
                    try:
                        await self.initialCtx.interaction.edit_original_response(
                            content=f"❌ Error al descargar **{videoTitle}**: {reason}",
                            view=None
                        )
                    except Exception:
                        pass
                    self.initialCtx = None
                if self.controlMessage is not None:
                    await self.updateControlMessage(f"❌ Error al descargar {videoTitle}: {reason}")
                self.bot.loop.create_task(self.skipSong())
                return
            finally:
                self.isDownloading = False
                self.downloadingIds.discard(videoId)
                self.activeDownloadProc = None

        # Lazy voice-connect: when /play was issued without an existing voice
        # client we deferred joining until now. Joining at this point (file
        # ready on disk → ffmpeg starts immediately) keeps the silent-in-channel
        # window at ~0, so the idle watchdog can't disconnect us mid-download.
        if self.vc is None and self.pendingVoiceChannel is not None:
            target = self.pendingVoiceChannel
            trigger_user = self.pendingTriggerUserId or 0
            self.pendingVoiceChannel = None
            self.pendingTriggerUserId = None
            try:
                set_pending_trigger(target.id, trigger_user)
                self.vc = await target.connect(reconnect=True)
            except Exception as e:
                playLogger.exception("[PLAY] deferred voice connect failed")
                analytics.capture_exception(
                    e, user=self.lastRequester, guild=guild,
                    properties={"stage": "deferred_connect",
                                "video_id": videoId, "title": videoTitle},
                )
                if self.initialCtx:
                    try:
                        await self.initialCtx.interaction.edit_original_response(
                            content=f"❌ No pude conectarme a voz: {e}",
                            view=None,
                        )
                    except Exception:
                        pass
                    self.initialCtx = None
                if self.controlMessage is not None:
                    await self.updateControlMessage(
                        f"❌ No pude conectarme a voz para reproducir **{videoTitle}**: {e}"
                    )
                self.currentSong = None
                return

        if not self.vc:
            playLogger.error(
                "[PLAYBACK ERROR] vc=None and no pending channel; aborting '%s'",
                videoTitle,
            )
            self.currentSong = None
            return

        # Start playback
        try:
            # Delete/cleanup the initial downloading message
            if self.initialCtx:
                try:
                    await self.initialCtx.interaction.delete_original_response()
                except Exception as e:
                    playLogger.warning(f"[PLAYBACK START] Could not delete original response: {e}")
                self.initialCtx = None

            if seek_seconds and seek_seconds > 0:
                audioSource = discord.FFmpegOpusAudio(
                    filepath, before_options=f"-ss {seek_seconds:.2f}",
                )
            else:
                audioSource = discord.FFmpegOpusAudio(filepath)

            def afterCallback(error):
                asyncio.run_coroutine_threadsafe(self.onSongFinished(error), self.bot.loop)

            self.vc.play(audioSource, after=afterCallback)
            # Reset elapsed-time tracking. When resuming from a seek the
            # virtual start is shifted back so ``_currentElapsedSeconds``
            # continues to report the real position within the song.
            self.playStartedAt = time.monotonic() - (seek_seconds or 0.0)
            self.pausedAt = None
            self.pausedAccumSecs = 0.0
            self.interrupted = False
            self.interruptedAtSeconds = 0.0
            analytics.capture("play song started", user=self.lastRequester, guild=guild,
                               properties={"video_id": videoId, "title": videoTitle,
                                           "queue_length": len(self.queue),
                                           "seek_seconds": seek_seconds or 0.0})
            await self.updateControlMessage()
            playLogger.info(f"[PLAYBACK START] Started playing '{videoTitle}' (ID: {videoId}) seek={seek_seconds:.1f}s")
            
            # Start background pre-downloading of the queue
            self.startPreDownloading()
        except Exception as e:
            print(f"[PLAYER ERROR] Playback start exception: {e}")
            analytics.capture_exception(e, user=self.lastRequester, guild=guild,
                                         properties={"stage": "play", "video_id": videoId, "title": videoTitle})
            playLogger.error(f"[PLAYBACK ERROR] Playback start exception for '{videoTitle}': {e}")
            await self.updateControlMessage(f"❌ Error al reproducir {videoTitle}: {e}")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            self.bot.loop.create_task(self.skipSong())

    async def onSongFinished(self, error):
        """Handle playback completion and advance the queue.

        Args:
            error: Playback error passed by Discord (if any).

        Side Effects:
            Deletes temporary files, mutates queue/history, updates UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if error:
            print(f"[PLAYER] Playback error: {error}")
            playLogger.error(f"[PLAYBACK ERROR] Playback finished with error for '{self.currentSong['title'] if self.currentSong else 'Unknown'}': {error}")

        # Defensive: if the voice client died (kick / network drop / region
        # change) and the caller didn't already flag the interruption via
        # mark_interrupted, detect it here so we don't lose the current song
        # to the cleanup below. ``isStopping`` / ``isPrevious`` are explicit
        # user actions and must continue past this gate.
        if (not self.interrupted
                and not self.isStopping
                and not self.isPrevious
                and self.currentSong is not None
                and self.vc is not None):
            try:
                connected = bool(self.vc.is_connected())
            except Exception:
                connected = True
            if not connected:
                # Capture the channel id BEFORE mark_interrupted nulls self.vc
                # so the auto-resume loop knows where to reconnect.
                channel_id = None
                try:
                    if self.vc.channel is not None:
                        channel_id = self.vc.channel.id
                except Exception:
                    channel_id = None
                self.mark_interrupted()
                # This path fires when the WS died mid-stream while the bot
                # was still "supposed" to be in the channel — that's the
                # region-change signature. on_voice_state_update handles real
                # kicks/quits separately and does not call this scheduler.
                if channel_id is not None:
                    self._scheduleAutoResume(channel_id)

        if self.interrupted:
            try:
                title = self.currentSong["title"] if self.currentSong else "?"
                await self.updateControlMessage(
                    f"⚠️ Conexión perdida — **{title}** quedó en "
                    f"{int(self.interruptedAtSeconds)}s. Pedile que retome con /play."
                )
            except Exception:
                pass
            return

        # Delete the file of the finished song
        if self.currentSong:
            videoId = self.currentSong["id"]
            downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            try:
                if os.path.exists(filepath):
                    fileSize = os.path.getsize(filepath)
                    os.remove(filepath)
                    playLogger.info(f"[CLEANUP] Deleted temporary file for '{self.currentSong['title']}' (ID: {videoId}). Size: {fileSize} bytes")
            except Exception as e:
                print(f"[PLAYER] Error deleting file {filepath}: {e}")
                playLogger.error(f"[CLEANUP ERROR] Error deleting file {filepath}: {e}")

        # Stop action
        if self.isStopping:
            self.isStopping = False
            title = self.currentSong["title"] if self.currentSong else "Unknown"
            playLogger.info(f"[PLAYBACK STOP] Playback stopped. Queue cleared.")
            self.currentSong = None
            await self.updateControlMessage("⏹️ Reproducción detenida y cola vaciada.")
            # The idle watchdog will disconnect us shortly — single source of
            # truth for leaving voice.
            return

        # Previous action
        if self.isPrevious:
            self.isPrevious = False
            if self.history:
                if self.currentSong:
                    self.queue.insert(0, self.currentSong)
                self.currentSong = self.history.pop()
                playLogger.info(f"[PLAYBACK PREVIOUS] Loading previous song: '{self.currentSong['title']}'")
                await self.startPlayingCurrent()
            else:
                self.currentSong = None
                await self.updateControlMessage("⚠️ No hay canciones anteriores.")
            return

        # Skip or natural finish: add current song to history
        if self.currentSong:
            playLogger.info(f"[PLAYBACK FINISH] Finished playing '{self.currentSong['title']}' (ID: {self.currentSong['id']})")
            self.history.append(self.currentSong)

        # Play next in queue
        if self.queue:
            self.currentSong = self.queue.pop(0)
            await self.startPlayingCurrent()
        else:
            self.currentSong = None
            await self.updateControlMessage("⏹️ Fin de la cola de reproducción.")
            # The idle watchdog observes the now-quiet vc and disconnects.

    async def predownloadQueue(self):
        """Background task that pre-downloads queued songs.

        Side Effects:
            Downloads audio files to disk and updates download state.

        Async:
            This function is a coroutine and must be awaited.
        """
        # We run a loop as long as the player is active and there are items in the queue
        while self.vc and (self.vc.is_playing() or self.vc.is_paused()) and self.queue:
            # Find the first item in the queue that is not yet downloaded and not currently downloading
            targetSong = None
            for song in self.queue:
                vid = song["id"]
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", f"{vid}.mp3")
                if not os.path.exists(path) and vid not in self.downloadingIds:
                    targetSong = song
                    break
            
            if not targetSong:
                # All songs in queue are either downloaded or currently downloading
                break
                
            videoId = targetSong["id"]
            videoTitle = targetSong["title"]
            downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            
            self.downloadingIds.add(videoId)
            playLogger.info(f"[PRE-DOWNLOAD START] Background downloading queue item '{videoTitle}' (ID: {videoId})...")
            startTime = time.time()
            try:
                ytDlpPath = config.YT_DLP_PATH
                inputStr = f"https://www.youtube.com/watch?v={videoId}"
                cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
                ytDlpArgs = [ytDlpPath]
                if os.path.exists(cookiesPath):
                    ytDlpArgs += ["--cookies", cookiesPath]
                if config.YT_DLP_POT_BASE_URL:
                    ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
                ytDlpArgs += [
                    "-x",
                    "--audio-format", "mp3",
                    "--no-playlist",
                    "-o", os.path.join(downloadsDir, "%(id)s.%(ext)s"),
                    inputStr,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ytDlpArgs,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    duration = time.time() - startTime
                    fileSize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    playLogger.info(f"[PRE-DOWNLOAD SUCCESS] Background downloaded '{videoTitle}' (ID: {videoId}) in {duration:.2f}s. Size: {fileSize} bytes ({fileSize / (1024*1024):.2f} MB)")
                else:
                    errTail = stderr.decode('utf-8', errors='replace').strip().splitlines()[-5:]
                    errMsg = " | ".join(errTail)
                    playLogger.warning(f"[PRE-DOWNLOAD FAIL] Failed to background download '{videoTitle}' (ID: {videoId}) with code {proc.returncode}. stderr: {errMsg}")
            except Exception as e:
                playLogger.error(f"[PRE-DOWNLOAD ERROR] Exception background downloading '{videoTitle}': {e}")
            finally:
                self.downloadingIds.discard(videoId)
                
            # Sleep slightly between downloads to avoid high CPU load/network spam
            await asyncio.sleep(1)

    def startPreDownloading(self):
        """Ensure the background pre-download task is running."""
        if not self.preDownloadTask or self.preDownloadTask.done():
            self.preDownloadTask = self.bot.loop.create_task(self.predownloadQueue())

    async def togglePausePlay(self):
        """Pause or resume playback based on current state.

        Side Effects:
            Calls pause/resume on the voice client and updates UI. Maintains
            elapsed-time accounting so the position stays accurate across
            multiple pause/resume cycles.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.vc:
            if self.vc.is_playing():
                self.vc.pause()
                self.pausedAt = time.monotonic()
                await self.updateControlMessage()
            elif self.vc.is_paused():
                if self.pausedAt is not None:
                    self.pausedAccumSecs += time.monotonic() - self.pausedAt
                    self.pausedAt = None
                self.vc.resume()
                await self.updateControlMessage()

    async def skipSong(self):
        """Skip the current song and advance the queue.

        Side Effects:
            Stops playback and triggers queue advancement.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def playPrevious(self):
        """Play the previous song from history.

        Side Effects:
            Mutates queue/history and restarts playback.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not self.history:
            return
        self.isPrevious = True
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def stopPlayback(self):
        """Stop playback and clear the queue.

        Side Effects:
            Clears queue state and stops the voice client if active.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.isStopping = True
        self.queue.clear()
        self.isDownloading = False
        if self.vc:
            if self.vc.is_playing() or self.vc.is_paused():
                self.vc.stop()
            else:
                await self.onSongFinished(None)

    async def updateControlMessage(self, customStatus=None):
        """Create or update the interactive control message.

        Args:
            customStatus: Optional status line override.

        Side Effects:
            Sends or edits a Discord embed with UI controls.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not self.textChannel:
            return

        embed = discord.Embed(title="🎵 Reproductor de Música", color=discord.Color.blurple())

        # Determine status text
        if customStatus:
            status = customStatus
        elif getattr(self, "isDownloading", False) and self.currentSong:
            status = f"⬇️ Descargando: **{self.currentSong['title']}**"
        elif self.vc and self.vc.is_paused():
            durStr = self.currentSong.get("duration_string", "")
            durSuffix = f" `[{durStr}]`" if durStr else ""
            status = f"⏸️ Pausado: **{self.currentSong['title']}**{durSuffix}"
        elif self.vc and self.vc.is_playing():
            durStr = self.currentSong.get("duration_string", "")
            durSuffix = f" `[{durStr}]`" if durStr else ""
            status = f"▶️ Reproduciendo: **{self.currentSong['title']}**{durSuffix}"
        else:
            status = "⏹️ Sin reproducción activa."

        embed.description = status

        # Queue list
        if self.queue:
            queueLines = []
            for i, song in enumerate(self.queue[:5]):
                durStr = song.get("duration_string", "")
                durSuffix = f" `[{durStr}]`" if durStr else ""
                queueLines.append(f"{i+1}. {song['title']}{durSuffix}")
            if len(self.queue) > 5:
                queueLines.append(f"... y {len(self.queue) - 5} más.")
            embed.add_field(name="📋 Siguientes en cola", value="\n".join(queueLines), inline=False)
        else:
            embed.add_field(name="📋 Siguientes en cola", value="La cola está vacía.", inline=False)

        # History footer
        if self.history:
            embed.set_footer(text=f"Canciones en historial: {len(self.history)}")

        view = PlayerControlView(self)

        try:
            if self.controlMessage:
                await self.controlMessage.edit(embed=embed, view=view)
            else:
                self.controlMessage = await self.textChannel.send(embed=embed, view=view)
        except Exception as e:
            print(f"[PLAYER] Error updating control message: {e}")
            try:
                self.controlMessage = await self.textChannel.send(embed=embed, view=view)
            except Exception:
                pass


class PlayerControlView(discord.ui.View):
    """Playback control buttons for the GuildPlayer UI."""
    def __init__(self, player: GuildPlayer):
        """Initialize the control view for a player.

        Args:
            player: GuildPlayer instance to control.
        """
        super().__init__(timeout=None)
        self.player = player
        self.updateButtonStates()

    def updateButtonStates(self):
        """Update button labels and disabled state based on player status."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "btn_prev":
                    child.disabled = len(self.player.history) == 0
                elif child.custom_id == "btn_pause_play":
                    if self.player.vc and self.player.vc.is_paused():
                        child.label = "▶️ Reanudar"
                        child.style = discord.ButtonStyle.success
                    else:
                        child.label = "⏸️ Pausar"
                        child.style = discord.ButtonStyle.primary
                elif child.custom_id == "btn_next":
                    child.disabled = len(self.player.queue) == 0

    @discord.ui.button(label="⏮️ Anterior", style=discord.ButtonStyle.secondary, custom_id="btn_prev")
    async def previousButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Previous button click."""
        await interaction.response.defer()
        await self.player.playPrevious()

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.primary, custom_id="btn_pause_play")
    async def pausePlayButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Pause/Resume button click."""
        await interaction.response.defer()
        await self.player.togglePausePlay()

    @discord.ui.button(label="⏭️ Siguiente", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def nextButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Next button click."""
        await interaction.response.defer()
        await self.player.skipSong()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def stopButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Stop button click."""
        await interaction.response.defer()
        await self.player.stopPlayback()


def getGuildPlayer(guildId: int, bot) -> GuildPlayer:
    """Return or create the GuildPlayer for a guild.

    Args:
        guildId: Discord guild ID.
        bot: Discord bot client.

    Returns:
        GuildPlayer instance bound to the guild.
    """
    if guildId not in guildPlayers:
        guildPlayers[guildId] = GuildPlayer(guildId, bot)
    return guildPlayers[guildId]

def clearGuildPlayer(guildId: int):
    """Clear a GuildPlayer and delete any queued downloads.

    Args:
        guildId: Discord guild ID.

    Side Effects:
        Cancels background tasks, deletes cached audio files, and clears state.
    """
    if guildId in guildPlayers:
        player = guildPlayers[guildId]
        # Cancel background downloader task
        if getattr(player, "preDownloadTask", None) and not player.preDownloadTask.done():
            player.preDownloadTask.cancel()
            playLogger.info(f"[CLEANUP] Cancelled active background pre-download task for guild {guildId}")

        # Delete all files in queue and currentSong
        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        filesToDelete = []
        if player.currentSong:
            filesToDelete.append(player.currentSong["id"])
        for song in player.queue:
            filesToDelete.append(song["id"])
            
        for videoId in filesToDelete:
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            webmpath = os.path.join(downloadsDir, f"{videoId}.webm")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    playLogger.info(f"[CLEANUP] Cleaned up file {filepath}")
            except Exception:
                pass
            try:
                if os.path.exists(webmpath):
                    os.remove(webmpath)
            except Exception:
                pass

        player.queue.clear()
        player.history.clear()
        player.currentSong = None
        player.vc = None
        player.controlMessage = None
        player.textChannel = None
        player.isDownloading = False
        del guildPlayers[guildId]

def _format_choice_prompt(candidates: list[dict]) -> str:
    """Render a numbered list of search candidates for the "¿cuál querés?"
    prompt. Shared shape used by the /play picker message."""
    lines = ["🎵 Encontré varias, ¿cuál querés?"]
    for i, c in enumerate(candidates, 1):
        dur = c.get("duration_string") or ""
        durSuffix = f" `[{dur}]`" if dur else ""
        lines.append(f"{_num_emoji(i)} {c['title']}{durSuffix}")
    return "\n".join(lines)


async def playLogic(ctx: discord.ApplicationContext, query: str):
    """Handle the /play slash command.

    Args:
        ctx: Discord application context.
        query: Search term or YouTube URL.

    Returns:
        None.

    Side Effects:
        Connects to voice, downloads audio with yt-dlp, and starts playback.

    Async:
        This function is a coroutine and must be awaited.
    """
    from bot import safe_defer, safe_respond, safeEdit

    if not await safe_defer(ctx):
        return

    # Ensure user is in a voice channel
    if not ctx.author.voice:
        return await safe_respond(ctx, "❌ ¡Debes estar en un canal de voz!")

    channel = ctx.author.voice.channel

    player = getGuildPlayer(ctx.guild.id, ctx.bot)
    player.textChannel = player._resolveMusicChannel(fallback=ctx.channel)

    # Voice-connect strategy:
    # - If we already have a voice client, reuse / move it as before. Resume
    #   any interrupted playback from the snapshotted position.
    # - If the player is in the "interrupted" state we MUST reconnect now so
    #   the saved song can keep playing from where it was cut off.
    # - Otherwise, defer the connect until the first song finishes downloading
    #   (see startPlayingCurrent). Joining the channel immediately and sitting
    #   silent during yt-dlp is what lets the idle watchdog kick us out before
    #   we ever play a note.
    if ctx.voice_client is not None:
        vc = ctx.voice_client
        if vc.channel.id != channel.id:
            if getattr(vc, "recording", False):
                try:
                    vc.stop_recording()
                except Exception:
                    pass
                setattr(vc, "recording", False)
            set_pending_trigger(channel.id, ctx.author.id)
            await vc.move_to(channel)
        if player.interrupted and player.currentSong:
            try:
                await player.resumeFromInterruption(vc)
            except Exception:
                playLogger.exception("[PLAY] resumeFromInterruption failed; falling back to fresh play")
                player.interrupted = False
                player.currentSong = None
        else:
            player.vc = vc
    elif player.interrupted and player.currentSong:
        # Saved song to resume but no live vc — reconnect now (file is still
        # cached because onSongFinished short-circuits when interrupted).
        try:
            set_pending_trigger(channel.id, ctx.author.id)
            vc = await channel.connect(reconnect=True)
            await player.resumeFromInterruption(vc)
        except Exception:
            playLogger.exception("[PLAY] reconnect-to-resume failed; falling back to fresh play")
            player.interrupted = False
            player.currentSong = None
            player.pendingVoiceChannel = channel
            player.pendingTriggerUserId = ctx.author.id
    else:
        # Defer the connect — startPlayingCurrent will join after the download
        # completes so the bot never sits silent in the channel.
        player.pendingVoiceChannel = channel
        player.pendingTriggerUserId = ctx.author.id

    # Prepare search or URL input
    inputStr = query.strip()
    isSearch = not (inputStr.startswith("http://") or inputStr.startswith("https://") or inputStr.startswith("ytsearch:"))
    if isSearch:
        # ytsearchN para tener candidatos: los primeros hits suelen ser canales
        # (ej. "Indio Solari" devuelve el canal UCzq3uuD... que yt-dlp no
        # puede bajar como video). Despues filtramos a videos validos y, si hay
        # mas de uno, le ofrecemos al usuario que elija cual quiere.
        inputStr = f"ytsearch{_PLAY_CHOICE_COUNT + 2}:{inputStr}"

    # 1. Fetch metadata (ID and Title)
    await safeEdit(ctx, "🔍 Buscando y obteniendo metadatos...")
    try:
        cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        ytDlpArgs = [config.YT_DLP_PATH]
        if os.path.exists(cookiesPath):
            ytDlpArgs += ["--cookies", cookiesPath]
        if config.YT_DLP_POT_BASE_URL:
            ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
        ytDlpArgs += [
            "--flat-playlist",
            "--simulate",
            "--print", "%(id)s",
            "--print", "%(title)s",
            "--print", "%(duration_string)s",
            inputStr,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ytDlpArgs,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            errMsg = stderr.decode('utf-8', errors='replace').strip()
            diag = _diagnoseYtDlpFailure(errMsg, proc.returncode)
            reason = diag.format()
            print(f"[PLAY ERROR] Metadata fetch failed: {reason} ({errMsg[:200]})")
            playLogger.error(f"[METADATA FAIL] Query '{query}' failed with returncode {proc.returncode}. audience: {diag.audience}. summary: {diag.summary}. stderr: {errMsg[:500]}")
            return await safeEdit(ctx, f"❌ Error al buscar el video: {reason}")

        lines = stdout.decode('utf-8', errors='replace').strip().split('\n')
        lines = [l.strip() for l in lines if l.strip()]
        if not lines:
            return await safeEdit(ctx, "❌ No se encontraron resultados.")

        songs = []
        for i in range(0, len(lines) - 2, 3):
            durStr = lines[i+2]
            songs.append({
                "id": lines[i],
                "title": lines[i+1],
                "duration_string": durStr if durStr != "NA" else ""
            })

        if isSearch:
            # YouTube search mezcla videos con canales/playlists; filtramos
            # los que no son videos (id de canal "UC..." o sin duracion).
            songs = [s for s in songs if not s["id"].startswith("UC") and s["duration_string"]]

        if not songs:
            return await safeEdit(ctx, "❌ No se pudieron obtener los metadatos del video.")
    except FileNotFoundError as e:
        diag = _diag("admin",
                     f"yt-dlp no encontrado en {config.YT_DLP_PATH}.",
                     admin_step=f"yt-dlp no encontrado en `{config.YT_DLP_PATH}`. Revisá YT_DLP_PATH en .env.")
        playLogger.error(f"[METADATA FAIL] yt-dlp binary missing at {config.YT_DLP_PATH}: {e}")
        return await safeEdit(ctx, f"❌ Error al buscar el video: {diag.format()}")
    except Exception as e:
        diag = _diagnoseYtDlpFailure(str(e))
        reason = diag.format()
        playLogger.error(f"[METADATA FAIL] Exception during metadata fetch for '{query}': {e} → audience: {diag.audience}. summary: {diag.summary}")
        print(f"[PLAY ERROR] Exception during metadata fetch: {e}")
        return await safeEdit(ctx, f"❌ Error al buscar el video: {reason}")

    # Free-text search with several candidates → let the requester pick which
    # one instead of silently grabbing the first hit (which often was the wrong
    # version). A direct URL/playlist skips this and queues straight away.
    # Exception: if the top hit's title overlaps strongly with the user query
    # (normalized, sin tildes/punctuation), there's a clear winner and we skip
    # the picker — the picker is meant for ambiguous queries, not specific ones.
    if isSearch and len(songs) > 1:
        if _should_autoplay_top(query, songs[0]["title"]):
            playLogger.info(
                "[PLAY AUTOPLAY] query=%r top=%r ratio=%.2f → skipping picker",
                query, songs[0]["title"],
                _query_title_ratio(query, songs[0]["title"]),
            )
            await player.addSongs([songs[0]], ctx)
            return
        # Reorder candidates by fuzzy match against the query so the default
        # pick (candidates[0] when nobody votes) actually matches what was
        # asked, not just YouTube's first hit.
        candidates = sorted(
            songs[:_PLAY_CHOICE_COUNT],
            key=lambda c: _query_title_ratio(query, c.get("title", "")),
            reverse=True,
        )

        async def _resolve(vote: MusicVote, winner: dict) -> None:
            await player.addSongs([winner], ctx)

        vote = open_music_vote(
            bot=ctx.bot, guild_id=ctx.guild.id,
            candidates=candidates, on_resolve=_resolve,
            requester_id=int(getattr(ctx.author, "id", 0) or 0),
        )
        prompt = _format_choice_prompt(candidates)
        msg = None
        try:
            msg = await ctx.interaction.edit_original_response(
                content=prompt, view=None,
            )
        except Exception:
            try:
                msg = await ctx.followup.send(prompt)
            except Exception:
                await safeEdit(ctx, prompt)
        if msg is not None:
            vote.reaction_message_id = int(msg.id)
            vote.reaction_channel_id = int(msg.channel.id)
            for i in range(len(candidates)):
                try:
                    await msg.add_reaction(_num_emoji(i + 1))
                except Exception:
                    pass
        vote.start_timeout()
        return

    # Single result (or a URL/playlist): queue it directly.
    await player.addSongs(songs, ctx)


# ---------- Programmatic entry points (no slash ctx) -----------------------
# These let other modules (notably geminiCommand when the indio decides to
# play music or a sound) drive playback without a Discord interaction.


def _pick_voice_channel(bot, guild_id: int) -> Optional[discord.VoiceChannel]:
    """Pick the most-populated voice channel in the guild, or the channel
    the bot is already connected to. Returns None if no usable channel."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    # Prefer where the bot is already connected.
    if guild.voice_client and getattr(guild.voice_client, "channel", None):
        return guild.voice_client.channel
    candidates = []
    for ch in guild.voice_channels:
        humans = sum(1 for m in ch.members if not m.bot)
        if humans > 0:
            candidates.append((ch, humans))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1], reverse=True)
    return candidates[0][0]


async def _yt_dlp_search(query: str, *, max_results: int = 1) -> list[dict]:
    """Run yt-dlp to resolve the query to song metadata. Returns a list of
    {id, title, duration_string} dicts; empty list on any failure.

    ``max_results`` caps how many *search* hits to return (defaults to 1, the
    legacy single-pick behaviour). It only applies to free-text searches; a
    direct URL/playlist always returns every entry yt-dlp reports so playlists
    keep queueing in full. We fetch a couple extra candidates beyond
    ``max_results`` because the first hits are often channels/playlists that get
    filtered out below.
    """
    inputStr = query.strip()
    isSearch = not (inputStr.startswith("http://") or inputStr.startswith("https://")
                    or inputStr.startswith("ytsearch:"))
    if isSearch:
        # ytsearchN + filtro abajo: los primeros hits suelen ser canales
        # (ej. "Indio Solari" → UCzq3uuD...) que no se pueden bajar, así que
        # pedimos algunos de más para tener candidatos válidos suficientes.
        fetch_n = max(max_results + 2, 3)
        inputStr = f"ytsearch{fetch_n}:{inputStr}"
    cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    args = [config.YT_DLP_PATH]
    if os.path.exists(cookiesPath):
        args += ["--cookies", cookiesPath]
    if config.YT_DLP_POT_BASE_URL:
        args += ["--extractor-args",
                 f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
    args += [
        "--flat-playlist", "--simulate",
        "--print", "%(id)s",
        "--print", "%(title)s",
        "--print", "%(duration_string)s",
        inputStr,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] yt-dlp spawn failed: {e}")
        return []
    if proc.returncode != 0:
        playLogger.warning(f"[PLAY-INDIO] yt-dlp rc={proc.returncode}: "
                           f"{stderr.decode('utf-8', 'replace').strip()[:200]}")
        return []
    lines = [l.strip() for l in stdout.decode("utf-8", "replace").strip().split("\n") if l.strip()]
    songs: list[dict] = []
    for i in range(0, len(lines) - 2, 3):
        dur = lines[i + 2]
        songs.append({
            "id": lines[i],
            "title": lines[i + 1],
            "duration_string": dur if dur != "NA" else "",
        })
    if isSearch:
        # Filtramos canales/playlists para quedarnos con videos reales.
        videos = [s for s in songs if not s["id"].startswith("UC") and s["duration_string"]]
        songs = videos[:max_results]
    return songs


async def playFromIndio(bot, guild_id: int, query: str,
                        voice_channel_id: Optional[int] = None,
                        *, songs: Optional[list[dict]] = None) -> tuple[bool, str]:
    """Queue a YouTube search/URL programmatically — no slash ctx required.

    Used by the indio when someone asks him to play music. Picks a voice
    channel automatically, but the text channel for status + GuildPlayer
    control panel is always ``config.INDIO_PLAY_CHANNEL_ID`` (no fallback);
    if that channel is missing the action fails.

    ``songs`` lets a caller pass an already-resolved list of
    ``{id, title, duration_string}`` dicts (e.g. the candidate the user picked
    from a disambiguation menu) so we skip the yt-dlp search entirely. When it
    is ``None`` we search using ``query`` as before.

    Returns:
        (ok, message): ``ok=True`` if playback started or song queued;
        the message is a short user-facing status.
    """
    if not query or not query.strip():
        return False, "query vacio"

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False, "guild no encontrado"

    voice_channel = None
    if voice_channel_id:
        ch = guild.get_channel(int(voice_channel_id))
        if isinstance(ch, discord.VoiceChannel):
            voice_channel = ch
    if voice_channel is None:
        voice_channel = _pick_voice_channel(bot, guild_id)
    if voice_channel is None:
        return False, "no hay nadie en un canal de voz para reproducir"

    text_channel = guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
    if text_channel is None or not hasattr(text_channel, "send"):
        playLogger.warning(
            "[PLAY-INDIO] INDIO_PLAY_CHANNEL_ID=%s no encontrado en guild %s",
            config.INDIO_PLAY_CHANNEL_ID, guild_id,
        )
        return False, (f"no encuentro el canal de musica configurado "
                       f"(id={config.INDIO_PLAY_CHANNEL_ID})")

    # Voice-connect strategy mirrors playLogic: if already connected reuse/move
    # the vc; otherwise defer the join until the first song finishes downloading
    # (startPlayingCurrent handles the lazy connect). Joining first and sitting
    # silent during yt-dlp would let the idle watchdog kick us out before we
    # play a note.
    vc = guild.voice_client
    deferred = False
    try:
        if vc is None or not vc.is_connected():
            vc = None
            deferred = True
        elif vc.channel.id != voice_channel.id:
            if getattr(vc, "recording", False):
                try:
                    vc.stop_recording()
                except Exception:
                    pass
                setattr(vc, "recording", False)
            set_pending_trigger(voice_channel.id, bot.user.id if bot.user else 0)
            await vc.move_to(voice_channel)
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] voice connect failed: {e}")
        return False, f"no pude conectarme a voz: {e}"

    if songs is None:
        songs = await _yt_dlp_search(query)
    if not songs:
        return False, "no encontre nada en YouTube con esa busqueda"

    player = getGuildPlayer(guild_id, bot)
    player.textChannel = text_channel
    if deferred:
        player.pendingVoiceChannel = voice_channel
        player.pendingTriggerUserId = bot.user.id if bot.user else 0
    else:
        player.vc = vc

    isFirst = (not player.currentSong and len(player.queue) == 0)
    title = songs[0]["title"]
    note = f"🎶 **{title}** {'arrancando' if isFirst else 'a la cola'} (pedido al indio)."
    try:
        await text_channel.send(note)
    except Exception:
        playLogger.exception("[PLAY-INDIO] failed to post note in sick-tunes")

    try:
        await player._enqueueAndMaybeStart(songs, source="indio")
    except Exception as e:
        playLogger.exception("[PLAY-INDIO] enqueue/start failed")
        return False, f"falló el inicio: {e}"

    return True, title


async def playSoundFromIndio(bot, guild_id: int, sound_query: str) -> tuple[bool, str]:
    """Play a local sound clip programmatically. Skips if music is currently
    playing — the indio shouldn't step on the music. ``sound_query`` is a
    fuzzy match against filenames under ``config.CUSTOM_AUDIO_PATH``.

    Returns:
        (ok, message).
    """
    if not sound_query or not sound_query.strip():
        return False, "sound query vacio"

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False, "guild no encontrado"

    # Refuse if music is playing.
    player = guildPlayers.get(guild_id)
    if player is not None and player.currentSong:
        return False, "hay musica sonando, no toco el soundpad"
    vc = guild.voice_client
    if vc and vc.is_playing():
        return False, "vapls ya esta reproduciendo algo, paso"

    # Locate the sound file under CUSTOM_AUDIO_PATH (recursive fuzzy match).
    root = getattr(config, "CUSTOM_AUDIO_PATH", None) or getattr(config, "AUDIO_DIR", None)
    if not root or not os.path.isdir(root):
        return False, "CUSTOM_AUDIO_PATH no configurado"
    needle = sound_query.strip().lower()
    matches: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
                continue
            if needle in f.lower():
                matches.append(os.path.join(dirpath, f))
    if not matches:
        return False, f"no encontre un sonido que matchee '{sound_query}'"
    matches.sort(key=lambda p: len(os.path.basename(p)))
    filepath = matches[0]

    voice_channel = _pick_voice_channel(bot, guild_id)
    if voice_channel is None:
        return False, "no hay nadie en voz para reproducir el sonido"

    try:
        if vc is None or not vc.is_connected():
            vc = await voice_channel.connect(reconnect=True)
        elif vc.channel.id != voice_channel.id:
            await vc.move_to(voice_channel)
    except Exception as e:
        return False, f"no pude conectarme a voz: {e}"

    try:
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.2)
        vc.play(discord.FFmpegOpusAudio(filepath))
    except Exception as e:
        playLogger.exception("[PLAY-INDIO] sound playback failed")
        return False, f"falló la reproduccion: {e}"

    return True, os.path.basename(filepath)

"""Instagram Reels streaming for Discord GoLive.

Extends H264VideoPlayer with:
  - Vertical-orientation letterboxing (1080×1920 → 1920×1080 with black bars)
  - Single-reel URL mode via yt-dlp (no credentials needed)
  - Infinite-scroll feed mode via InstagramReelFeed (yt-dlp flat_playlist)

URL mode (``video_url`` + ``audio_url``) plays one reel.  Feed mode
discovers reel page URLs via yt-dlp ``flat_playlist``, then extracts
each reel with yt-dlp for proper video+audio DASH streams — no
Instagram credentials required.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from streamer import (
    H264VideoPlayer,
    _stream_fps,
    _stream_bitrate,
    _EncoderConfig,
    _ENCODER,
    _SW_ENCODER,
)

from golive_connection import GoLiveConnection, _GoLiveVCProxy, GoLiveAudioSender

from instagram_feed import InstagramReelFeed

log = logging.getLogger(__name__)

# ── Vertical letterbox filter ────────────────────────────────────────────────
# Instagram Reels are 1080×1920 (9:16 portrait).  We output 1920×1080 (16:9
# landscape) with black letterbox bars to fit Discord's GoLive resolution cap.
_VERTICAL_RES = "1920:1080"

_VERTICAL_VF = (
    f"scale={_VERTICAL_RES}:force_original_aspect_ratio=decrease,"
    f"pad={_VERTICAL_RES}:(ow-iw)/2:(oh-ih)/2:black"
)


def _vertical_encoder() -> _EncoderConfig:
    """Return an encoder config that forces libopenh264 with the vertical
    letterbox filter chain.  Hardware encoders are skipped because their
    filter pipeline (vaapi/nvenc) doesn't always support the pad filter
    in the same invocation."""
    br = _stream_bitrate()
    return _EncoderConfig(
        name="libopenh264",
        pre_input=["-threads", "4"],
        post_codec=[
            "-profile:v",
            "constrained_baseline",
            "-level:v",
            "4.2",
            "-b:v",
            br,
            "-maxrate",
            br,
            "-bufsize",
            br,
            "-threads",
            "4",
            "-allow_skip_frames",
            "1",
        ],
        vf=_VERTICAL_VF,
    )


# ── InstagramReelPlayer ───────────────────────────────────────────────────────


class InstagramReelPlayer(H264VideoPlayer):
    """H.264 video player for Instagram Reels.

    Two modes:

    1. **Feed mode** (pass ``feed``): infinite scroll — discovers reel page
       URLs via yt-dlp ``flat_playlist``, then extracts each reel's
       video+audio DASH streams before playing.  No credentials needed.

    2. **URL mode** (pass ``video_url`` + optionally ``audio_url``): plays a
       single reel extracted via yt-dlp, then stops.  No credentials needed.
       ``audio_url`` is required for DASH streams (video/audio separate);
       if ``None`` the player falls back to a single-input pipeline.

    Both modes apply vertical letterboxing (9:16 → 16:9 with black bars).
    The player runs in a daemon thread — the first reel's extraction happens
    inside :meth:`run` so it never blocks the async event loop.
    """

    def __init__(
        self,
        feed: InstagramReelFeed | None = None,
        voice_client=None,
        fps: float = 30.0,
        audio: bool = True,
        video_url: str | None = None,
        audio_url: str | None = None,
        title: str = "Instagram Reel",
    ) -> None:
        self._feed = feed
        self._title = title

        if video_url:
            url: str | tuple[str, str] = (
                (video_url, audio_url) if audio_url else video_url
            )
        else:
            url = ""  # Feed mode: first URL resolved in run()

        super().__init__(
            url=url,
            voice_client=voice_client,
            fps=fps,
            live=False,
            audio=audio,
        )

        self._enc = _vertical_encoder()
        self._frames_emitted = 0

    def run(self) -> None:
        """Main loop: play reels back-to-back until stopped or feed runs dry.

        In feed mode each reel is extracted via yt-dlp synchronously (safe
        because this runs in a daemon thread), which gives us proper
        video+audio DASH URLs — the same pipeline that makes ``/stream``
        with a single reel URL work.
        """
        from ytdlp import _extract_instagram_sync

        try:
            while not self._end.is_set():
                # ── Resolve the next reel ──────────────────────────────────
                if self._feed:
                    page_url = self._feed.next_reel_url()
                    if not page_url:
                        log.info("[INSTAGRAM] No hay más reels — deteniendo")
                        break

                    log.info("[INSTAGRAM] Extrayendo reel: %s", page_url[:80])
                    extracted = _extract_instagram_sync(page_url)
                    if not extracted:
                        log.warning("[INSTAGRAM] No se pudo extraer reel, saltando")
                        if self._end.wait(timeout=1.0):
                            break
                        continue

                    video_url = extracted.get("video_url")
                    audio_url = extracted.get("audio_url")
                    if video_url and audio_url:
                        self._url = (video_url, audio_url)
                    elif video_url:
                        self._url = video_url
                    else:
                        log.warning("[INSTAGRAM] Sin video URL, saltando")
                        continue

                    self._title = extracted.get("title", "Instagram Reel")
                    self._frames_emitted = 0

                if not self._url:
                    log.info("[INSTAGRAM] Sin URL para reproducir — deteniendo")
                    break

                # ── Stream the current reel ────────────────────────────────
                self._seeking = False
                self._stream()

                if self._end.is_set():
                    break

                if not self._feed:
                    log.info("[INSTAGRAM] Single reel ended — deteniendo")
                    break

                if self._end.wait(timeout=1.0):
                    break
        except Exception:
            log.exception("InstagramReelPlayer error")
        finally:
            if self._proc is not None:
                self._kill_proc(self._proc)
            self._cleanup_fifo()
        log.info("InstagramReelPlayer detenido")


# ── InstagramGoLiveStream ─────────────────────────────────────────────────────


class InstagramGoLiveStream:
    """GoLive stream manager for Instagram Reels.

    Mirrors the lifecycle of :class:`GoLiveStream` (from bot.py) but:
      * Uses :class:`InstagramReelPlayer` instead of ``H264VideoPlayer``.
      * Supports two modes: **URL mode** (yt-dlp, no credentials) and
        **feed mode** (infinite scroll via :class:`InstagramReelFeed`).
      * Runs in non-live (VOD) mode.
    """

    def __init__(
        self,
        bot,
        guild_id: int,
        channel_id: int,
        vc,
        feed: InstagramReelFeed | None = None,
        reel_url: dict | None = None,
    ) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.vc = vc
        self.feed = feed
        self.reel_url = reel_url  # {video_url, audio_url, title} from yt-dlp
        self.conn: Optional[GoLiveConnection] = None
        self.video_player: Optional[InstagramReelPlayer] = None
        self.audio_sender: Optional[GoLiveAudioSender] = None
        self.video_ssrc: Optional[int] = None
        self.is_live = False
        self._stopped = False
        self._inactivity_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        log.info("[INSTAGRAM] Estableciendo conexión GoLive...")
        self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
        await self.conn.connect(timeout=30.0)
        self.video_ssrc = self.conn.ssrc + 1
        await self._start_players()
        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def _start_players(self) -> None:
        assert self.conn is not None
        proxy_vc = _GoLiveVCProxy(self.conn)

        if self.reel_url:
            # URL mode — yt-dlp, no credentials
            r = self.reel_url
            self.video_player = InstagramReelPlayer(
                voice_client=proxy_vc,
                fps=_stream_fps(),
                audio=True,
                video_url=r.get("video_url"),
                audio_url=r.get("audio_url"),
                title=r.get("title", "Instagram Reel"),
            )
        else:
            # Feed mode — yt-dlp flat_playlist, no credentials needed
            self.video_player = InstagramReelPlayer(
                feed=self.feed,
                voice_client=proxy_vc,
                fps=_stream_fps(),
                audio=True,
            )

        self.video_player.start()
        log.info("[INSTAGRAM] InstagramReelPlayer iniciado")

        log.info("[INSTAGRAM] Esperando FIFO de audio...")
        try:
            f = await asyncio.wait_for(
                asyncio.to_thread(open, self.video_player.audio_fifo, "rb"),
                timeout=15.0,
            )
        except TimeoutError:
            log.error("[INSTAGRAM] Timeout esperando FIFO de audio")
            raise RuntimeError("Timeout esperando FIFO de audio")

        self.audio_sender = GoLiveAudioSender(
            file_obj=f,
            conn=self.conn,
            is_source_active=self.video_player.is_source_active,
        )
        self.audio_sender.start()
        log.info("[INSTAGRAM] Audio sender iniciado")

    async def _stop_players(self) -> None:
        if self.video_player:
            self.video_player.stop()
        if self.audio_sender:
            self.audio_sender.stop()
        await asyncio.to_thread(self._wait_players)
        self.video_player = None
        self.audio_sender = None

    def _wait_players(self) -> None:
        deadline = time.monotonic() + 5.0
        for p in (self.video_player, self.audio_sender):
            if p and p.is_alive():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    p.join(timeout=remaining)
                if p.is_alive():
                    log.warning("[INSTAGRAM] %s sigue vivo después de 5s", p.name)

    async def _inactivity_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(2)
                if self._stopped:
                    break
                if self.conn and not self.conn.healthy:
                    log.warning("[INSTAGRAM] Conexión GoLive perdida. Reconectando...")
                    ok = await self._restart_connection()
                    if self._stopped:
                        break
                    if not ok:
                        log.error("[INSTAGRAM] Reconexión GoLive fallida. Deteniendo.")
                        break
                    continue
                if self.video_player and not self.video_player.is_alive():
                    log.warning("[INSTAGRAM] El player murió inesperadamente")
                    break
        except asyncio.CancelledError:
            return
        if not self._stopped:
            await self.stop()

    async def _restart_connection(self) -> bool:
        if self._stopped:
            return False
        await self._stop_players()
        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass
        await asyncio.sleep(3)
        if self._stopped:
            return False
        try:
            self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
            await self.conn.connect(timeout=30.0)
            self.video_ssrc = self.conn.ssrc + 1
            await self._start_players()
            log.info("[INSTAGRAM] GoLive reconectado")
            return True
        except Exception as e:
            log.error("[INSTAGRAM] Reconexión GoLive falló: %s", e)
            return False

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._inactivity_task:
            self._inactivity_task.cancel()
            self._inactivity_task = None
        log.info("[INSTAGRAM] Deteniendo stream de Instagram...")
        try:
            await self._stop_players()
        except Exception:
            pass
        await asyncio.sleep(0.05)
        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass
        if self.vc and self.vc.is_connected():
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        log.info("[INSTAGRAM] Stream de Instagram detenido y limpiado")

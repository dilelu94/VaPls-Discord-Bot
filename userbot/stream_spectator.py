"""Stream spectator logic for the Indio userbot.

Manages periodic or on-demand stream inspection sessions using Gemini Vision
and Piper TTS output when the userbot is in a voice channel.
"""

import asyncio
import logging
import random
import time
from typing import Callable, Optional

import geminiCommand
import tts

logger = logging.getLogger("bot.userbot.stream_spectator")

# Default random interval range (in seconds) between automatic comments
MIN_COMMENT_INTERVAL_SEC = 120.0  # 2 minutes
MAX_COMMENT_INTERVAL_SEC = 300.0  # 5 minutes


class StreamSpectatorManager:
    """Manages stream watching sessions per guild for the Indio userbot."""

    def __init__(self, voice_client_getter: Callable[[int], Optional[object]]):
        self._get_vc = voice_client_getter
        # guild_id -> session dict
        self._sessions: dict[int, dict] = {}

    def is_watching(self, guild_id: int) -> bool:
        return guild_id in self._sessions

    def get_session(self, guild_id: int) -> Optional[dict]:
        return self._sessions.get(guild_id)

    async def start_watching(
        self,
        guild_id: int,
        streamer_name: str,
        streamer_id: Optional[int] = None,
        immediate_check: bool = True,
        sample_frame_fn: Optional[Callable[[], Optional[bytes]]] = None,
    ) -> dict:
        """Start or update a stream spectator session for a guild."""
        if guild_id in self._sessions:
            await self.stop_watching(guild_id)

        session = {
            "guild_id": guild_id,
            "streamer_name": streamer_name,
            "streamer_id": streamer_id,
            "start_ts": time.time(),
            "last_comment_ts": 0.0,
            "sample_frame_fn": sample_frame_fn,
            "active": True,
        }
        self._sessions[guild_id] = session

        task = asyncio.create_task(
            self._spectator_loop(guild_id, immediate_check=immediate_check)
        )
        session["task"] = task
        logger.info(
            "Started stream spectator for guild=%s, streamer=%s (id=%s)",
            guild_id,
            streamer_name,
            streamer_id,
        )
        return session

    async def stop_watching(self, guild_id: int) -> bool:
        """Stop watching stream in a guild."""
        session = self._sessions.pop(guild_id, None)
        if session:
            session["active"] = False
            task = session.get("task")
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info("Stopped stream spectator for guild=%s", guild_id)
            return True
        return False

    async def inspect_now(self, guild_id: int) -> Optional[str]:
        """Trigger an immediate stream inspection and commentary for a guild."""
        session = self._sessions.get(guild_id)
        if not session:
            return None

        return await self._run_single_inspection(session)

    async def _run_single_inspection(self, session: dict) -> Optional[str]:
        guild_id = session["guild_id"]
        streamer_name = session["streamer_name"]
        streamer_id = session.get("streamer_id")
        sample_fn = session.get("sample_frame_fn")

        # 1. Obtain image snapshot bytes
        image_bytes: Optional[bytes] = None
        if sample_fn is not None:
            try:
                image_bytes = sample_fn()
            except Exception as e:
                logger.warning("sample_frame_fn failed: %s", e)

        # Fallback dummy 1x1 JPEG if no frame provider available (or during testing/mock)
        if not image_bytes:
            image_bytes = (
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
                b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09"
                b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f"
                b"\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
                b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00"
                b"\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00"
                b"\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00"
                b"\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
            )

        # 2. Call Gemini Vision
        try:
            commentary = await geminiCommand.ask_indio_stream_vision(
                image_bytes=image_bytes,
                streamer_name=streamer_name,
                streamer_id=streamer_id,
                guild_id=guild_id,
            )
        except Exception as e:
            logger.warning("ask_indio_stream_vision exception: %s", e)
            commentary = None

        if not commentary:
            logger.info("Stream inspection for guild=%s returned SKIP/None", guild_id)
            return None

        session["last_comment_ts"] = time.time()
        logger.info(
            "Stream commentary generated for guild=%s, streamer=%s: %s",
            guild_id,
            streamer_name,
            commentary,
        )

        # 3. Speak via Voice Client if connected
        vc = self._get_vc(guild_id)
        if vc is not None and getattr(vc, "is_connected", lambda: False)():
            await self._speak_commentary(vc, commentary)

        return commentary

    async def _speak_commentary(self, vc, text: str) -> None:
        """Synthesize TTS and play in voice channel."""
        import os
        import discord
        try:
            from . import greeting
        except ImportError:
            try:
                import userbot.greeting as greeting
            except ImportError:
                import greeting

        try:
            wav_path = await asyncio.to_thread(tts.generate_tts_wav, text)
            if not wav_path or not os.path.exists(wav_path):
                return

            def _after(_err):
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except Exception:
                    pass

            opts = getattr(greeting, "FFMPEG_NORMALIZE_OPTS", "")
            source = discord.FFmpegOpusAudio(wav_path, options=opts)
            if hasattr(vc, "play"):
                vc.play(source, after=_after)
        except Exception as e:
            logger.warning("Failed to play stream commentary TTS: %s", e)


    async def _spectator_loop(self, guild_id: int, immediate_check: bool = True) -> None:
        """Periodic loop that checks the stream at random intervals."""
        session = self._sessions.get(guild_id)
        if not session:
            return

        if immediate_check:
            await self._run_single_inspection(session)

        while session.get("active", False) and guild_id in self._sessions:
            interval = random.uniform(MIN_COMMENT_INTERVAL_SEC, MAX_COMMENT_INTERVAL_SEC)
            await asyncio.sleep(interval)

            if not session.get("active", False) or guild_id not in self._sessions:
                break

            await self._run_single_inspection(session)

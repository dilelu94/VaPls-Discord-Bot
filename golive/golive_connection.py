"""
golive_connection.py — Discord go-live (screenshare) stream connection.

Protocol flow:
  1. Send op 18 (STREAM_CREATE) on the main gateway to request a go-live stream.
  2. Send op 22 (STREAM_SET_PAUSED, paused=False) to mark the stream as active.
  3. Receive STREAM_CREATE event → rtc_server_id (stream voice server).
  4. Receive STREAM_SERVER_UPDATE event → endpoint + token for the stream server.
  5. Open a separate voice WebSocket to the stream server and run the standard
     voice handshake (IDENTIFY → READY → IP discovery → SELECT_PROTOCOL →
     SESSION_DESCRIPTION).
  6. Send op 12 (VIDEO) on the stream WebSocket to announce video capability.
  7. Stream audio (via GoLiveAudioSender) and video (via H264VideoPlayer) through
     the stream connection's UDP socket — viewers watching the go-live see both.
  8. On teardown, send op 19 (STREAM_DELETE) on the main gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord
import discord.opus as _opus
import nacl.secret
import nacl.utils
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import SocketReader

import davey_compat

log = logging.getLogger(__name__)


def _av_sync_ms() -> int:
    """A/V lip-sync offset in milliseconds (STREAM_AV_SYNC_MS, default 0).

    Audio and video are sent on independent threads with no shared clock, so a
    roughly constant offset can appear. Positive advances audio (discard that
    much backlogged initial audio) to fix "audio behind"; negative delays audio
    start to fix "audio ahead". Dial in by testing. Clamped to +/-5000 ms.
    """
    raw = os.environ.get("STREAM_AV_SYNC_MS", "").strip()
    if raw:
        try:
            return max(-5000, min(5000, int(raw)))
        except ValueError:
            log.warning("STREAM_AV_SYNC_MS=%r not an integer; using 0", raw)
    return 0

# Main gateway opcodes for go-live streaming (not present in discord.py-self)
_OP_STREAM_CREATE = 18
_OP_STREAM_DELETE = 19
_OP_STREAM_SET_PAUSED = 22


# ── GoLiveConnection ──────────────────────────────────────────────────────────


class GoLiveConnection:
    """
    Manages a separate Discord go-live (screenshare) voice connection.

    Acts as a VoiceConnectionState-compatible object so it can be used with
    DiscordVoiceWebSocket.from_connection_state().  The patched identify(),
    select_protocol(), and client_connect() methods from video_compat.py are
    automatically applied because they replace methods on DiscordVoiceWebSocket
    globally — so this connection gets video-capable handshake for free.

    Also exposes the send_packet / mode / secret_key interface that
    H264VideoPlayer expects from voice_client._connection.
    """

    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        channel_id: int,
        vc: discord.VoiceClient,
    ) -> None:
        self._bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self._regular_vc = vc  # regular voice connection (for session_id, user)

        # Filled in by connect()
        self.server_id: int | None = None
        self._stream_key: str | None = None
        self.endpoint: str | None = None
        self.token: str | None = None

        # VoiceConnectionState-compatible voice handshake state
        self.ssrc: int = 0
        self.voice_port: int | None = None
        self.endpoint_ip: str | None = None
        self.ip: str | None = None
        self.port: int | None = None
        self.socket: socket.socket | None = None
        self.ws: DiscordVoiceWebSocket | None = None
        self.mode: str = ""
        self.secret_key: list[int] = []

        self.dave_session = None
        self.dave_protocol_version: int = 0
        self.dave_pending_transitions: dict = {}
        self.dave_downgraded: bool = False

        self._poll_task: asyncio.Task | None = None
        self._socket_reader: SocketReader | None = None

    # ── VoiceConnectionState-compatible properties ────────────────────────────
    # DiscordVoiceWebSocket.identify() (patched by video_compat) reads these.

    @property
    def session_id(self) -> str | None:
        return self._regular_vc.session_id

    @property
    def user(self):
        return self._regular_vc.user

    @property
    def voice_client(self) -> discord.VoiceClient:
        # DiscordVoiceWebSocket.from_connection_state() uses voice_client.loop
        # and voice_client._state.http to open the WebSocket.
        return self._regular_vc

    @property
    def supported_modes(self):
        return type(self._regular_vc).supported_modes

    @property
    def max_dave_protocol_version(self) -> int:
        return davey_compat.DAVE_PROTOCOL_VERSION

    @property
    def can_encrypt(self) -> bool:
        return (
            self.dave_protocol_version != 0
            and self.dave_session is not None
            and self.dave_session.ready
        )

    async def reinit_dave_session(self) -> None:
        if self.dave_protocol_version > 0:
            # channel_id for go-live DAVE group is server_id - 1
            dave_channel_id = self.server_id - 1  # type: ignore[operator]
            if self.dave_session is not None:
                self.dave_session.reinit(
                    self.dave_protocol_version, self.user.id, dave_channel_id
                )
            else:
                self.dave_session = davey_compat.DaveSession(  # type: ignore[assignment]
                    self.dave_protocol_version, self.user.id, dave_channel_id
                )
                # Give libdave access to channel members so MLS proposals from
                # users in the voice channel are recognized and not rejected.
                self.dave_session._voice_state = self  # type: ignore[attr-defined]
            if self.dave_session is not None:
                await self.ws.send_binary(  # type: ignore[union-attr]
                    DiscordVoiceWebSocket.MLS_KEY_PACKAGE,
                    self.dave_session.get_serialized_key_package(),
                )
        elif self.dave_session:
            self.dave_session.reset()
            self.dave_session.set_passthrough_mode(True, 10)

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        await self.ws.send_as_json(  # type: ignore[union-attr]
            {
                "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
                "d": {"transition_id": transition_id},
            }
        )
        await self.reinit_dave_session()

    async def _execute_transition(self, transition_id: int) -> None:
        log.debug("GoLive: executing transition ID %d", transition_id)
        if transition_id not in self.dave_pending_transitions:
            log.warning(
                "GoLive: received execute transition but no pending"
                " transition for ID %d",
                transition_id,
            )
            return

        old_version = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)

        if (
            old_version != self.dave_protocol_version
            and self.dave_protocol_version == 0
        ):
            self.dave_downgraded = True
            log.debug("GoLive: DAVE session downgraded")
        elif transition_id > 0 and self.dave_downgraded:
            self.dave_downgraded = False
            if self.dave_session:
                self.dave_session.set_passthrough_mode(True, 10)
            log.debug("GoLive: DAVE session upgraded")

    # ── Public API ────────────────────────────────────────────────────────────

    async def connect(self, timeout: float = 30.0) -> None:
        """
        Signal go-live to Discord, negotiate the stream voice connection, and
        start the background WebSocket heartbeat task.
        """
        user_id = self._regular_vc.user.id
        stream_key = f"guild:{self.guild_id}:{self.channel_id}:{user_id}"

        main_ws = self._bot.ws

        # Register gateway event futures BEFORE sending op 18 to avoid losing
        # events that arrive before we start listening.
        create_fut = main_ws.wait_for(
            "STREAM_CREATE",
            predicate=lambda d: d.get("stream_key", "") == stream_key,
        )
        server_fut = main_ws.wait_for(
            "STREAM_SERVER_UPDATE",
            predicate=lambda d: d.get("stream_key", "") == stream_key,
        )

        log.info(
            "Sending STREAM_CREATE for guild=%s channel=%s user=%s",
            self.guild_id,
            self.channel_id,
            user_id,
        )
        await main_ws.send_as_json(
            {
                "op": _OP_STREAM_CREATE,
                "d": {
                    "type": "guild",
                    "guild_id": str(self.guild_id),
                    "channel_id": str(self.channel_id),
                    "preferred_region": None,
                },
            }
        )
        await main_ws.send_as_json(
            {
                "op": _OP_STREAM_SET_PAUSED,
                "d": {
                    "stream_key": stream_key,
                    "paused": False,
                },
            }
        )

        # Wait for Discord to respond with stream server credentials
        create_data = await asyncio.wait_for(create_fut, timeout=timeout)
        server_data = await asyncio.wait_for(server_fut, timeout=timeout)

        self.server_id = int(create_data["rtc_server_id"])
        self._stream_key = create_data["stream_key"]
        log.info(
            "STREAM_CREATE received: server_id=%s stream_key=%s",
            self.server_id,
            self._stream_key,
        )

        endpoint = server_data["endpoint"]
        if endpoint.startswith("wss://"):
            endpoint = endpoint[6:]
        self.endpoint = endpoint
        self.token = server_data["token"]
        log.info("STREAM_SERVER_UPDATE received: endpoint=%s", endpoint)

        # Create UDP socket before WebSocket connect — initial_connection() uses
        # it immediately to perform IP discovery.
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self._socket_reader = SocketReader(self, start_paused=False)  # type: ignore[arg-type]
        self._socket_reader.start()

        # Connect WebSocket to stream server and run the voice handshake.
        # from_connection_state() uses the patched identify() from video_compat
        # which adds video:true and streams descriptors to the IDENTIFY payload.
        self.ws = await DiscordVoiceWebSocket.from_connection_state(
            self, resume=False  # type: ignore[arg-type]
        )

        # Poll until READY completes IP discovery (self.ip is set)
        while not self.ip:
            await self.ws.poll_event()  # type: ignore[union-attr]
        log.info("GoLive: IP discovery complete (%s:%s)", self.ip, self.port)

        # Poll until SESSION_DESCRIPTION arrives (ws.secret_key is set)
        while self.ws.secret_key is None:
            await self.ws.poll_event()
        log.info(
            "GoLive: session established (mode=%s, audio_ssrc=%d, video_ssrc=%d)",
            self.mode,
            self.ssrc,
            self.ssrc + 1,
        )

        # Announce video capability using the patched client_connect() from
        # video_compat, which sends the full VIDEO opcode with video/rtx SSRCs
        # and stream resolution/bitrate metadata.
        await self.ws.client_connect()

        # Keep the stream WebSocket heartbeat alive in the background
        loop = asyncio.get_event_loop()
        self._poll_task = loop.create_task(self._poll_ws(), name="golive-ws-poll")

        log.info(
            "GoLive connection ready: audio SSRC %d, video SSRC %d",
            self.ssrc,
            self.ssrc + 1,
        )

    async def _poll_ws(self) -> None:
        """Continuously poll the stream WebSocket to handle heartbeats."""
        try:
            while True:
                await self.ws.poll_event()  # type: ignore[union-attr]
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("GoLive WS poller ended: %s", exc)

    def add_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.register(callback)

    def remove_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.unregister(callback)

    def send_packet(self, packet: bytes) -> None:
        """Send a raw RTP packet to the go-live stream server."""
        try:
            self.socket.sendall(packet)  # type: ignore[union-attr]
        except OSError:
            pass

    async def disconnect(self) -> None:
        """Stop the go-live stream and release all resources."""
        if self._stream_key:
            try:
                await self._bot.ws.send_as_json(
                    {
                        "op": _OP_STREAM_DELETE,
                        "d": {"stream_key": self._stream_key},
                    }
                )
                log.info("Sent STREAM_DELETE for %s", self._stream_key)
            except Exception:
                log.debug("GoLive: could not send STREAM_DELETE", exc_info=True)

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

        if self._socket_reader is not None:
            self._socket_reader.stop()
            self._socket_reader = None

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass

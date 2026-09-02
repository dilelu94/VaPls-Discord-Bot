"""GoLiveWatcherConnection: Manages a Discord stream watcher connection.

Discreetly sends Opcode 20 (STREAM_WATCH) and Opcode 22 (STREAM_SET_PAUSED: false)
to capture video RTP sample bursts from a target user's Go Live stream without
triggering telemetry flags.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import struct
import time
from typing import Optional

import discord
try:
    from discord.gateway import DiscordVoiceWebSocket
except ImportError:
    DiscordVoiceWebSocket = getattr(discord.gateway, "DiscordVoiceWebSocket", None)
try:
    from discord.voice_state import SocketReader
except ImportError:
    SocketReader = None

import davey_compat
from golive.receiver import StreamSnapshotReceiver

log = logging.getLogger(__name__)

_OP_STREAM_WATCH = 20
_OP_STREAM_SET_PAUSED = 22

_rtp_listeners: set = set()


def register_rtp_listener(callback) -> None:
    _rtp_listeners.add(callback)


def unregister_rtp_listener(callback) -> None:
    _rtp_listeners.discard(callback)


def dispatch_rtp_packet(data: bytes) -> None:
    for cb in list(_rtp_listeners):
        try:
            cb(data)
        except Exception:
            pass


async def _send_gateway_json(ws, data: dict) -> bool:
    """Safely sends a JSON opcode dictionary over any Discord Gateway WebSocket type."""
    if ws is None:
        return False
    payload_str = json.dumps(data)

    # Check for send_as_json first
    send_as_json_fn = getattr(ws, "send_as_json", None)
    if send_as_json_fn is not None and callable(send_as_json_fn):
        try:
            res = send_as_json_fn(data)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception as exc:
            log.debug("[WATCHER] send_as_json failed: %s", exc)

    # Check for send method
    send_fn = getattr(ws, "send", None)
    if send_fn is not None and callable(send_fn):
        try:
            res = send_fn(payload_str)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception as exc:
            log.debug("[WATCHER] send failed: %s", exc)

    # Check for send_str method (aiohttp)
    send_str_fn = getattr(ws, "send_str", None)
    if send_str_fn is not None and callable(send_str_fn):
        try:
            res = send_str_fn(payload_str)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception:
            pass

async def _wait_for_gateway_event(target, event_name: str, predicate, timeout: float = 8.0) -> Optional[dict]:
    ws = target if hasattr(target, "wait_for") else getattr(target, "ws", None)
    if ws and hasattr(ws, "wait_for"):
        try:
            fut = ws.wait_for(event_name, predicate)
            data = await asyncio.wait_for(fut, timeout=timeout)
            log.info("[WATCHER-GW] Received gateway event %s: %s", event_name, data)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log.debug("[WATCHER-GW] ws.wait_for(%s) timed out or failed: %s", event_name, exc)
            return None
    return None


class GoLiveWatcherConnection:
    """Manages a discrete stream watcher connection for taking video snapshots."""

    def __init__(
        self,
        bot,
        guild_id: int,
        channel_id: int,
        target_user_id: int,
        vc: discord.VoiceClient,
    ) -> None:
        self._bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.target_user_id = target_user_id
        self._regular_vc = vc

        self.server_id: Optional[int] = None
        self._stream_key: str = f"guild:{guild_id}:{channel_id}:{target_user_id}"

        self.endpoint: Optional[str] = None
        self.token: Optional[str] = None
        self.ssrc: int = getattr(vc, "ssrc", 0)
        self.voice_port: Optional[int] = None
        self.endpoint_ip: Optional[str] = None
        self.ip: Optional[str] = None
        self.port: Optional[int] = None
        self.mode: str = ""
        self.secret_key: list[int] = []

        self.socket: Optional[socket.socket] = None
        self.ws: Optional[DiscordVoiceWebSocket] = None
        self._socket_reader: Optional[SocketReader] = None
        self._poll_task: Optional[asyncio.Task] = None

        self.dave_protocol_version: int = 0
        self.dave_pending_transitions: dict = {}
        self.dave_downgraded: bool = False

        state = getattr(vc, "_connection", None)
        self.dave_session = getattr(state, "dave_session", None) if state else getattr(vc, "dave_session", None)
        self.receiver = StreamSnapshotReceiver()
        self._connected = False

    @property
    def session_id(self) -> Optional[str]:
        return self._regular_vc.session_id

    @property
    def user(self):
        return self._regular_vc.user

    @property
    def voice_client(self) -> discord.VoiceClient:
        return self._regular_vc

    @property
    def supported_modes(self):
        return type(self._regular_vc).supported_modes

    @property
    def max_dave_protocol_version(self) -> int:
        try:
            import davey_compat
            return davey_compat.DAVE_PROTOCOL_VERSION
        except Exception:
            return 1

    @property
    def can_encrypt(self) -> bool:
        return (
            self.dave_protocol_version != 0
            and self.dave_session is not None
            and getattr(self.dave_session, "ready", False)
        )

    async def reinit_dave_session(self, force: bool = False) -> None:
        if self.dave_protocol_version > 0 and self.server_id:
            dave_channel_id = self.server_id - 1
            if self.dave_session is not None:
                self.dave_session.reinit(
                    self.dave_protocol_version, self.user.id, dave_channel_id
                )
            else:
                try:
                    import davey_compat
                    self.dave_session = davey_compat.DaveSession(
                        self.dave_protocol_version, self.user.id, dave_channel_id
                    )
                    self.dave_session._voice_state = self
                except Exception as exc:
                    log.warning("[WATCHER] DaveSession init error: %s", exc)
            if self.dave_session is not None and self.ws:
                try:
                    from discord.gateway import DiscordVoiceWebSocket
                    await self.ws.send_binary(
                        DiscordVoiceWebSocket.MLS_KEY_PACKAGE,
                        self.dave_session.get_serialized_key_package(),
                    )
                except Exception as exc:
                    log.warning("[WATCHER] Could not send MLS_KEY_PACKAGE: %s", exc)
        elif self.dave_session:
            self.dave_session.reset()
            self.dave_session.set_passthrough_mode(True, 10)

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        if self.ws:
            await self.ws.send_as_json(
                {
                    "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
                    "d": {"transition_id": transition_id},
                }
            )
            await self.reinit_dave_session(force=True)

    async def _execute_transition(self, transition_id: int) -> None:
        if transition_id not in self.dave_pending_transitions:
            return
        old_version = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)
        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            self.dave_downgraded = True
        elif transition_id > 0 and self.dave_downgraded:
            self.dave_downgraded = False
            if self.dave_session:
                self.dave_session.set_passthrough_mode(True, 10)

    def add_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.register(callback)

    def remove_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.unregister(callback)

    def send_packet(self, packet: bytes) -> None:
        if self.socket:
            try:
                self.socket.sendall(packet)
            except OSError:
                pass

    async def connect(self, timeout: float = 20.0) -> bool:
        """Discreetly sends STREAM_WATCH and STREAM_SET_PAUSED to trigger stream packet flow."""
        main_ws = self._bot.ws
        if not main_ws:
            log.warning("[WATCHER] bot.ws is None — cannot connect stream watcher")
            return False

        stream_key = self._stream_key
        server_fut = asyncio.create_task(
            _wait_for_gateway_event(
                self._bot,
                "STREAM_SERVER_UPDATE",
                lambda d: str(self.target_user_id) in str(d.get("stream_key", "")),
                timeout=5.0,
            )
        )
        create_fut = asyncio.create_task(
            _wait_for_gateway_event(
                self._bot,
                "STREAM_CREATE",
                lambda d: str(self.target_user_id) in str(d.get("stream_key", "")),
                timeout=5.0,
            )
        )

        log.info(
            "[WATCHER] Requesting STREAM_WATCH for stream_key=%s",
            self._stream_key,
        )

        try:
            # 1. Send Opcode 20 (STREAM_WATCH)
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_WATCH,
                    "d": {
                        "stream_key": self._stream_key,
                        "video_codec": "H264",
                    },
                },
            )

            # 2. Send Opcode 22 (STREAM_SET_PAUSED: false) on Gateway WS
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_SET_PAUSED,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

            # 3. Send Opcode 22 (STREAM_SET_PAUSED: false) on Voice WS if available
            voice_ws = getattr(self._regular_vc, "ws", None) or getattr(getattr(self._regular_vc, "_connection", None), "ws", None)
            if voice_ws:
                await _send_gateway_json(
                    voice_ws,
                    {
                        "op": 22,
                        "d": {
                            "stream_key": self._stream_key,
                            "paused": False,
                        },
                    },
                )

            # 4. Connect dedicated stream WebSocket & UDP socket if STREAM_SERVER_UPDATE is received
            try:
                cached_server = getattr(self._bot, "_active_stream_updates", {}).get(f"{self._stream_key}:server") or getattr(self._bot, "_active_stream_updates", {}).get(self._stream_key)
                if cached_server:
                    log.info("[WATCHER] Using cached STREAM_SERVER_UPDATE for %s: endpoint=%s", self._stream_key, cached_server.get("endpoint"))
                    server_data = cached_server
                else:
                    server_data = await server_fut

                if not server_data:
                    raise asyncio.TimeoutError("STREAM_SERVER_UPDATE timed out")
                endpoint = server_data.get("endpoint", "")
                if endpoint.startswith("wss://"):
                    endpoint = endpoint[6:]
                self.endpoint = endpoint
                self.token = server_data.get("token")
                log.info("[WATCHER] Received STREAM_SERVER_UPDATE: endpoint=%s", self.endpoint)

                cached_create = getattr(self._bot, "_active_stream_updates", {}).get(f"{self._stream_key}:create")
                if cached_create:
                    create_data = cached_create
                else:
                    create_data = await create_fut
                if create_data and "rtc_server_id" in create_data:
                    self.server_id = int(create_data["rtc_server_id"])
                else:
                    self.server_id = self.guild_id

                if self.endpoint and DiscordVoiceWebSocket is not None:
                    self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.socket.setblocking(False)
                    self._socket_reader = SocketReader(self, start_paused=False)
                    self._socket_reader.register(self._on_udp_packet)
                    self._socket_reader.start()

                    self.ws = await DiscordVoiceWebSocket.from_connection_state(
                        self, resume=False
                    )
                    while not self.ip:
                        await self.ws.poll_event()

                    if self.endpoint_ip and self.voice_port:
                        self.socket.connect((self.endpoint_ip, self.voice_port))

                    while self.ws.secret_key is None:
                        await self.ws.poll_event()

                    self.mode = getattr(self.ws, "mode", "") or "aead_xchacha20_poly1305_rtpsize"
                    self.secret_key = getattr(self.ws, "secret_key", None) or []

                    await self.ws.client_connect()
                    loop = asyncio.get_event_loop()
                    self._poll_task = loop.create_task(self._poll_ws(), name="watcher-ws-poll")
                    log.info("[WATCHER] Dedicated stream socket connection established to %s (mode=%s)", self.endpoint, self.mode)
            except Exception as exc:
                log.info("[WATCHER] Dedicated stream connection note: %s (using main voice listener fallback)", exc)

            self._connected = True
            log.info("[WATCHER] STREAM_WATCH signal sent (status=True)")
            return True
        except Exception as exc:
            log.error("[WATCHER] Failed to send STREAM_WATCH: %s", exc, exc_info=True)
            return False

    async def _poll_ws(self) -> None:
        """Continuously poll the stream WebSocket to handle heartbeats."""
        try:
            while self.ws:
                await self.ws.poll_event()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("[WATCHER] Stream WS poller ended: %s", exc)

    def _on_udp_packet(self, data: bytes) -> None:
        """UDP Socket callback for incoming RTP packets."""
        if not data or len(data) < 12:
            return

        if not hasattr(self, "_total_udp_count"):
            self._total_udp_count = 0
        self._total_udp_count += 1
        if self._total_udp_count <= 25:
            pt = data[1] & 0x7F if len(data) > 1 else -1
            ssrc = struct.unpack("!I", data[8:12])[0] if len(data) >= 12 else -1
            log.info("[WATCHSTREAM-DEBUG] Packet #%d: len=%d PT=%d SSRC=%d hex=%s", self._total_udp_count, len(data), pt, ssrc, data[:20].hex())

        # Check payload type — skip Opus audio (120, 111, 121, 77) and RTCP (72-76, 200-206)
        raw_pt = data[1]
        if 200 <= raw_pt <= 206 or 72 <= raw_pt <= 76:
            return

        pt = raw_pt & 0x7F
        if pt in (120, 111, 121, 77):
            return

        # Extract SSRC from 12-byte RTP header (bytes 8..11)
        ssrc = struct.unpack("!I", data[8:12])[0]
        self.video_ssrc = ssrc
        self.receiver.ssrc = ssrc

        if not hasattr(self, "_packet_count"):
            self._packet_count = 0
        self._packet_count += 1
        if self._packet_count == 1 or self._packet_count % 100 == 0:
            log.info(
                "[WATCHSTREAM] Video RTP packet #%d (PT=%d, SSRC=%d, len=%d, hex_hdr=%s)",
                self._packet_count, pt, ssrc, len(data), data[:16].hex()
            )

        dave_sess = getattr(self, "dave_session", None) or getattr(self._regular_vc, "dave_session", None)
        self.receiver.process_rtp_packet(
            rtp_data=data,
            dave_session=dave_sess,
            ssrc=ssrc,
            user_id=self.target_user_id,
        )

    async def capture_snapshot(
        self,
        duration_sec: float = 6.0,
        filename: str = "latest_snapshot.jpg",
        max_wait_sec: float = 15.0,
    ) -> Optional[str]:
        """Captures a burst of RTP video packets for duration_sec seconds after waiting for stream handshake."""
        log.info(
            "[WATCHSTREAM] Initiating stream snapshot capture for target_user=%s (stream_key=%s)",
            self.target_user_id,
            self._stream_key,
        )
        # Bind listener to VoiceRecvClient reader thread (_reader._socket_listeners)
        register_rtp_listener(self._on_udp_packet)
        vc_conn = getattr(self._regular_vc, "_connection", None) or self._regular_vc
        reader = (
            getattr(self._regular_vc, "_reader", None)
            or getattr(vc_conn, "_reader", None)
            or getattr(vc_conn, "_socket_reader", None)
        )

        if reader is not None:
            if hasattr(reader, "add_socket_listener") and callable(reader.add_socket_listener):
                reader.add_socket_listener(self._on_udp_packet)
            elif hasattr(reader, "_socket_listeners") and isinstance(reader._socket_listeners, list):
                if self._on_udp_packet not in reader._socket_listeners:
                    reader._socket_listeners.append(self._on_udp_packet)
            elif hasattr(reader, "register") and callable(reader.register):
                reader.register(self._on_udp_packet)
        elif hasattr(vc_conn, "add_socket_listener"):
            vc_conn.add_socket_listener(self._on_udp_packet)

        # Always re-announce stream watch & unpause to ensure media server routing
        main_ws = self._bot.ws
        if main_ws:
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_WATCH,
                    "d": {
                        "stream_key": self._stream_key,
                        "video_codec": "H264",
                    },
                },
            )
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_SET_PAUSED,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

        voice_ws = getattr(self._regular_vc, "ws", None) or getattr(getattr(self._regular_vc, "_connection", None), "ws", None)
        if voice_ws:
            await _send_gateway_json(
                voice_ws,
                {
                    "op": 22,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

        if not self._connected:
            ok = await self.connect()
            if not ok:
                log.warning("[WATCHSTREAM] connect() failed — aborting capture_snapshot")
                unregister_rtp_listener(self._on_udp_packet)
                return None

        # Enable DAVE passthrough mode only if DAVE is not active/ready
        for ds in (self.dave_session, getattr(self._regular_vc, "dave_session", None)):
            if ds is not None and hasattr(ds, "set_passthrough_mode"):
                try:
                    if getattr(ds, "ready", False):
                        ds.set_passthrough_mode(False)
                    else:
                        ds.set_passthrough_mode(True, 10)
                except Exception as e:
                    log.debug("[WATCHSTREAM] set_passthrough_mode note: %s", e)

        self.receiver.start_capture()
        log.info("[WATCHSTREAM] Waiting for initial stream packets (up to %.1fs)...", max_wait_sec)

        # Wait until video packets start arriving from Discord Gateway (handshake sync)
        start_wait = time.monotonic()
        video_started = False
        last_pli_time = 0.0

        sock = getattr(self, "socket", None) or getattr(vc_conn, "socket", None)

        while time.monotonic() - start_wait < max_wait_sec:
            now = time.monotonic()
            if now - last_pli_time >= 1.0:
                last_pli_time = now
                try:
                    target_ssrc = getattr(self, "video_ssrc", None) or getattr(self.receiver, "ssrc", None) or 1
                    pli_pkt = struct.pack("!BBHII", 0x81, 206, 2, 1, target_ssrc)
                    if sock and hasattr(sock, "sendall"):
                        sock.sendall(pli_pkt)
                        log.info("[WATCHSTREAM] Sent RTCP PLI keyframe request for SSRC %s", target_ssrc)
                except Exception as e:
                    log.debug("[WATCHSTREAM] RTCP PLI request note: %s", e)

            if (
                len(self.receiver._raw_nal_buffer) >= 15000
                and self.receiver._sps_nal
                and self.receiver._pps_nal
            ):
                video_started = True
                log.info(
                    "[WATCHSTREAM] Stream video packets detected! Initial buffer: %d bytes. Collecting sample burst for %.1fs...",
                    len(self.receiver._raw_nal_buffer),
                    duration_sec,
                )
                break
            await asyncio.sleep(0.3)

        if not video_started and len(self.receiver._raw_nal_buffer) > 0:
            log.info(
                "[WATCHSTREAM] Proceeding with available buffer (%d bytes) after wait...",
                len(self.receiver._raw_nal_buffer),
            )

        # Collect sample burst for duration_sec
        await asyncio.sleep(duration_sec)
        self.receiver.stop_capture()

        # Unregister listener
        unregister_rtp_listener(self._on_udp_packet)
        if hasattr(vc_conn, "remove_socket_listener"):
            vc_conn.remove_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "unregister"):
            socket_reader.unregister(self._on_udp_packet)

        log.info(
            "[WATCHSTREAM] Sample burst finished. Collected %d bytes of raw NAL data. Extracting snapshot...",
            len(self.receiver._raw_nal_buffer),
        )

        # Extract snapshot JPEG: prioritize active READY DAVE session from voice client
        vc_ds = getattr(self._regular_vc, "dave_session", None)
        if vc_ds is not None and getattr(vc_ds, "ready", False):
            ds = vc_ds
        elif self.dave_session and getattr(self.dave_session, "ready", False):
            ds = self.dave_session
        else:
            ds = vc_ds or self.dave_session

        ssrc = getattr(self, "video_ssrc", None) or getattr(self.receiver, "ssrc", None) or getattr(self, "_target_ssrc", None)
        snapshot_path = self.receiver.extract_snapshot(
            filename=filename,
            dave_session=ds,
            ssrc=ssrc,
            user_id=self.target_user_id,
        )
        log.info("[WATCHSTREAM] Snapshot extraction result: %s", snapshot_path)
        return snapshot_path

    async def record_and_decode_stream(
        self,
        duration_sec: float = 10.0,
        h264_filename: str = "recorded_stream.h264",
        mp4_filename: str = "recorded_stream.mp4",
        max_wait_sec: float = 12.0,
    ) -> Optional[str]:
        """Captures a 10-second burst of RTP video packets and converts them into an MP4 video file."""
        log.info(
            "[WATCHSTREAM] Initiating 10s video recording for target_user=%s (stream_key=%s)",
            self.target_user_id,
            self._stream_key,
        )
        # Bind listener to VoiceRecvClient reader thread (_reader._socket_listeners)
        register_rtp_listener(self._on_udp_packet)
        vc_conn = getattr(self._regular_vc, "_connection", None) or self._regular_vc
        reader = (
            getattr(self._regular_vc, "_reader", None)
            or getattr(vc_conn, "_reader", None)
            or getattr(vc_conn, "_socket_reader", None)
        )

        if reader is not None:
            if hasattr(reader, "add_socket_listener") and callable(reader.add_socket_listener):
                reader.add_socket_listener(self._on_udp_packet)
            elif hasattr(reader, "_socket_listeners") and isinstance(reader._socket_listeners, list):
                if self._on_udp_packet not in reader._socket_listeners:
                    reader._socket_listeners.append(self._on_udp_packet)
            elif hasattr(reader, "register") and callable(reader.register):
                reader.register(self._on_udp_packet)
        elif hasattr(vc_conn, "add_socket_listener"):
            vc_conn.add_socket_listener(self._on_udp_packet)

        if not self._connected:
            ok = await self.connect()
            if not ok:
                log.warning("[WATCHSTREAM] connect() failed — aborting record_and_decode_stream")
                unregister_rtp_listener(self._on_udp_packet)
                return None

        self.receiver.start_capture()
        log.info("[WATCHSTREAM] Waiting for initial stream packets (up to %.1fs)...", max_wait_sec)

        start_wait = time.monotonic()
        video_started = False
        while time.monotonic() - start_wait < max_wait_sec:
            if (
                self.receiver._seen_keyframe
                and self.receiver._sps_nal
                and self.receiver._pps_nal
                and len(self.receiver._raw_nal_buffer) >= 15000
            ):
                video_started = True
                log.info(
                    "[WATCHSTREAM] Stream video packets detected! Initial buffer: %d bytes. Recording sample for %.1fs...",
                    len(self.receiver._raw_nal_buffer),
                    duration_sec,
                )
                break
            await asyncio.sleep(0.3)

        if not video_started:
            if self.receiver._seen_keyframe and len(self.receiver._raw_nal_buffer) > 0:
                log.info(
                    "[WATCHSTREAM] Proceeding with keyframe buffer (%d bytes) after wait...",
                    len(self.receiver._raw_nal_buffer),
                )
            else:
                log.warning("[WATCHSTREAM] Keyframe (SPS+PPS+IDR) was not received within %.1fs timeout", max_wait_sec)

        await asyncio.sleep(duration_sec)
        self.receiver.stop_capture()

        unregister_rtp_listener(self._on_udp_packet)
        if hasattr(vc_conn, "remove_socket_listener"):
            vc_conn.remove_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "unregister"):
            socket_reader.unregister(self._on_udp_packet)

        log.info(
            "[WATCHSTREAM] Recording burst finished. Collected %d bytes of raw NAL data. Decoding to MP4...",
            len(self.receiver._raw_nal_buffer),
        )

        mp4_path = self.receiver.convert_sample_to_mp4(h264_filename=h264_filename, mp4_filename=mp4_filename)
        log.info("[WATCHSTREAM] MP4 video recording result: %s", mp4_path)
        return mp4_path

    async def disconnect(self) -> None:
        """Sends STREAM_SET_PAUSED: true to stop stream packet flow cleanly."""
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

        if not self._connected:
            return

        main_ws = self._bot.ws
        if main_ws:
            try:
                await _send_gateway_json(
                    main_ws,
                    {
                        "op": _OP_STREAM_SET_PAUSED,
                        "d": {
                            "stream_key": self._stream_key,
                            "paused": True,
                        },
                    },
                )
                log.info("[WATCHER] Sent STREAM_SET_PAUSED: true for %s", self._stream_key)
            except Exception as exc:
                log.warning("[WATCHER] Error disconnecting watcher: %s", exc)

        self._connected = False

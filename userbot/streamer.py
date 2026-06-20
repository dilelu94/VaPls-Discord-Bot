"""Discord Go Live IPTV streaming via the userbot.

Architecture:
  FFmpeg (M3U8 → raw H.264) → AnnexB parser → RTP packetizer → Discord UDP socket

No files touch disk — FFmpeg transcodes HLS to H.264 in memory.
"""

import asyncio
import logging
import random
import struct
from asyncio.subprocess import DEVNULL, PIPE, Process
from typing import Optional

logger = logging.getLogger("userbot.streamer")

# --- RTP constants ---
PAYLOAD_TYPE_H264 = 101
RTP_HEADER_SIZE = 12
DISCORD_MTU = 1200
NAL_TYPE_FU_A = 28
FU_START = 0x80
FU_END = 0x40
TS_PER_FRAME = 3000  # 90kHz clock / 30fps

FFMPEG_VIDEO_ARGS = [
    "-re",
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-analyzeduration",
    "500000",
    "-probesize",
    "500000",
    "-c:v",
    "libopenh264",
    "-b:v",
    "2500k",
    "-maxrate",
    "2500k",
    "-bufsize",
    "5000k",
    "-r",
    "30",
    "-g",
    "60",
    "-f",
    "h264",
    "pipe:1",
]


def _rand_ssrc() -> int:
    return random.randint(1, 0xFFFFFFFE)


def _build_rtp_header(ssrc: int, seq: int, ts: int, marker: bool = False) -> bytes:
    first = 0x80  # V=2, P=0, X=0, CC=0
    second = PAYLOAD_TYPE_H264 & 0x7F
    if marker:
        second |= 0x80
    return struct.pack(
        ">BBHII", first, second, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF
    )


def _packetize_nal(nal: bytes, ssrc: int, seq: int, ts: int) -> list[tuple[bytes, int]]:
    """Packetize one H.264 NAL unit into RTP packets (FU-A if needed)."""
    if not nal:
        return []
    nh = nal[0]
    nt = nh & 0x1F

    if len(nal) <= DISCORD_MTU - RTP_HEADER_SIZE:
        h = _build_rtp_header(ssrc, seq, ts, marker=True)
        return [(h + nal, (seq + 1) & 0xFFFF)]

    fi = (nh & 0xE0) | NAL_TYPE_FU_A
    payload = nal[1:]
    max_frag = DISCORD_MTU - RTP_HEADER_SIZE - 2
    offset = 0
    result = []
    while offset < len(payload):
        end = min(offset + max_frag, len(payload))
        frag = payload[offset:end]
        is_first = offset == 0
        is_last = end >= len(payload)
        fh = nt
        if is_first:
            fh |= FU_START
        if is_last:
            fh |= FU_END
        h = _build_rtp_header(ssrc, seq, ts, marker=is_last)
        pkt = h + bytes([fi, fh]) + frag
        result.append((pkt, (seq + 1) & 0xFFFF))
        offset = end
        seq = (seq + 1) & 0xFFFF
    return result


def _parse_annexb(data: bytes) -> list[bytes]:
    """Split AnnexB byte stream into NAL units (stripping start codes)."""
    nals = []
    start = 0
    i = 0
    while i < len(data):
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            if i > start:
                nals.append(data[start:i])
            start = i + 4
            i = start
        elif i + 3 <= len(data) and data[i : i + 3] == b"\x00\x00\x01":
            if i > start:
                nals.append(data[start:i])
            start = i + 3
            i = start
        else:
            i += 1
    if start < len(data):
        nals.append(data[start:])
    return nals


def _is_slice_nal(nal: bytes) -> bool:
    if not nal:
        return False
    t = nal[0] & 0x1F
    return t in (1, 5)  # non-IDR slice, IDR slice


class VideoStream:
    """Manages one video stream: FFmpeg subprocess + Discord RTP output."""

    def __init__(
        self,
        url: str,
        guild_id: int,
        vc,
        ws,
        sock,
        endpoint_ip: str,
        endpoint_port: int,
        audio_ssrc: int,
    ):
        self.url = url
        self.guild_id = guild_id
        self.vc = vc
        self.ws = ws
        self.sock = sock
        self.endpoint_ip = endpoint_ip
        self.endpoint_port = endpoint_port
        self.audio_ssrc = audio_ssrc
        self.video_ssrc = _rand_ssrc()
        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)

        self._proc: Optional[Process] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._frame_nals: list[bytes] = []

    async def start(self) -> None:
        """Launch FFmpeg, announce video SSRC, begin send loop."""
        await self._announce_video_ssrc(active=True)
        cmd = ["ffmpeg", "-i", self.url] + FFMPEG_VIDEO_ARGS
        logger.info(
            "[STREAM] guild=%s ffmpeg: %s", self.guild_id, " ".join(cmd[:6]) + " ..."
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=DEVNULL,
        )
        if self._proc.stdout is None:
            raise RuntimeError("ffmpeg stdout closed")
        self._task = asyncio.create_task(self._send_loop())
        logger.info(
            "[STREAM] guild=%s started (video_ssrc=%d)", self.guild_id, self.video_ssrc
        )

    async def _announce_video_ssrc(self, active: bool) -> None:
        try:
            await self.ws.send_as_json(
                {
                    "op": 12,
                    "d": {
                        "audio_ssrc": self.audio_ssrc,
                        "video_ssrc": self.video_ssrc,
                        "rtx_ssrc": 0,
                        "stream_id": "",
                        "quality": 1,
                        "type": 1 if active else 0,
                    },
                }
            )
            logger.debug("[STREAM] ws op=12 ssrc=%d active=%s", self.video_ssrc, active)
        except Exception as e:
            logger.warning("[STREAM] ws op=12 failed: %s", e)

    async def _send_loop(self) -> None:
        """Read raw H.264 from FFmpeg stdout and send RTP packets."""
        assert self._proc is not None
        stdout = self._proc.stdout
        buf = b""

        while not self._stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(stdout.read(65536), timeout=2.0)
            except asyncio.TimeoutError:
                if self._proc.returncode is not None:
                    logger.warning(
                        "[STREAM] ffmpeg exited early (rc=%d)", self._proc.returncode
                    )
                    break
                self._flush_frame()
                continue
            if not chunk:
                break
            buf += chunk
            nals = _parse_annexb(buf)
            if not nals:
                continue
            consumed = len(buf)
            # Reconstruct the raw buffer without the last partial NAL
            if nals:
                last = nals[-1]
                consumed = len(buf) - len(last)
                idx = buf.rfind(last)
                if idx >= 0:
                    consumed = idx
                buf = buf[consumed:]
            else:
                buf = b""
            for nal in nals:
                self._feed_nal(nal)
        self._flush_frame()
        logger.info("[STREAM] guild=%s send loop ended", self.guild_id)

    def _feed_nal(self, nal: bytes) -> None:
        """Queue a NAL unit. Flushes previous frame when a new slice is seen."""
        if _is_slice_nal(nal) and self._frame_nals:
            self._flush_frame()
        self._frame_nals.append(nal)

    def _flush_frame(self) -> None:
        """Send all buffered NALs as one frame, then advance timestamp."""
        if not self._frame_nals:
            return
        for nal in self._frame_nals:
            pkts = _packetize_nal(nal, self.video_ssrc, self.seq, self.ts)
            for pkt, new_seq in pkts:
                try:
                    self.sock.sendto(pkt, (self.endpoint_ip, self.endpoint_port))
                except (BlockingIOError, OSError) as e:
                    logger.debug("[STREAM] sendto dropped: %s", e)
                    break
                self.seq = new_seq
        self.ts = (self.ts + TS_PER_FRAME) & 0xFFFFFFFF
        self._frame_nals = []

    async def stop(self) -> None:
        """Kill FFmpeg, announce stop on WS, clean up."""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._proc:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
        await self._announce_video_ssrc(active=False)
        logger.info("[STREAM] guild=%s stopped", self.guild_id)

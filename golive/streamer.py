"""GoLive video streamer: FFmpeg → H.264 → RTP → Discord voice socket.

Self-contained — no imports from userbot/. Handles SPS/VUI rewriting
(Discord requires bitstream_restriction_flag=1, max_num_reorder_frames=0)
and RTP encryption (XSalsa20-Poly1305 / XChaCha20-Poly1305) via PyNaCl.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import struct
import subprocess

from typing import Optional

import nacl.secret
import nacl.utils

log = logging.getLogger("golive.streamer")

# --- RTP constants ---
_PT_H264 = 101
_RTP_HEADER_SIZE = 12
_MTU = 1200
_NAL_FU_A = 28
_FU_START = 0x80
_FU_END = 0x40
_TS_PER_FRAME = 3000  # 90 kHz clock / 30 fps

# Nonce base for video — high half of 32-bit space to avoid audio overlap
_VIDEO_NONCE_BASE = 0x8000_0000

_H264_ENCODERS = ["h264_nvenc", "h264_vaapi", "libx264"]


def _detect_encoder() -> str:
    for enc in _H264_ENCODERS:
        try:
            # Test if encoder works with a 1-frame null input
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=1280x720:d=1",
                    "-c:v",
                    enc,
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
            log.info("golive: encoder probe OK → %s", enc)
            return enc
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.info("golive: encoder %s not usable (%s), trying next", enc, e)
            continue
    log.info("golive: falling back to libx264")
    return "libx264"


_ENCODER = _detect_encoder()


def _ffmpeg_args(url: str) -> list[str]:
    base = [
        "ffmpeg",
        "-re",
        "-fflags",
        "nobuffer",
        "-analyzeduration",
        "500000",
        "-probesize",
        "500000",
        "-i",
        url,
        "-flags",
        "low_delay",
        "-c:v",
        _ENCODER,
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
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
    if _ENCODER == "h264_nvenc":
        base[base.index("-c:v") + 1] = "h264_nvenc"
        # Remove libx264 specific preset/tune before adding nvenc ones
        preset_idx = base.index("-preset")
        del base[preset_idx:preset_idx+4]
        base += [
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-profile:v",
            "high",
            "-level:v",
            "4.2",
        ]
    elif _ENCODER == "h264_vaapi":
        base[base.index("-c:v") + 1] = "h264_vaapi"
        # Remove libx264 specific preset/tune
        preset_idx = base.index("-preset")
        del base[preset_idx:preset_idx+4]
        base += [
            "-vaapi_device",
            "/dev/dri/renderD128",
            "-vf",
            "format=nv12,hwupload,scale_vaapi=1280:720",
            "-rc_mode",
            "CBR",
        ]
    return base


def _build_rtp_header(ssrc: int, seq: int, ts: int, marker: bool = False) -> bytes:
    first = 0x80
    second = _PT_H264 & 0x7F
    if marker:
        second |= 0x80
    return struct.pack(
        ">BBHII", first, second, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF
    )


def _packetize_nal(nal: bytes, ssrc: int, seq: int, ts: int, is_last_nal: bool) -> list[tuple[bytes, int]]:
    """Packetize one H.264 NAL unit into RTP packets (FU-A if needed)."""
    if not nal:
        return []
    nh = nal[0]
    nt = nh & 0x1F

    if len(nal) <= _MTU - _RTP_HEADER_SIZE:
        h = _build_rtp_header(ssrc, seq, ts, marker=is_last_nal)
        return [(h + nal, (seq + 1) & 0xFFFF)]

    fi = (nh & 0xE0) | _NAL_FU_A
    payload = nal[1:]
    max_frag = _MTU - _RTP_HEADER_SIZE - 2
    offset = 0
    result = []
    while offset < len(payload):
        end = min(offset + max_frag, len(payload))
        frag = payload[offset:end]
        is_first = offset == 0
        is_last_frag = end >= len(payload)
        fh = nt
        if is_first:
            fh |= _FU_START
        if is_last_frag:
            fh |= _FU_END
        h = _build_rtp_header(ssrc, seq, ts, marker=(is_last_frag and is_last_nal))
        pkt = h + bytes([fi, fh]) + frag
        result.append((pkt, (seq + 1) & 0xFFFF))
        offset = end
        seq = (seq + 1) & 0xFFFF
    return result


def _parse_annexb(data: bytes) -> list[bytes]:
    """Split Annex B byte stream into NAL units (stripping start codes)."""
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
    return t in (1, 5)


# --- SPS/VUI rewriting --------------------------------------------------------
# Discord requires bitstream_restriction_flag=1 and max_num_reorder_frames=0
# in every SPS or it rejects the stream with Error 2015.

_HIGH_PROFILES = frozenset({100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 144})


def _ep_remove(data: bytes) -> bytes:
    """Strip emulation prevention bytes (0x00 0x00 0x03 -> 0x00 0x00)."""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out.append(0)
            out.append(0)
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _ep_add(data: bytes) -> bytes:
    """Re-insert emulation prevention bytes."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _BR:
    """H.264 RBSP bit reader."""

    __slots__ = ("_d", "_p")

    def __init__(self, data: bytes) -> None:
        self._d = data
        self._p = 0

    def remaining(self) -> int:
        return len(self._d) * 8 - self._p

    def u(self, n: int) -> int:
        val = 0
        for _ in range(n):
            bi = self._p >> 3
            if bi >= len(self._d):
                raise IndexError(f"SPS read past end at bit {self._p}")
            val = (val << 1) | ((self._d[bi] >> (7 - (self._p & 7))) & 1)
            self._p += 1
        return val

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("Exp-Golomb: too many leading zeros")
        return 0 if z == 0 else (1 << z) - 1 + self.u(z)

    def se(self) -> int:
        c = self.ue()
        if c == 0:
            return 0
        return ((c + 1) >> 1) if c & 1 else -(c >> 1)


class _BW:
    """H.264 RBSP bit writer."""

    __slots__ = ("_bits",)

    def __init__(self) -> None:
        self._bits: list[int] = []

    def u(self, n: int, val: int) -> None:
        for i in range(n - 1, -1, -1):
            self._bits.append((val >> i) & 1)

    def ue(self, val: int) -> None:
        n = val + 1
        bl = n.bit_length()
        for _ in range(bl - 1):
            self._bits.append(0)
        for i in range(bl - 1, -1, -1):
            self._bits.append((n >> i) & 1)

    def se(self, val: int) -> None:
        self.ue(2 * val - 1 if val > 0 else -2 * val)

    def to_bytes(self) -> bytes:
        bits = list(self._bits) + [1] + [0] * ((-len(self._bits) - 1) % 8)
        out = bytearray()
        for i in range(0, len(bits), 8):
            b = 0
            for j in range(8):
                b = (b << 1) | bits[i + j]
            out.append(b)
        return bytes(out)


def _copy_scaling_list(r: _BR, w: _BW, size: int) -> None:
    last = 8
    nxt = 8
    for _ in range(size):
        if nxt != 0:
            delta = r.se()
            w.se(delta)
            nxt = (last + delta + 256) % 256
        last = nxt if nxt != 0 else last


def _copy_hrd(r: _BR, w: _BW) -> None:
    cpb = r.ue()
    w.ue(cpb)
    w.u(4, r.u(4))
    w.u(4, r.u(4))
    for _ in range(cpb + 1):
        w.ue(r.ue())
        w.ue(r.ue())
        w.u(1, r.u(1))
    w.u(5, r.u(5))
    w.u(5, r.u(5))
    w.u(5, r.u(5))
    w.u(5, r.u(5))


def _do_rewrite_sps(nal: bytes) -> bytes:
    r = _BR(_ep_remove(nal[1:]))
    w = _BW()

    profile_idc = r.u(8)
    w.u(8, profile_idc)
    w.u(8, r.u(8))
    w.u(8, r.u(8))
    w.ue(r.ue())

    chroma_format_idc = 1
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = r.ue()
        w.ue(chroma_format_idc)
        if chroma_format_idc == 3:
            w.u(1, r.u(1))
        w.ue(r.ue())
        w.ue(r.ue())
        w.u(1, r.u(1))
        ssmf = r.u(1)
        w.u(1, ssmf)
        if ssmf:
            n_lists = 12 if chroma_format_idc == 3 else 8
            for i in range(n_lists):
                flag = r.u(1)
                w.u(1, flag)
                if flag:
                    _copy_scaling_list(r, w, 16 if i < 6 else 64)

    w.ue(r.ue())
    poc = r.ue()
    w.ue(poc)
    if poc == 0:
        w.ue(r.ue())
    elif poc == 1:
        w.u(1, r.u(1))
        w.se(r.se())
        w.se(r.se())
        n = r.ue()
        w.ue(n)
        for _ in range(n):
            w.se(r.se())

    max_num_ref_frames = r.ue()
    w.ue(max_num_ref_frames)
    w.u(1, r.u(1))
    w.ue(r.ue())
    w.ue(r.ue())
    fmof = r.u(1)
    w.u(1, fmof)
    if not fmof:
        w.u(1, r.u(1))
    w.u(1, r.u(1))
    fcf = r.u(1)
    w.u(1, fcf)
    if fcf:
        w.ue(r.ue())
        w.ue(r.ue())
        w.ue(r.ue())
        w.ue(r.ue())

    vui_present = r.u(1) if r.remaining() > 0 else 0
    w.u(1, 1)

    def _write_restriction() -> None:
        w.u(1, 1)
        w.ue(2)
        w.ue(1)
        w.ue(16)
        w.ue(16)
        w.ue(0)
        w.ue(max_num_ref_frames)

    if not vui_present:
        w.u(2, 0)
        w.u(1, 0)
        w.u(5, 0)
        w.u(1, 1)
        _write_restriction()
    else:
        arif = r.u(1)
        w.u(1, arif)
        if arif:
            ari = r.u(8)
            w.u(8, ari)
            if ari == 255:
                w.u(16, r.u(16))
                w.u(16, r.u(16))

        oif = r.u(1)
        w.u(1, oif)
        if oif:
            w.u(1, r.u(1))

        vstf = r.u(1)
        w.u(1, 0)
        if vstf:
            r.u(3)
            r.u(1)
            cdpf = r.u(1)
            if cdpf:
                r.u(8)
                r.u(8)
                r.u(8)

        clif = r.u(1)
        w.u(1, clif)
        if clif:
            w.ue(r.ue())
            w.ue(r.ue())

        tif = r.u(1)
        w.u(1, tif)
        if tif:
            w.u(32, r.u(32))
            w.u(32, r.u(32))
            w.u(1, r.u(1))

        nhp = r.u(1)
        w.u(1, nhp)
        if nhp:
            _copy_hrd(r, w)

        vhp = r.u(1)
        w.u(1, vhp)
        if vhp:
            _copy_hrd(r, w)

        if nhp or vhp:
            w.u(1, r.u(1))

        w.u(1, r.u(1))

        brf = r.u(1)
        w.u(1, 1)
        if not brf:
            _write_restriction()
        else:
            w.u(1, r.u(1))
            w.ue(r.ue())
            w.ue(r.ue())
            w.ue(r.ue())
            w.ue(r.ue())
            r.ue()
            w.ue(0)
            r.ue()
            w.ue(max_num_ref_frames)

    return bytes([nal[0]]) + _ep_add(w.to_bytes())


def rewrite_sps_vui(nal: bytes) -> bytes:
    """Force bitstream_restriction_flag=1 and max_num_reorder_frames=0."""
    if not nal or (nal[0] & 0x1F) != 7:
        return nal
    try:
        return _do_rewrite_sps(nal)
    except Exception:
        log.debug("SPS rewrite failed; passthrough", exc_info=True)
        return nal


# --- RTP encryption -----------------------------------------------------------


def _encrypt(
    header: bytes,
    payload: bytes,
    mode: str,
    secret_key: list[int],
    nonce_counter: list[int],
) -> bytes:
    key = bytes(secret_key)

    if mode == "aead_xchacha20_poly1305_rtpsize":
        aead_box = nacl.secret.Aead(key)
        nonce = bytearray(24)
        struct.pack_into(">I", nonce, 0, nonce_counter[0])
        nonce_counter[0] = (nonce_counter[0] + 1) & 0xFFFF_FFFF
        ct = aead_box.encrypt(payload, bytes(header), bytes(nonce)).ciphertext
        return bytes(header) + ct + bytes(nonce[:4])

    if mode == "xsalsa20_poly1305":
        box = nacl.secret.SecretBox(key)
        nonce = bytearray(24)
        nonce[:12] = header
        return bytes(header) + box.encrypt(payload, bytes(nonce)).ciphertext

    if mode == "xsalsa20_poly1305_suffix":
        box = nacl.secret.SecretBox(key)
        nonce_bytes = nacl.utils.random(24)
        return (
            bytes(header) + box.encrypt(payload, nonce_bytes).ciphertext + nonce_bytes
        )

    if mode == "xsalsa20_poly1305_lite":
        box = nacl.secret.SecretBox(key)
        nonce = bytearray(24)
        struct.pack_into(">I", nonce, 0, nonce_counter[0])
        nonce_counter[0] = (nonce_counter[0] + 1) & 0xFFFF_FFFF
        ct = box.encrypt(payload, bytes(nonce)).ciphertext
        return bytes(header) + ct + bytes(nonce[:4])

    raise ValueError(f"Unknown voice encryption mode: {mode!r}")


# --- VideoStream --------------------------------------------------------------


class VideoStream:
    """Manages one video stream: FFmpeg subprocess + encrypted RTP output."""

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
        self.video_ssrc = audio_ssrc + 1
        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)
        self._nonce = [_VIDEO_NONCE_BASE]

        # Encryption params — set after voice handshake
        self._secret_key: list[int] = getattr(vc, "secret_key", [])
        self._mode: str = getattr(vc, "mode", "xsalsa20_poly1305")
        log.info("[STREAM] negotiated mode: %s, key len: %d", self._mode, len(self._secret_key))

        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._frame_nals: list[bytes] = []

    async def start(self) -> None:
        """Launch FFmpeg and begin send loop."""
        conn = self.vc._connection
        if hasattr(conn, "dave_session") and conn.dave_session and hasattr(conn.dave_session, "register_video_ssrc"):
            try:
                conn.dave_session.register_video_ssrc(self.video_ssrc)
                log.info("DAVE: registered video SSRC %d with H264 codec", self.video_ssrc)
            except Exception:
                log.warning("DAVE: failed to register video SSRC", exc_info=True)

        cmd = _ffmpeg_args(self.url)
        log.info(
            "[STREAM] guild=%s encoder=%s ffmpeg: %s ...",
            self.guild_id,
            _ENCODER,
            " ".join(cmd[:6]),
        )
        # Use subprocess.Popen (synchronous) so stdout is a plain file object.
        # This lets _send_loop call stdout.read() in a thread executor without
        # the asyncio pipe transport layer, avoiding the asyncio.wait_for
        # timeout problem that kills the loop during HLS probe delays.
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._task = asyncio.create_task(self._send_loop())
        log.info(
            "[STREAM] guild=%s started (video_ssrc=%d)", self.guild_id, self.video_ssrc
        )

    async def _send_loop(self) -> None:
        """Read raw H.264 from FFmpeg stdout and send encrypted RTP packets.

        Runs blocking stdout.read() in a thread executor so asyncio is never
        blocked, and HLS probe delays (ffmpeg -re can take 4-8 s before the
        first byte) never trigger a premature exit.
        """
        assert self._proc is not None
        assert self._proc.stdout is not None
        loop = asyncio.get_event_loop()
        raw_stdout = self._proc.stdout
        buf = b""

        def _blocking_read() -> bytes:
            return raw_stdout.read(65536)

        while not self._stop_event.is_set():
            try:
                chunk = await loop.run_in_executor(None, _blocking_read)
            except Exception as e:
                log.warning("[STREAM] guild=%s read error: %s", self.guild_id, e)
                break
            if not chunk:
                break
            buf += chunk
            nals = _parse_annexb(buf)
            if not nals:
                continue
            if nals:
                last = nals[-1]
                idx = buf.rfind(last)
                buf = buf[idx:] if idx >= 0 else b""
            else:
                buf = b""
            for nal in nals[:-1]:  # last NAL may be incomplete — keep in buf
                self._feed_nal(nal)
        self._flush_frame()
        
        self._proc.poll()
        if self._proc.returncode is not None and self._proc.returncode != 0:
            assert self._proc.stderr is not None
            err = self._proc.stderr.read().decode(errors="replace")
            log.error("[STREAM] ffmpeg failed (rc=%d): %s", self._proc.returncode, err)
            
        log.info("[STREAM] guild=%s send loop ended", self.guild_id)

    def _feed_nal(self, nal: bytes) -> None:
        """Queue a NAL unit. Flushes previous frame on new slice."""
        if _is_slice_nal(nal) and self._frame_nals:
            self._flush_frame()
        # Rewrite SPS VUI so Discord accepts the stream
        if nal and (nal[0] & 0x1F) == 7:
            nal = rewrite_sps_vui(nal)
        self._frame_nals.append(nal)

    def _flush_frame(self) -> None:
        """Send all buffered NALs as one frame (encrypted), advance timestamp."""
        if not self._frame_nals:
            return
            
        conn = self.vc._connection
        _dave = (
            conn.dave_session
            if (hasattr(conn, "dave_session") and conn.dave_session and hasattr(conn.dave_session, "encrypt_h264"))
            else None
        )

        rtp_nals: list[bytes]
        if _dave is not None:
            annex_b = b"".join(b"\x00\x00\x00\x01" + nal for nal in self._frame_nals)
            try:
                enc_frame = _dave.encrypt_h264(self.video_ssrc, annex_b)
                if enc_frame is not annex_b:
                    rtp_nals = []
                    offset = 0
                    for nal in self._frame_nals:
                        offset += 4
                        rtp_nals.append(enc_frame[offset : offset + len(nal)])
                        offset += len(nal)
                    if offset < len(enc_frame) and rtp_nals:
                        rtp_nals[-1] = rtp_nals[-1] + enc_frame[offset:]
                else:
                    rtp_nals = list(self._frame_nals)
            except Exception:
                log.debug("DAVE encrypt_h264 failed", exc_info=True)
                rtp_nals = list(self._frame_nals)
        else:
            rtp_nals = list(self._frame_nals)
            
        pkts = []
        for idx, nal in enumerate(rtp_nals):
            is_last_nal = (idx == len(rtp_nals) - 1)
            pkts.extend(_packetize_nal(nal, self.video_ssrc, self.seq, self.ts, is_last_nal))
            if pkts:
                self.seq = pkts[-1][1]
        if not pkts:
            self._frame_nals = []
            return

        # Encrypt all packets for this frame
        for pkt, _ in pkts:
            hdr = pkt[:_RTP_HEADER_SIZE]
            payload = pkt[_RTP_HEADER_SIZE:]
            try:
                encrypted = _encrypt(
                    hdr, payload, self._mode, self._secret_key, self._nonce
                )
                self.sock.sendto(encrypted, (self.endpoint_ip, self.endpoint_port))
            except (BlockingIOError, OSError) as e:
                log.debug("[STREAM] sendto dropped: %s", e)
                break

        self.ts = (self.ts + _TS_PER_FRAME) & 0xFFFFFFFF
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
                self._proc.wait()
            except Exception:
                pass
            self._proc = None
        log.info("[STREAM] guild=%s stopped", self.guild_id)

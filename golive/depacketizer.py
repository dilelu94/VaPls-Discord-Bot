"""H.264 RTP Depacketizer for Discord video streaming.

Parses incoming RTP packets, handles Single NAL units (types 1-23) and
Fragmentation Units (FU-A type 28), and yields Annex-B formatted NAL units
(prefixed with 0x00000001) ready for FFmpeg / PyAV decoding.
"""

from __future__ import annotations

import struct
import logging
from typing import Generator, Optional, Tuple

log = logging.getLogger(__name__)

ANNEXB_PREFIX = b"\x00\x00\x00\x01"


class H264RTPDepacketizer:
    """Reassembles Annex-B NAL units from RTP video packets (Payload Type 101/102)."""

    def __init__(self) -> None:
        self._fu_buffer: bytearray = bytearray()
        self._fu_in_progress: bool = False
        self._last_seq: Optional[int] = None

    def reset(self) -> None:
        self._fu_buffer.clear()
        self._fu_in_progress = False
        self._last_seq = None

    def depacketize(self, rtp_packet: bytes) -> Generator[bytes, None, None]:
        """Parse an RTP packet payload and yield complete Annex-B NAL unit(s)."""
        if len(rtp_packet) < 12:
            return

        # Parse 12-byte RTP header
        byte0, byte1, seq, ts, ssrc = struct.unpack("!BBHII", rtp_packet[:12])
        csrc_count = byte0 & 0x0F
        has_extension = bool((byte0 >> 4) & 0x01)

        offset = 12 + (csrc_count * 4)
        if len(rtp_packet) < offset:
            return

        if has_extension:
            if len(rtp_packet) < offset + 4:
                return
            ext_len = struct.unpack("!H", rtp_packet[offset + 2 : offset + 4])[0]
            offset += 4 + (ext_len * 4)

        payload = rtp_packet[offset:]
        if not payload or len(payload) < 4:
            return

        # Sequence continuity check for video stream
        if self._last_seq is not None:
            expected_seq = (self._last_seq + 1) & 0xFFFF
            diff = (seq - expected_seq) & 0xFFFF
            if diff > 50 and diff < 65000:
                log.debug("[DEPACK] Large sequence gap detected: expected %d, got %d", expected_seq, seq)
                if self._fu_in_progress:
                    log.warning("[DEPACK] Discarding incomplete FU-A buffer due to large sequence gap")
                    self._fu_buffer.clear()
                    self._fu_in_progress = False

        self._last_seq = seq

        fu_indicator = payload[0]
        nal_type = fu_indicator & 0x1F

        if not hasattr(self, "_depack_debug_count"):
            self._depack_debug_count = 0
        self._depack_debug_count += 1
        if self._depack_debug_count <= 10:
            log.info("[DEPACK-DEBUG] #%d: len=%d has_ext=%s offset=%d fu_ind=0x%02x nal_type=%d payload_hex=%s",
                     self._depack_debug_count, len(rtp_packet), has_extension, offset, fu_indicator, nal_type, payload[:16].hex())

        # Single NAL Unit (types 1..23)
        if 1 <= nal_type <= 23:
            if self._fu_in_progress:
                self._fu_buffer.clear()
                self._fu_in_progress = False
            clean_payload = bytes([payload[0] & 0x7F]) + payload[1:]
            yield ANNEXB_PREFIX + clean_payload

        # FU-A (Fragmentation Unit Type A - type 28 / 0x1C)
        elif nal_type == 28:
            if len(payload) < 2:
                return
            fu_header = payload[1]
            start_bit = bool((fu_header >> 7) & 0x01)
            end_bit = bool((fu_header >> 6) & 0x01)
            original_nal_type = fu_header & 0x1F

            reconstructed_nal_header = bytes([(fu_indicator & 0x60) | original_nal_type])

            if start_bit:
                self._fu_buffer.clear()
                self._fu_buffer.extend(ANNEXB_PREFIX)
                self._fu_buffer.extend(reconstructed_nal_header)
                self._fu_buffer.extend(payload[2:])
                self._fu_in_progress = True
            elif self._fu_in_progress:
                self._fu_buffer.extend(payload[2:])

            if end_bit and self._fu_in_progress:
                complete_nal = bytes(self._fu_buffer)
                self._fu_buffer.clear()
                self._fu_in_progress = False
                yield complete_nal

        # STAP-A (Single-Time Aggregation Packet Type A - type 24)
        elif nal_type == 24:
            pos = 1
            while pos + 2 < len(payload):
                nal_size = struct.unpack("!H", payload[pos : pos + 2])[0]
                pos += 2
                if pos + nal_size <= len(payload):
                    sub_nal = payload[pos : pos + nal_size]
                    if sub_nal:
                        clean_sub = bytes([sub_nal[0] & 0x7F]) + sub_nal[1:]
                        yield ANNEXB_PREFIX + clean_sub
                    pos += nal_size
                else:
                    break

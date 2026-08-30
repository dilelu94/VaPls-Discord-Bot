"""StreamSnapshotReceiver: Captures raw RTP video samples and extracts JPEG snapshots."""

from __future__ import annotations

import os
import time
import subprocess
import logging
from typing import Optional, List

from golive.depacketizer import H264RTPDepacketizer

log = logging.getLogger(__name__)


class StreamSnapshotReceiver:
    """Captures incoming H.264 RTP packets, decrypts them, reassembles Annex-B NAL units,

    and converts sample bursts into JPEG image snapshots for AI vision processing.
    """

    def __init__(self, output_dir: str = "data") -> None:
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self.depacketizer = H264RTPDepacketizer()
        self._raw_nal_buffer: bytearray = bytearray()
        self._sample_start_time: Optional[float] = None
        self._is_capturing: bool = False

    def start_capture(self) -> None:
        self.depacketizer.reset()
        self._raw_nal_buffer.clear()
        self._sample_start_time = time.monotonic()
        self._is_capturing = True
        log.info("[RECEIVER] Started video snapshot sample capture")

    def stop_capture(self) -> None:
        self._is_capturing = False
        log.info("[RECEIVER] Stopped video snapshot capture")

    def process_rtp_packet(
        self,
        rtp_data: bytes,
        dave_session=None,
        ssrc: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        """Process an incoming video RTP packet: DAVE decrypt -> Depacketize -> Buffer."""
        if len(rtp_data) < 12:
            return

        # Parse RTP header offset
        byte0 = rtp_data[0]
        csrc_count = byte0 & 0x0F
        has_extension = bool((byte0 >> 4) & 0x01)
        offset = 12 + (csrc_count * 4)

        if has_extension and len(rtp_data) >= offset + 4:
            ext_len = struct.unpack("!H", rtp_data[offset + 2 : offset + 4])[0]
            offset += 4 + (ext_len * 4)

        if len(rtp_data) <= offset:
            return

        header = rtp_data[:offset]
        raw_payload = rtp_data[offset:]

        # 1. DAVE E2EE video decryption on payload if session is present
        decrypted_payload = raw_payload
        if dave_session:
            if hasattr(dave_session, "decrypt_h264"):
                try:
                    decrypted_payload = dave_session.decrypt_h264(ssrc or 0, raw_payload, user_id=user_id)
                except Exception as exc:
                    log.warning("[RECEIVER] DAVE decrypt_h264 warning: %s", exc)
            elif hasattr(dave_session, "decrypt"):
                try:
                    decrypted_payload = dave_session.decrypt(user_id or 0, 1, raw_payload)
                except Exception as exc:
                    log.warning("[RECEIVER] DAVE decrypt warning: %s", exc)

        decrypted_rtp = header + decrypted_payload

        # 2. Depacketize RTP -> Annex-B NAL units
        for nal in self.depacketizer.depacketize(decrypted_rtp):
            self._raw_nal_buffer.extend(nal)

    def extract_snapshot(
        self,
        duration_sec: float = 3.0,
        filename: str = "latest_snapshot.jpg",
    ) -> Optional[str]:
        """Saves collected NAL units to disk and extracts a JPEG frame using FFmpeg.

        Returns absolute path to the generated JPEG image, or None if extraction failed.
        """
        if not self._raw_nal_buffer:
            log.warning("[RECEIVER] No NAL units collected in buffer")
            return None

        h264_path = os.path.join(self.output_dir, "sample_stream.h264")
        jpg_path = os.path.join(self.output_dir, filename)

        try:
            with open(h264_path, "wb") as f:
                f.write(self._raw_nal_buffer)
            log.info("[RECEIVER] Saved %d bytes of H.264 data to %s", len(self._raw_nal_buffer), h264_path)

            # Invoke FFmpeg to decode the H.264 stream sample and save the first keyframe as JPEG
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-probesize",
                "10M",
                "-analyzeduration",
                "10M",
                "-f",
                "h264",
                "-i",
                h264_path,
                "-vframes",
                "1",
                "-q:v",
                "2",
                jpg_path,
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)

            if res.returncode == 0 and os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 0:
                log.info("[RECEIVER] Snapshot extracted successfully: %s", jpg_path)
                return os.path.abspath(jpg_path)
            else:
                log.warning("[RECEIVER] FFmpeg snapshot extraction failed: %s", res.stderr.decode("utf-8", errors="ignore"))
                return None
        except Exception as exc:
            log.error("[RECEIVER] Failed to extract snapshot: %s", exc)
            return None

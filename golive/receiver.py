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
        self._sps_nal: Optional[bytes] = None
        self._pps_nal: Optional[bytes] = None
        self._sample_start_time: Optional[float] = None
        self._is_capturing: bool = False
        self._seen_keyframe: bool = False

    def start_capture(self) -> None:
        self.depacketizer.reset()
        self._raw_nal_buffer.clear()
        self._sps_nal = None
        self._pps_nal = None
        self._sample_start_time = time.monotonic()
        self._is_capturing = True
        self._seen_keyframe = False
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
        if not self._is_capturing or not rtp_data or len(rtp_data) < 12:
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
                    try:
                        import davey as dave
                    except ImportError:
                        try:
                            import dave
                        except ImportError:
                            dave = None
                    m_type = getattr(dave, "MediaType", None).video if (dave and hasattr(dave, "MediaType")) else 1
                    decrypted_payload = dave_session.decrypt(user_id or 0, m_type, raw_payload)
                except Exception as exc:
                    log.warning("[RECEIVER] DAVE decrypt warning: %s", exc)

        decrypted_rtp = header + decrypted_payload

        # 2. Depacketize RTP -> Annex-B NAL units (sync to first keyframe NAL 5, 7, 8)
        for nal in self.depacketizer.depacketize(decrypted_rtp):
            if len(nal) > 4:
                nal_type = nal[4] & 0x1F
                if nal_type == 7:
                    self._sps_nal = bytes(nal)
                elif nal_type == 8:
                    self._pps_nal = bytes(nal)

                if not self._seen_keyframe and nal_type in (5, 7, 8):
                    self._seen_keyframe = True
                    log.info("[RECEIVER] Synchronized to H.264 keyframe boundary (NAL type %d)", nal_type)

            if self._seen_keyframe:
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
            final_buffer = bytearray()
            if self._sps_nal and not self._raw_nal_buffer.startswith(self._sps_nal):
                final_buffer.extend(self._sps_nal)
            if self._pps_nal and self._pps_nal not in final_buffer and self._pps_nal not in self._raw_nal_buffer:
                final_buffer.extend(self._pps_nal)
            final_buffer.extend(self._raw_nal_buffer)

            with open(h264_path, "wb") as f:
                f.write(final_buffer)
            log.info("[RECEIVER] Saved %d bytes of H.264 data to %s", len(final_buffer), h264_path)

            # Invoke FFmpeg to decode the H.264 stream sample and save the first keyframe as JPEG
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-err_detect",
                "ignore_err",
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

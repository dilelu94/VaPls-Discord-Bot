"""StreamSnapshotReceiver: Captures raw RTP video samples and extracts JPEG snapshots."""

from __future__ import annotations

import os
import time
import struct
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
        self._nal_counts = {7: 0, 8: 0, 5: 0, 1: 0, 28: 0, "other": 0}
        log.info("[RECEIVER] Started video snapshot sample capture")

    def stop_capture(self) -> None:
        self._is_capturing = False
        log.info(
            "[RECEIVER] Stopped video capture: %d bytes buffered (SPS=%d, PPS=%d, IDR=%d, Slices=%d, FU-A=%d)",
            len(self._raw_nal_buffer),
            self._nal_counts.get(7, 0),
            self._nal_counts.get(8, 0),
            self._nal_counts.get(5, 0),
            self._nal_counts.get(1, 0),
            self._nal_counts.get(28, 0),
        )

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
                    exc_str = str(exc)
                    if "UnencryptedWhenPassthroughDisabled" in exc_str and hasattr(dave_session, "set_passthrough_mode"):
                        try:
                            dave_session.set_passthrough_mode(True, 10)
                        except Exception:
                            pass
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
                    exc_str = str(exc)
                    if "UnencryptedWhenPassthroughDisabled" in exc_str and hasattr(dave_session, "set_passthrough_mode"):
                        try:
                            dave_session.set_passthrough_mode(True, 10)
                        except Exception:
                            pass
                    log.warning("[RECEIVER] DAVE decrypt warning: %s", exc)

        has_ext = bool((rtp_data[0] >> 4) & 0x01)
        if has_ext and len(decrypted_payload) >= 4:
            try:
                ext_len = int.from_bytes(decrypted_payload[2:4], "big")
                ext_bytes = 4 + (ext_len * 4)
                if len(decrypted_payload) > ext_bytes:
                    decrypted_payload = decrypted_payload[ext_bytes:]
            except Exception:
                pass

        clean_hdr = bytearray(rtp_data[:12])
        clean_hdr[0] = 0x80  # Force RTP v2, no extension, 0 CSRC since extension was stripped above
        decrypted_rtp = bytes(clean_hdr) + decrypted_payload

        # 2. Depacketize RTP -> Annex-B NAL units (sync to first keyframe NAL 7 or keyframe with SPS/PPS)
        for nal in self.depacketizer.depacketize(decrypted_rtp):
            if len(nal) > 4:
                nal_type = nal[4] & 0x1F
                if nal_type in self._nal_counts:
                    self._nal_counts[nal_type] += 1
                else:
                    self._nal_counts["other"] += 1

                if nal_type == 7:
                    self._sps_nal = bytes(nal)
                elif nal_type == 8:
                    self._pps_nal = bytes(nal)

                if not self._seen_keyframe and (nal_type == 7 or (nal_type in (5, 8) and self._sps_nal and self._pps_nal)):
                    self._seen_keyframe = True
                    log.info("[RECEIVER] Synchronized to H.264 keyframe boundary (NAL type %d)", nal_type)

            if self._seen_keyframe:
                self._raw_nal_buffer.extend(nal)

    def extract_snapshot(
        self,
        duration_sec: float = 3.0,
        filename: str = "latest_snapshot.jpg",
    ) -> Optional[str]:
        """Saves collected NAL units to disk and extracts a JPEG frame using FFmpeg."""
        if not self._raw_nal_buffer:
            log.warning("[RECEIVER] No NAL units collected in buffer")
            return None

        jpg_path = os.path.join(self.output_dir, filename)
        h264_path = os.path.join(self.output_dir, "sample_stream.h264")

        try:
            final_buffer = bytearray()
            if self._sps_nal and self._sps_nal not in self._raw_nal_buffer:
                final_buffer.extend(self._sps_nal)
            if self._pps_nal and self._pps_nal not in self._raw_nal_buffer and self._pps_nal not in final_buffer:
                final_buffer.extend(self._pps_nal)
            final_buffer.extend(self._raw_nal_buffer)

            with open(h264_path, "wb") as f:
                f.write(final_buffer)
            log.info("[RECEIVER] Saved %d bytes of raw H.264 bitstream to %s", len(final_buffer), h264_path)

            # Direct JPEG extraction from raw H.264 bitstream using FFmpeg
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
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
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)

            if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 100:
                log.info("[RECEIVER] Snapshot extracted successfully: %s (%d bytes)", jpg_path, os.path.getsize(jpg_path))
                return os.path.abspath(jpg_path)

            # Fallback to MP4 conversion
            mp4_path = self.convert_sample_to_mp4(h264_filename="sample_stream.h264", mp4_filename="snapshot_stream.mp4")
            if mp4_path and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 100:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    mp4_path,
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    jpg_path,
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)
                if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 100:
                    log.info("[RECEIVER] Snapshot extracted from MP4 successfully: %s (%d bytes)", jpg_path, os.path.getsize(jpg_path))
                    return os.path.abspath(jpg_path)

            log.warning("[RECEIVER] FFmpeg snapshot extraction failed")
            return None
        except Exception as exc:
            log.error("[RECEIVER] Failed to extract snapshot: %s", exc)
            return None

    def convert_sample_to_mp4(
        self,
        h264_filename: str = "recorded_stream.h264",
        mp4_filename: str = "recorded_stream.mp4",
    ) -> Optional[str]:
        """Encodes collected NAL units into an MP4 video file and returns its path."""
        if not self._raw_nal_buffer:
            log.warning("[RECEIVER] No NAL units collected in buffer to convert to MP4")
            return None

        h264_path = os.path.join(self.output_dir, h264_filename)
        mp4_path = os.path.join(self.output_dir, mp4_filename)

        try:
            final_buffer = bytearray()
            if self._sps_nal and self._sps_nal not in self._raw_nal_buffer:
                final_buffer.extend(self._sps_nal)
            if self._pps_nal and self._pps_nal not in self._raw_nal_buffer and self._pps_nal not in final_buffer:
                final_buffer.extend(self._pps_nal)
            final_buffer.extend(self._raw_nal_buffer)

            with open(h264_path, "wb") as f:
                f.write(final_buffer)
            log.info("[RECEIVER] Saved %d bytes of raw H.264 bitstream to %s", len(final_buffer), h264_path)

            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "info",
                "-f",
                "h264",
                "-i",
                h264_path,
                "-c:v",
                "copy",
                mp4_path,
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30.0)
            stderr_out = res.stderr.decode("utf-8", errors="ignore")

            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1000:
                mp4_size = os.path.getsize(mp4_path)
                log.info(
                    "[RECEIVER] MP4 video encoded successfully: %s (%d bytes). FFmpeg log summary:\n%s",
                    mp4_path,
                    mp4_size,
                    "\n".join(stderr_out.splitlines()[-6:]),
                )
                return os.path.abspath(mp4_path)
            else:
                log.warning("[RECEIVER] FFmpeg MP4 encoding failed (rc=%d):\n%s", res.returncode, stderr_out)
                return None
        except Exception as exc:
            log.error("[RECEIVER] Failed to convert sample to MP4: %s", exc)
            return None

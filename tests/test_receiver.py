"""Tests for golive/receiver.py."""

import os
import struct
import tempfile
from unittest.mock import MagicMock, patch
from golive.receiver import StreamSnapshotReceiver


def make_rtp_packet(seq: int, payload: bytes) -> bytes:
    header = struct.pack("!BBHII", 0x80, 101, seq & 0xFFFF, 1000, 12345)
    return header + payload


def test_receiver_buffering():
    with tempfile.TemporaryDirectory() as tmpdir:
        receiver = StreamSnapshotReceiver(output_dir=tmpdir)
        receiver.start_capture()

        # IDR Keyframe NAL packet (NAL type 5: 0x05 & 0x1f = 5)
        pkt1 = make_rtp_packet(1, b"\x05\x88\x84\x00\x00")
        receiver.process_rtp_packet(pkt1)

        assert len(receiver._raw_nal_buffer) > 0
        assert b"\x00\x00\x00\x01\x05" in receiver._raw_nal_buffer


def test_extract_snapshot_ffmpeg_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        receiver = StreamSnapshotReceiver(output_dir=tmpdir)
        receiver.start_capture()

        pkt1 = make_rtp_packet(1, b"\x05\x88\x84\x00\x00")
        receiver.process_rtp_packet(pkt1)

        jpg_path = os.path.join(tmpdir, "test_snapshot.jpg")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            def create_fake_jpg(*args, **kwargs):
                cmd = args[0] if args else []
                if cmd and len(cmd) > 0:
                    out_file = cmd[-1]
                    with open(out_file, "wb") as f:
                        f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 2000)
                with open(jpg_path, "wb") as f:
                    f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 2000)
                res = MagicMock()
                res.returncode = 0
                res.stderr = b""
                return res

            mock_run.side_effect = create_fake_jpg

            res_path = receiver.extract_snapshot(filename="test_snapshot.jpg")
            assert res_path == os.path.abspath(jpg_path)
            assert os.path.exists(jpg_path)

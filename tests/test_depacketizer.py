"""Tests for golive/depacketizer.py."""

import struct
from golive.depacketizer import H264RTPDepacketizer, ANNEXB_PREFIX


def make_rtp_packet(seq: int, payload: bytes, pt: int = 101, ssrc: int = 12345) -> bytes:
    header = struct.pack("!BBHII", 0x80, pt & 0x7F, seq & 0xFFFF, 1000, ssrc)
    return header + payload


def test_single_nal_depacketization():
    depack = H264RTPDepacketizer()
    payload = b"\x07\x42\xe0\x1f"  # SPS (nal type 7)
    pkt = make_rtp_packet(seq=1, payload=payload)

    nals = list(depack.depacketize(pkt))
    assert len(nals) == 1
    assert nals[0] == ANNEXB_PREFIX + payload


def test_fua_fragmented_nal_depacketization():
    depack = H264RTPDepacketizer()

    # FU Indicator: 0x1C (type 28, NRI 0), FU Header Start: 0x85 (Start=1, End=0, NAL type 5 IDR)
    p1 = make_rtp_packet(seq=10, payload=b"\x1c\x85" + b"HEADER_PART_")
    # FU Indicator: 0x1C, FU Header Middle: 0x05 (Start=0, End=0, NAL type 5)
    p2 = make_rtp_packet(seq=11, payload=b"\x1c\x05" + b"MIDDLE_PART_")
    # FU Indicator: 0x1C, FU Header End: 0x45 (Start=0, End=1, NAL type 5)
    p3 = make_rtp_packet(seq=12, payload=b"\x1c\x45" + b"END_PART")

    res1 = list(depack.depacketize(p1))
    assert res1 == []

    res2 = list(depack.depacketize(p2))
    assert res2 == []

    res3 = list(depack.depacketize(p3))
    assert len(res3) == 1
    expected_header = bytes([(0x1C & 0xE0) | 5])  # 0x05 NAL header
    assert res3[0] == ANNEXB_PREFIX + expected_header + b"HEADER_PART_MIDDLE_PART_END_PART"


def test_stapa_aggregation_depacketization():
    depack = H264RTPDepacketizer()
    nal1 = b"\x07\x42\xe0\x1f"
    nal2 = b"\x08\xce\x3c\x80"
    payload = b"\x18" + struct.pack("!H", len(nal1)) + nal1 + struct.pack("!H", len(nal2)) + nal2
    pkt = make_rtp_packet(seq=20, payload=payload)

    nals = list(depack.depacketize(pkt))
    assert len(nals) == 2
    assert nals[0] == ANNEXB_PREFIX + nal1
    assert nals[1] == ANNEXB_PREFIX + nal2

"""
tests/test_dave_ciphertext_leak_prevention.py

Behavioral tests ensuring zero ciphertext leak into libopus.
Encrypted DAVE audio payloads (ending with 0xFAFA) that cannot be decrypted
must ALWAYS be silenced (replaced with Opus silence 0xF8FFFE) rather than
passed into libopus as raw pseudo-random bytes, preventing full-scale digital
static ("puro ruido").
"""

from unittest.mock import MagicMock
import pytest

dave = pytest.importorskip("dave")
from davey_compat import DaveSession, _OPUS_SILENCE
from userbot.bot import _process_dave_audio_payload


def test_dave_decrypt_audio_returns_silence_when_ratchet_missing_and_trailer_is_fafa():
    """When a packet has the DAVE 0xFAFA trailer and no key ratchet is available, return Opus silence."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_impl = MagicMock()
    mock_impl.get_key_ratchet.return_value = None
    session._session = mock_impl

    ciphertext_packet = b"\x12\x34\x56\x78\xfa\xfa"
    result = session.decrypt(111, "audio", ciphertext_packet)

    assert result == _OPUS_SILENCE
    assert result != ciphertext_packet


def test_dave_decrypt_audio_returns_silence_when_decryptor_raises_and_trailer_is_fafa():
    """When decryptor raises an exception on a packet with 0xFAFA trailer, return Opus silence."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_dec = MagicMock()
    mock_dec.decrypt.side_effect = RuntimeError("libdave internal error")
    session._audio_decryptors["111"] = mock_dec
    session._audio_decryptor_epochs["111"] = 1
    session._epoch = 1

    ciphertext_packet = b"\xde\xad\xbe\xef\xfa\xfa"
    result = session.decrypt(111, "audio", ciphertext_packet)

    assert result == _OPUS_SILENCE


def test_dave_decrypt_audio_returns_plaintext_on_successful_decryption():
    """When decryption succeeds, plaintext Opus is returned intact."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_dec = MagicMock()
    mock_dec.decrypt.return_value = b"\xf8\x01\x02\x03"  # valid plaintext opus
    session._audio_decryptors["111"] = mock_dec
    session._audio_decryptor_epochs["111"] = 1
    session._epoch = 1

    ciphertext_packet = b"\xaa\xbb\xcc\xfa\xfa"
    result = session.decrypt(111, "audio", ciphertext_packet)

    assert result == b"\xf8\x01\x02\x03"


def test_dave_decrypt_audio_passthrough_non_dave_packet():
    """Non-DAVE packets (not ending with 0xFAFA) pass through unmodified when passthrough is active."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_impl = MagicMock()
    mock_impl.get_key_ratchet.return_value = None
    session._session = mock_impl

    raw_opus = b"\x78\x00\x11\x22"
    result = session.decrypt(111, "audio", raw_opus)

    assert result == raw_opus


def test_process_dave_audio_payload_silences_unmapped_ssrc_ciphertext():
    """If an RTP audio packet ends with 0xFAFA and SSRC/uid is unknown/unmapped, return silence."""
    mock_vc = MagicMock()
    mock_vc._connection = MagicMock(dave_protocol_version=1, dave_session=None)
    mock_packet = MagicMock(ssrc=99999)

    raw_ciphertext = b"\x00\x01\x02\x03\xfa\xfa"
    result = _process_dave_audio_payload(
        raw=raw_ciphertext,
        packet=mock_packet,
        vc=mock_vc,
        dave=None,
        uid=None,
        ssrc_map={},
    )

    assert result == _OPUS_SILENCE


def test_process_dave_audio_payload_silences_when_dave_ready_is_false():
    """If an RTP audio packet ends with 0xFAFA and dave.ready is False, return silence."""
    mock_dave = MagicMock(ready=False)
    mock_vc = MagicMock()
    mock_vc._connection = MagicMock(dave_protocol_version=1, dave_session=mock_dave)
    mock_packet = MagicMock(ssrc=12345)

    raw_ciphertext = b"\xaa\xbb\xcc\xdd\xfa\xfa"
    result = _process_dave_audio_payload(
        raw=raw_ciphertext,
        packet=mock_packet,
        vc=mock_vc,
        dave=mock_dave,
        uid=555,
        ssrc_map={12345: 555},
    )

    assert result == _OPUS_SILENCE


def test_process_dave_audio_payload_silences_on_decryption_exception():
    """If dave.decrypt raises an exception on a packet ending in 0xFAFA, return silence."""
    mock_dave = MagicMock(ready=True)
    mock_dave.decrypt.side_effect = RuntimeError("Failed to decrypt")
    mock_vc = MagicMock()
    mock_vc._connection = MagicMock(dave_protocol_version=1, dave_session=mock_dave)
    mock_packet = MagicMock(ssrc=12345)

    raw_ciphertext = b"\xfe\xed\xfa\xce\xfa\xfa"
    result = _process_dave_audio_payload(
        raw=raw_ciphertext,
        packet=mock_packet,
        vc=mock_vc,
        dave=mock_dave,
        uid=555,
        ssrc_map={12345: 555},
    )

    assert result == _OPUS_SILENCE


def test_process_dave_audio_payload_passes_plaintext_when_decrypted():
    """When DAVE decryption succeeds, plaintext Opus payload is returned."""
    mock_dave = MagicMock(ready=True)
    plaintext_opus = b"\xf8\xde\xad\xbe\xef"
    mock_dave.decrypt.return_value = plaintext_opus

    mock_vc = MagicMock()
    mock_vc._connection = MagicMock(dave_protocol_version=1, dave_session=mock_dave)
    mock_packet = MagicMock(ssrc=12345)

    raw_ciphertext = b"\x11\x22\x33\xfa\xfa"
    result = _process_dave_audio_payload(
        raw=raw_ciphertext,
        packet=mock_packet,
        vc=mock_vc,
        dave=mock_dave,
        uid=555,
        ssrc_map={12345: 555},
    )

    assert result == plaintext_opus


def test_process_dave_audio_payload_passthrough_unencrypted_channel():
    """In an unencrypted non-DAVE voice channel, standard Opus audio passes through untouched."""
    mock_vc = MagicMock()
    mock_vc._connection = MagicMock(dave_protocol_version=0, dave_session=None)
    mock_vc.dave_protocol_version = 0
    mock_packet = MagicMock(ssrc=12345)

    normal_opus = b"\x78\x12\x34\x56"
    result = _process_dave_audio_payload(
        raw=normal_opus,
        packet=mock_packet,
        vc=mock_vc,
        dave=None,
        uid=555,
        ssrc_map={12345: 555},
    )

    assert result == normal_opus

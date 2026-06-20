"""
davey_compat.py – Compatibility shim for discord.py-self's DAVE/E2EE support.

Wraps dave.py (DisnakeDev's Python bindings for Discord's official C++ libdave)
to expose the same API that discord.py-self expects from the "davey" package.

discord.py-self 2.1.0 imports davey in voice_state.py and gateway.py. By
replacing those module-level references with this shim, the working libdave
implementation is used instead of davey's broken Rust binding.

Applied in bot.py before any voice connections are made:
    import discord.voice_state, discord.gateway
    import davey_compat
    discord.voice_state.davey = davey_compat
    discord.gateway.davey     = davey_compat
    davey_compat.patch_reinit(discord.voice_state)
"""

from __future__ import annotations

import logging
import dave

log = logging.getLogger(__name__)

# ── Protocol version ──────────────────────────────────────────────────────────

DAVE_PROTOCOL_VERSION: int = dave.get_max_supported_protocol_version()


# ── ProposalsOperationType ────────────────────────────────────────────────────
# gateway.py passes davey.ProposalsOperationType.append / .revoke as the first
# argument to dave_session.process_proposals(). Values are 0 / 1.


class ProposalsOperationType:
    append = 0
    revoke = 1


# ── CommitWelcome ─────────────────────────────────────────────────────────────
# gateway.py does:
#   if isinstance(result, davey.CommitWelcome):
#       await send_binary(result.commit + (result.welcome or b''))


class CommitWelcome:
    __slots__ = ("commit", "welcome")

    def __init__(self, data: bytes) -> None:
        self.commit = data  # dave.py returns combined commit(+welcome) bytes
        self.welcome = None  # already concatenated into .commit


# ── Fake stats wrapper ────────────────────────────────────────────────────────


class _EncryptionStats:
    __slots__ = ("attempts", "successes", "failures")

    def __init__(self, s) -> None:
        self.attempts = getattr(s, "encrypt_attempts", 0)
        self.successes = getattr(s, "encrypt_success_count", 0)
        self.failures = getattr(s, "encrypt_failure_count", 0)


# ── SessionStatus ─────────────────────────────────────────────────────────────


class SessionStatus:
    inactive = 0
    pending = 1
    active = 3


# ── DaveSession ───────────────────────────────────────────────────────────────


class DaveSession:
    """
    Drop-in replacement for davey.DaveSession, backed by dave.py (libdave).

    Constructed the same way discord.py-self does:
        DaveSession(protocol_version, user_id, channel_id)
    """

    # Fixed SSRC=0 for our single outgoing audio stream.
    _SSRC: int = 0

    def __init__(self, protocol_version: int, user_id: int, channel_id: int) -> None:
        self._protocol_version = protocol_version
        self._user_id = user_id
        self._channel_id = channel_id

        # Injected by patch_reinit() so we can read channel.members for
        # recognized_user_ids. None until the patch runs.
        self._voice_state = None

        self._session = dave.Session(mls_failure_callback=self._on_mls_failure)
        self._encryptor = dave.Encryptor()
        self._encryptor.assign_ssrc_to_codec(self._SSRC, dave.Codec.opus)
        self._encryptor.set_passthrough_mode(True)  # passthrough until key ready

        self._ready = False
        self._epoch: int | None = None
        self.status = SessionStatus.inactive
        self.voice_privacy_code: str | None = None

        self._do_init()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _on_mls_failure(self, reason: str, detail: str) -> None:
        log.error("[DAVE] MLS failure: %s – %s", reason, detail)

    def _do_init(self) -> None:
        self._session.init(
            self._protocol_version,
            self._channel_id,  # group_id == voice channel id
            str(self._user_id),  # libdave expects user_id as str
        )
        self.status = SessionStatus.pending

    def _get_recognized_users(self) -> set:
        """
        Build the set of string user IDs that libdave should trust.
        Always includes the bot itself; adds current voice-channel members
        when a back-reference to VoiceConnectionState is available.
        """
        users = {str(self._user_id)}
        if self._voice_state is not None:
            try:
                channel = self._voice_state.voice_client.channel
                for member in channel.members:
                    users.add(str(member.id))
            except Exception as exc:
                log.warning("[DAVE] Could not read channel members: %s", exc)
        return users

    def _refresh_key(self) -> None:
        """Pull our own key ratchet from the MLS session → give it to Encryptor."""
        ratchet = self._session.get_key_ratchet(str(self._user_id))
        if ratchet is not None:
            self._encryptor.set_key_ratchet(ratchet)
            self._encryptor.set_passthrough_mode(False)
            self._ready = True
            self.status = SessionStatus.active
        else:
            log.warning("[DAVE] WARNING: get_key_ratchet returned None")

    # ── Public API (mirrors davey.DaveSession) ────────────────────────────────

    def reinit(self, protocol_version: int, user_id: int, channel_id: int) -> None:
        self._protocol_version = protocol_version
        self._user_id = user_id
        self._channel_id = channel_id
        self._session.reset()
        self._ready = False
        self._epoch = None
        self.status = SessionStatus.inactive
        self._encryptor.set_key_ratchet(None)
        self._encryptor.set_passthrough_mode(True)
        self._do_init()

    def reset(self) -> None:
        self._session.reset()
        self._ready = False
        self._epoch = None
        self.status = SessionStatus.inactive
        self._encryptor.set_key_ratchet(None)
        self._encryptor.set_passthrough_mode(True)

    def get_serialized_key_package(self) -> bytes:
        """Returns the MLS key package to send to Discord (opcode MLS_KEY_PACKAGE)."""
        return self._session.get_marshalled_key_package()

    def set_external_sender(self, data: bytes) -> None:
        self._session.set_external_sender(data)

    def set_passthrough_mode(self, passthrough: bool, transition_expiry=None) -> None:
        self._encryptor.set_passthrough_mode(passthrough)

    def process_proposals(self, optype, proposals: bytes):
        """
        Called by gateway.py with (ProposalsOperationType, msg[4:]).

        discord.py-self extracts the proposals_op_type byte from msg[3] and
        passes msg[4:] here. libdave's process_proposals expects msg[3:] — the
        op-type byte must be RE-PREPENDED before handing to libdave.
        """
        optype_byte = bytes([0 if optype == ProposalsOperationType.append else 1])
        full_data = optype_byte + proposals
        recognized = self._get_recognized_users()
        try:
            result = self._session.process_proposals(full_data, recognized)
        except Exception as exc:
            log.error("[DAVE] process_proposals FAILED: %s", exc)
            return None
        if result is not None:
            return CommitWelcome(result)
        return None

    def process_commit(self, commit: bytes) -> None:
        """Called by gateway.py. Raises on rejection so gateway can recover."""
        result = self._session.process_commit(commit)
        if isinstance(result, dave.RejectType):
            raise RuntimeError(f"DAVE commit rejected: {result.name}")
        if isinstance(result, dict) and result:
            self._epoch = max(result.keys())
        self._refresh_key()

    def process_welcome(self, welcome: bytes) -> None:
        """Called by gateway.py. Raises on failure so gateway can recover."""
        recognized = self._get_recognized_users()
        try:
            result = self._session.process_welcome(welcome, recognized)
        except Exception as exc:
            log.error("[DAVE] process_welcome FAILED: %s", exc)
            raise
        if result is None:
            raise RuntimeError("DAVE welcome rejected by libdave")
        if isinstance(result, dict) and result:
            self._epoch = max(result.keys())
        self._refresh_key()

    def encrypt_opus(self, data: bytes) -> bytes:
        """DAVE-encrypt an Opus frame before transport encryption."""
        result = self._encryptor.encrypt(dave.MediaType.audio, self._SSRC, data)
        if result is None:
            return data  # passthrough (no key yet)
        return result

    def register_video_ssrc(self, video_ssrc: int) -> None:
        """Register a video SSRC with the H.264 codec so DAVE can encrypt it."""
        try:
            self._encryptor.assign_ssrc_to_codec(video_ssrc, dave.Codec.h264)
        except Exception as exc:
            log.warning("[DAVE] register_video_ssrc(%s) failed: %s", video_ssrc, exc)

    def encrypt_h264(self, video_ssrc: int, data: bytes) -> bytes:
        """DAVE-encrypt an H.264 RTP payload before transport encryption."""
        result = self._encryptor.encrypt(dave.MediaType.video, video_ssrc, data)
        if result is None:
            return data  # passthrough (no key yet)
        return result

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def epoch(self):
        return self._epoch

    # ── Diagnostic helpers ────────────────────────────────────────────────────

    def get_user_ids(self) -> list:
        return []

    def get_encryption_stats(self):
        try:
            return _EncryptionStats(self._encryptor.get_stats(dave.MediaType.audio))
        except Exception:
            return type("_S", (), {"attempts": 0, "successes": 0, "failures": 0})()

    def decrypt(self, user_id: int, media_type, packet: bytes) -> bytes:
        raise RuntimeError("DaveSession.decrypt() not implemented in davey_compat")

    def __repr__(self) -> str:
        return (
            f"<DaveSession(libdave) epoch={self._epoch} ready={self._ready} "
            f"status={self.status}>"
        )


# ── Patch helper ──────────────────────────────────────────────────────────────


def patch_reinit(voice_state_module) -> None:
    """
    Monkey-patch VoiceConnectionState.reinit_dave_session so that every
    freshly-created DaveSession gets a back-reference (_voice_state) to the
    VoiceConnectionState that owns it. This lets _get_recognized_users() read
    the current voice-channel member list.

    Call once from bot.py after patching discord.voice_state.davey:
        davey_compat.patch_reinit(discord.voice_state)
    """
    original = voice_state_module.VoiceConnectionState.reinit_dave_session

    async def _patched(self_state):
        await original(self_state)
        ds = self_state.dave_session
        if ds is not None and isinstance(ds, DaveSession):
            ds._voice_state = self_state

    voice_state_module.VoiceConnectionState.reinit_dave_session = _patched

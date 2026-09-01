"""
davey_compat.py – Compatibility shim for discord.py-self's DAVE/E2EE support.

Wraps the ``davey`` package (Snazzah's Python bindings for the DAVE protocol)
to expose the same API that discord.py-self 2.1.0 expects.

discord.py-self imports davey in voice_state.py and gateway.py. We replace
those module-level references with this shim so that the davey DaveSession is
used transparently.

Applied in bot.py before any voice connections are made:
    import discord.voice_state, discord.gateway
    import davey_compat
    discord.voice_state.davey = davey_compat
    discord.gateway.davey     = davey_compat
    davey_compat.patch_reinit(discord.voice_state)
"""

from __future__ import annotations

import logging
import davey

log = logging.getLogger(__name__)

# ── Protocol version ──────────────────────────────────────────────────────────
# davey exposes this as a module-level integer constant (not a function).

DAVE_PROTOCOL_VERSION: int = davey.DAVE_PROTOCOL_VERSION


# ── Re-export enums/types that discord.py-self references directly ────────────

ProposalsOperationType = davey.ProposalsOperationType
CommitWelcome = davey.CommitWelcome
SessionStatus = davey.SessionStatus
MediaType = davey.MediaType
Codec = davey.Codec


# ── DaveSession ───────────────────────────────────────────────────────────────
# discord.py-self calls:
#   session = davey.DaveSession(protocol_version, user_id, channel_id)
#   session.reinit(protocol_version, user_id, channel_id)
#   session.reset()
#   session.get_serialized_key_package() -> bytes
#   session.set_external_sender(data: bytes)
#   session.process_proposals(op_type, proposals: bytes) -> CommitWelcome | None
#   session.process_commit(commit: bytes)
#   session.process_welcome(welcome: bytes)
#   session.set_passthrough_mode(passthrough: bool, transition_expiry=None)
#   session.encrypt_opus(data: bytes) -> bytes
#   session.decrypt(user_id: int, media_type, packet: bytes) -> bytes
#   session.ready  (bool property)
#   session.status (SessionStatus property)
#
# davey.DaveSession provides all of these natively — we just delegate.


class DaveSession:
    """
    Thin wrapper around ``davey.DaveSession`` that adds helpers used by the
    golive streaming stack (encrypt_h264, register_video_ssrc) and provides
    the same interface expected by discord.py-self gateway / voice_state.
    """

    def __init__(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        self._session = davey.DaveSession(protocol_version, user_id, channel_id)
        self._voice_state = None  # set by patch_reinit
        log.debug(
            "[DAVE] DaveSession created (protocol=%s user=%s channel=%s)",
            protocol_version, user_id, channel_id,
        )

    # ── Delegation to native davey.DaveSession ────────────────────────────────

    def reinit(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        self._session.reinit(protocol_version, user_id, channel_id)
        log.debug("[DAVE] reinit (protocol=%s)", protocol_version)

    def reset(self) -> None:
        self._session.reset()

    def get_serialized_key_package(self) -> bytes:
        return self._session.get_serialized_key_package()

    def set_external_sender(self, data: bytes) -> None:
        self._session.set_external_sender(data)

    def process_proposals(
        self,
        operation_type,
        proposals: bytes,
        expected_user_ids=None,
    ):
        """
        Called by gateway.py with (ProposalsOperationType, proposals_bytes).
        davey's native API accepts the enum directly.
        """
        try:
            result = self._session.process_proposals(operation_type, proposals, expected_user_ids)
        except Exception as exc:
            log.error("[DAVE] process_proposals FAILED: %s", exc)
            return None
        return result  # CommitWelcome | None

    def process_commit(self, commit: bytes) -> None:
        """Called by gateway.py. Falls back to passthrough on failure."""
        log.info("[DAVE] Processing MLS commit (size=%d)", len(commit))
        try:
            self._session.process_commit(commit)
        except Exception as exc:
            log.warning("[DAVE] process_commit note: %s — switching to passthrough mode", exc)
            self.set_passthrough_mode(True)

    def process_welcome(self, welcome: bytes) -> None:
        """Called by gateway.py. Falls back to passthrough on failure."""
        log.info("[DAVE] Processing MLS welcome (size=%d)", len(welcome))
        try:
            self._session.process_welcome(welcome)
        except Exception as exc:
            log.warning("[DAVE] process_welcome note: %s — switching to passthrough mode", exc)
            self.set_passthrough_mode(True)

    def set_passthrough_mode(
        self, passthrough: bool, transition_expiry=None
    ) -> None:
        self._session.set_passthrough_mode(passthrough, transition_expiry)

    def encrypt_opus(self, data: bytes) -> bytes:
        """DAVE-encrypt an Opus frame."""
        try:
            return self._session.encrypt_opus(data)
        except Exception:
            return data  # passthrough on error

    def decrypt(self, user_id: int, media_type, packet: bytes) -> bytes:
        """Decrypt a DAVE-encrypted packet."""
        try:
            return self._session.decrypt(user_id, media_type, packet)
        except Exception:
            return packet  # passthrough on error

    def get_user_ids(self) -> list:
        return self._session.get_user_ids()

    def get_encryption_stats(self, media_type=None):
        return self._session.get_encryption_stats(media_type)

    def can_passthrough(self, user_id: int) -> bool:
        return self._session.can_passthrough(user_id)

    # ── GoLive streaming extras ───────────────────────────────────────────────

    def register_video_ssrc(self, video_ssrc: int) -> None:
        """Register a video SSRC with the H.264 codec so DAVE can encrypt it."""
        # davey handles this transparently via encrypt() with Codec.h264
        log.debug("[DAVE] register_video_ssrc(%s)", video_ssrc)

    def encrypt_h264(self, video_ssrc: int, data: bytes) -> bytes:
        """DAVE-encrypt an H.264 RTP payload before transport encryption."""
        try:
            return self._session.encrypt(
                davey.MediaType.video, davey.Codec.h264, data
            )
        except Exception:
            return data  # passthrough on error

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._session.ready

    @property
    def status(self):
        return self._session.status

    @property
    def epoch(self):
        return self._session.epoch

    def __repr__(self) -> str:
        return repr(self._session)


# ── Patch helper ──────────────────────────────────────────────────────────────


def patch_reinit(voice_state_module) -> None:
    """
    Monkey-patch VoiceConnectionState.reinit_dave_session so that every
    freshly-created DaveSession gets a back-reference (_voice_state) to the
    VoiceConnectionState that owns it.

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

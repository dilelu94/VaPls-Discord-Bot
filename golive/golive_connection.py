"""
golive_connection.py — Compatibility proxy pointing to golive.slopsoil.golive.

Unifies all references across the codebase to the canonical Slopsoil GoLive implementation.
"""
from __future__ import annotations

from golive.slopsoil.golive import (
    GoLiveConnection,
    _GoLiveVCProxy,
    GoLiveAudioSender,
    _encrypt_audio,
    _av_sync_ms,
    _OP_STREAM_CREATE,
    _OP_STREAM_DELETE,
    _OP_STREAM_SET_PAUSED,
)

__all__ = [
    "GoLiveConnection",
    "_GoLiveVCProxy",
    "GoLiveAudioSender",
    "_encrypt_audio",
    "_av_sync_ms",
    "_OP_STREAM_CREATE",
    "_OP_STREAM_DELETE",
    "_OP_STREAM_SET_PAUSED",
]

"""
golive_connection.py — Root compatibility proxy pointing to Slopsoil golive implementation.
"""
from __future__ import annotations

try:
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
except ModuleNotFoundError:
    from slopsoil.golive import (
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

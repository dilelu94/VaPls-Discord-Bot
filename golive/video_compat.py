"""Patches discord.py-self DiscordVoiceWebSocket to advertise H.264 video.

Three patches must be applied before any voice connections:

1. identify() — adds video: true + streams descriptor to IDENTIFY (op 0)
2. select_protocol() — adds H264 codec to SELECT_PROTOCOL (op 1)
3. client_connect() — adds video_ssrc + streams to VIDEO (op 12)

Without these, Discord silently drops all video RTP packets.

client_connect() reads the actual encoder config from env vars (same vars as
streamer.py) so max_bitrate/max_framerate/max_resolution match what FFmpeg is
configured to produce. Free Discord accounts are capped at 720p30.
"""

from __future__ import annotations

import os
import re

H264_PAYLOAD_TYPE: int = 101
VIDEO_SSRC_OFFSET: int = 1
RTX_SSRC_OFFSET: int = 2

_STREAM_PRESETS: dict[str, tuple[int, int, int, int]] = {
    "720p": (1280, 720, 30, 2_500_000),
    "1080p": (1920, 1080, 30, 4_500_000),
    "4k": (3840, 2160, 30, 8_000_000),
}


def _stream_cfg() -> tuple[int, int, int, int]:
    """(width, height, fps, bitrate) from env, capped at 720p30 for free accts."""
    raw_q = os.environ.get("STREAM_QUALITY", "").strip().lower()
    w, h, f, br = _STREAM_PRESETS.get(raw_q, _STREAM_PRESETS["720p"])

    raw_res = os.environ.get("STREAM_RESOLUTION", "").strip().replace("x", ":")
    if raw_res and re.fullmatch(r"\d{2,5}:\d{2,5}", raw_res):
        parts = raw_res.split(":")
        w, h = int(parts[0]), int(parts[1])

    raw_fps = os.environ.get("STREAM_FPS", "").strip()
    if raw_fps:
        try:
            f = int(float(raw_fps))
        except ValueError:
            pass

    raw_br = os.environ.get("STREAM_VIDEO_BITRATE", "").strip()
    if raw_br:
        br = _parse_bitrate(raw_br)

    # Cap at 720p30 (Discord free account limit)
    w = min(w, 1280)
    h = min(h, 720)
    f = min(f, 30)
    return w, h, f, br


def _parse_bitrate(s: str) -> int:
    s = s.lower().strip()
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    return int(s)


async def _patched_identify(self) -> None:
    state = self._connection
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "max_dave_protocol_version": state.max_dave_protocol_version,
            "video": True,
            "streams": [{"type": "video", "rid": "100", "quality": 100}],
        },
    }
    await self.send_as_json(payload)


async def _patched_select_protocol(self, ip: str, port: int, mode: str) -> None:
    payload = {
        "op": self.SELECT_PROTOCOL,
        "d": {
            "protocol": "udp",
            "data": {"address": ip, "port": port, "mode": mode},
            "codecs": [
                {
                    "name": "opus",
                    "type": "audio",
                    "priority": 1000,
                    "payload_type": 120,
                },
                {
                    "name": "H264",
                    "type": "video",
                    "priority": 1000,
                    "payload_type": H264_PAYLOAD_TYPE,
                    "rtx_payload_type": 102,
                },
            ],
        },
    }
    await self.send_as_json(payload)


async def _patched_client_connect(self) -> None:
    ssrc = self._connection.ssrc
    video_ssrc = ssrc + VIDEO_SSRC_OFFSET
    rtx_ssrc = ssrc + RTX_SSRC_OFFSET
    w, h, f, br = _stream_cfg()
    payload = {
        "op": getattr(self, "VIDEO", 12),
        "d": {
            "audio_ssrc": ssrc,
            "video_ssrc": video_ssrc,
            "rtx_ssrc": rtx_ssrc,
            "streams": [
                {
                    "type": "video",
                    "rid": "100",
                    "ssrc": video_ssrc,
                    "active": True,
                    "quality": 100,
                    "rtx_ssrc": rtx_ssrc,
                    "max_bitrate": br,
                    "max_framerate": f,
                    "max_resolution": {
                        "type": "fixed",
                        "width": w,
                        "height": h,
                    },
                }
            ],
        },
    }
    await self.send_as_json(payload)


def patch_video(gateway_module) -> None:
    """Apply video patches. Call once before any voice connections.

    Usage:
        import discord.gateway
        import golive.video_compat as vc
        vc.patch_video(discord.gateway)
    """
    gateway_module.DiscordVoiceWebSocket.identify = _patched_identify
    gateway_module.DiscordVoiceWebSocket.select_protocol = _patched_select_protocol
    gateway_module.DiscordVoiceWebSocket.client_connect = _patched_client_connect

"""Patches discord.py-self DiscordVoiceWebSocket to advertise H.264 video.

Three patches must be applied before any voice connections:

1. identify() — adds video: true + streams descriptor to IDENTIFY (op 0)
2. select_protocol() — adds H264 codec to SELECT_PROTOCOL (op 1)
3. client_connect() — adds video_ssrc + streams to VIDEO (op 12)

Without these, Discord silently drops all video RTP packets.
"""

from __future__ import annotations

H264_PAYLOAD_TYPE: int = 101
VIDEO_SSRC_OFFSET: int = 1
RTX_SSRC_OFFSET: int = 2


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
                    "max_bitrate": 2_500_000,
                    "max_framerate": 30,
                    "max_resolution": {
                        "type": "fixed",
                        "width": 1280,
                        "height": 720,
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

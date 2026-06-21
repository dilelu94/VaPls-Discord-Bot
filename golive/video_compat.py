"""
video_compat.py – Patches discord.py-self to support H.264 video streaming.

Three patches are applied in bot.py before any voice connections are made:

1.  DiscordVoiceWebSocket.identify
    Adds `video: true` and a `streams` descriptor to the IDENTIFY (op 0)
    payload.  Without this the voice server never sets up video forwarding,
    so all video RTP packets are silently dropped regardless of op 12.

2.  DiscordVoiceWebSocket.select_protocol
    Adds a `codecs` field to the SELECT_PROTOCOL (op 1) payload so Discord's
    voice server knows we support H.264 video alongside Opus audio.

3.  DiscordVoiceWebSocket.client_connect
    Extends the VIDEO (op 12) payload to include `video_ssrc`, `rtx_ssrc`, and
    a `streams` array with the required `max_bitrate`, `max_framerate`, and
    `max_resolution` fields that Discord needs to allocate bandwidth.

The video SSRC is always audio_ssrc + 1 (VIDEO_SSRC_OFFSET).  The RTX SSRC
(used for retransmission, not sent by the bot) is audio_ssrc + 2.
"""

from __future__ import annotations

# H.264 dynamic payload type Discord uses (matches what the official client sends)
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
        "op": self.VIDEO,
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
                    "max_bitrate": 10_000_000,
                    "max_framerate": 60,
                    "max_resolution": {
                        "type": "fixed",
                        "width": 1920,
                        "height": 1080,
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

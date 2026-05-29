"""
VaPls userbot: listens to Discord voice channels using a real user account
(so DAVE E2EE works naturally) and transcribes Spanish speech with VOSK.

Runs separately from the main Discord bot — the main bot still handles
/play, /soundpad, slash commands, etc. This userbot is voice-input-only.

Library stack: discord.py-self (user-token client) + discord-ext-voice-recv
(voice receive extension) + vosk (offline ASR).
"""

import asyncio
import audioop
import json
import logging
import os
import sys
import time
from typing import Optional

import aiohttp
from aiohttp import web
import discord  # discord.py-self
from discord.ext import voice_recv
import vosk

import config

# Import the main bot's user mapping (parent directory) so we can show
# friendly names instead of Discord display_name fallbacks.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from users import USERS as _USERS
except Exception:
    _USERS = {}


def _name_for(user_id: int, member=None) -> str:
    info = _USERS.get(user_id)
    if info and info.get("name"):
        return info["name"]
    if member is not None:
        return member.display_name
    return f"User {user_id}"


# ---------- DAVE decryption monkey-patch -----------------------------------
# voice_recv decrypts only the outer AEAD layer; the inner Opus payload is
# still DAVE-encrypted in E2EE channels. Because we're logged in as a real
# user, dave_session has the MLS keys to decrypt — but voice_recv doesn't
# know to call dave.decrypt(). Wrap each _decrypt_rtp_* method on
# PacketDecryptor to apply DAVE decryption after AEAD.

from discord.ext.voice_recv.reader import AudioReader, PacketDecryptor

try:
    import davey
except ImportError:
    davey = None

_dave_stats = {"total": 0, "dave_ok": 0, "dave_skip": 0, "dave_fail": 0}

# Opus 20ms mono silence frame — used as fallback when DAVE decryption fails so
# opus_decode produces silence instead of crashing the PacketRouter thread.
_OPUS_SILENCE = b"\xf8\xff\xfe"


def _install_dave_patch():
    _orig_init = AudioReader.__init__

    def _patched_init(self, sink, voice_client, *args, **kwargs):
        _orig_init(self, sink, voice_client, *args, **kwargs)
        # Stash the voice client reference on the decryptor so the wrapped
        # _decrypt_rtp_* method can read dave_session + ssrc_user_map.
        # Upstream AudioReader signature is (self, sink, voice_client, ...).
        self.decryptor._voice_client = voice_client

    AudioReader.__init__ = _patched_init

    def _wrap_method(method_name):
        original = getattr(PacketDecryptor, method_name, None)
        if original is None:
            return

        def wrapped(self, packet):
            raw = original(self, packet)
            _dave_stats["total"] += 1
            n = _dave_stats["total"]

            if davey is None:
                _dave_stats["dave_skip"] += 1
                return raw

            vc = getattr(self, "_voice_client", None)
            if vc is None:
                _dave_stats["dave_skip"] += 1
                return raw

            # In voice_recv's VoiceRecvClient (which subclasses VoiceClient),
            # the active VoiceConnectionState lives at vc._connection, and the
            # dave_session is set on it during reinit_dave_session.
            state = getattr(vc, "_connection", None)
            dave = getattr(state, "dave_session", None) if state else None

            if n == 1:
                log.info(
                    f"[DAVE-DBG] vc_type={type(vc).__name__} "
                    f"state_type={type(state).__name__ if state else None} "
                    f"state_dave_attr={hasattr(state, 'dave_session') if state else None} "
                    f"vc_attrs_with_dave={[a for a in dir(vc) if 'dave' in a.lower()]} "
                    f"state_attrs_with_dave={[a for a in dir(state) if 'dave' in a.lower()] if state else []}"
                )

            if dave is None or not getattr(dave, "ready", False):
                _dave_stats["dave_skip"] += 1
                if n <= 5 or n % 500 == 0:
                    log.info(
                        f"[DAVE-DBG] #{n} dave not ready "
                        f"(dave={dave is not None}, "
                        f"ready={getattr(dave, 'ready', None) if dave else None})"
                    )
                return _OPUS_SILENCE

            ssrc_map = getattr(vc, "_ssrc_to_id", None)
            if not ssrc_map:
                ssrc_map = getattr(vc, "ssrc_user_map", {}) or {}
            uid = ssrc_map.get(packet.ssrc) if ssrc_map else None
            if not uid:
                _dave_stats["dave_skip"] += 1
                if n <= 5 or n % 500 == 0:
                    log.info(
                        f"[DAVE-DBG] #{n} no uid for ssrc={packet.ssrc} "
                        f"(map_size={len(ssrc_map) if ssrc_map else 0})"
                    )
                return _OPUS_SILENCE

            try:
                decrypted = dave.decrypt(uid, davey.MediaType.audio, raw)
                _dave_stats["dave_ok"] += 1
                if n <= 3 or n % 500 == 0:
                    log.info(
                        f"[DAVE-DBG] #{n} dave.decrypt OK uid={uid} "
                        f"in={len(raw)}B out={len(decrypted)}B"
                    )
                return decrypted
            except Exception as e:
                _dave_stats["dave_fail"] += 1
                if n <= 5 or n % 500 == 0:
                    log.info(f"[DAVE-DBG] #{n} dave.decrypt failed: {e}")
                return _OPUS_SILENCE

        setattr(PacketDecryptor, method_name, wrapped)

    for mode in [
        "xsalsa20_poly1305",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305_lite",
        "aead_xchacha20_poly1305_rtpsize",
    ]:
        _wrap_method(f"_decrypt_rtp_{mode}")


def _install_opus_resilience_patch():
    """Stop OpusError from killing the PacketRouter thread.

    When dave.decrypt() fails on a real Opus packet, the bytes we return are
    not a valid Opus frame and opus_decode raises OpusError, which propagates
    up the router thread's run() and kills the listener forever. Wrap the
    decoder to swallow OpusError and produce silence instead.
    """
    from discord.ext.voice_recv import opus as _vr_opus
    from discord.opus import OpusError

    _orig_decode_packet = _vr_opus.PacketDecoder._decode_packet
    _err_count = {"n": 0}

    def safe_decode_packet(self, packet):
        try:
            return _orig_decode_packet(self, packet)
        except OpusError as e:
            _err_count["n"] += 1
            n = _err_count["n"]
            if n <= 3 or n % 500 == 0:
                log.info(f"[OPUS-SAFE] #{n} swallowed OpusError: {e}")
            return packet, b"\x00" * 3840

    _vr_opus.PacketDecoder._decode_packet = safe_decode_packet


# ---------- Logging --------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("userbot")
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.INFO)
# Crank up gateway logging to capture SESSION_DESCRIPTION + MLS handshake.
logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)

_install_dave_patch()
log.info("DAVE decrypt monkey-patch installed.")
_install_opus_resilience_patch()
log.info("Opus decode resilience patch installed.")


# Also wrap reinit_dave_session to confirm it runs and what protocol version
# Discord assigned to this user.
from discord.voice_state import VoiceConnectionState as _VCS

_orig_reinit = _VCS.reinit_dave_session


async def _patched_reinit(self):
    log.info(
        f"[DAVE-INIT] reinit_dave_session called: "
        f"dave_protocol_version={self.dave_protocol_version}"
    )
    await _orig_reinit(self)
    log.info(
        f"[DAVE-INIT] After reinit: dave_session={self.dave_session is not None}, "
        f"ready={getattr(self.dave_session, 'ready', None) if self.dave_session else None}"
    )


_VCS.reinit_dave_session = _patched_reinit


# ---------- VOSK setup -----------------------------------------------------

log.info(f"Loading Spanish VOSK model from {config.MODEL_PATH_ES} ...")
if not os.path.exists(config.MODEL_PATH_ES):
    log.error(f"Spanish model not found at {config.MODEL_PATH_ES}")
    sys.exit(1)
model_es = vosk.Model(config.MODEL_PATH_ES)
log.info("✅ Spanish VOSK model loaded.")


# ---------- Sink: VOSK transcription per speaking user ---------------------


class TranscriberSink(voice_recv.AudioSink):
    """Per-user Spanish transcription sink.

    write() is called once per Opus frame from each speaking SSRC; voice_recv
    decodes to PCM before delivery when wants_opus() is False.
    """

    def __init__(self, client_ref: discord.Client):
        """Initialize the sink and per-user recognizer state.

        Args:
            client_ref: Discord client used to schedule callbacks.
        """
        super().__init__()
        self._client_ref = client_ref
        self.recognizers: dict[int, vosk.KaldiRecognizer] = {}
        self.resample_states: dict[int, object] = {}
        self.packet_count = 0
        self.start_time = time.time()

    def wants_opus(self) -> bool:
        """Return False to receive decoded PCM audio.

        Returns:
            False to request decoded PCM frames.
        """
        return False  # we want decoded PCM

    def cleanup(self) -> None:
        """Release per-user recognizers and log summary.

        Side Effects:
            Clears recognizer state and logs packet counts.
        """
        log.info(f"[VOSK] Sink cleanup. Total packets: {self.packet_count}")
        self.recognizers.clear()
        self.resample_states.clear()

    def write(self, source, data: voice_recv.VoiceData) -> None:
        """Process PCM frames, run Vosk, and dispatch transcripts.

        Args:
            source: Voice source (speaking member).
            data: Voice data with PCM payload.

        Returns:
            None.

        Side Effects:
            Logs transcripts and schedules on_transcript callbacks.
        """
        user_id = getattr(source, "id", None)
        if user_id is None:
            return
        if user_id in config.IGNORE_USER_IDS:
            return
        pcm_data = data.pcm
        if not pcm_data:
            return

        self.packet_count += 1
        if self.packet_count == 1:
            log.info(
                f"[VOSK] First packet received "
                f"(user_id={user_id}, bytes={len(pcm_data)})"
            )
        elif self.packet_count % 500 == 0:
            elapsed = time.time() - self.start_time
            log.info(
                f"[VOSK] {self.packet_count} packets in {elapsed:.1f}s "
                f"({self.packet_count / max(elapsed, 1):.0f} pkts/s)"
            )

        if user_id not in self.recognizers:
            self.recognizers[user_id] = vosk.KaldiRecognizer(model_es, 16000)
            self.resample_states[user_id] = None

        try:
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(
                mono, 2, 1, 48000, 16000, self.resample_states[user_id]
            )
            self.resample_states[user_id] = new_state
            rec = self.recognizers[user_id]
            if rec.AcceptWaveform(data_16k):
                result = json.loads(rec.Result())
                text = result.get("text", "").strip()
                if text:
                    log.info(f"[VOSK][es] user_id={user_id}: {text}")
                    asyncio.run_coroutine_threadsafe(
                        on_transcript(user_id, text), self._client_ref.loop
                    )
        except Exception as e:
            log.exception(f"[VOSK] write error: {e}")


# ---------- Optional downstream forwarding ---------------------------------

_http_session: Optional[aiohttp.ClientSession] = None


async def _get_http() -> aiohttp.ClientSession:
    """Return a cached aiohttp session for HTTP forwarding.

    Returns:
        Shared aiohttp ClientSession instance.
    """
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def on_transcript(user_id: int, text: str):
    """Handle a completed transcription.

    Args:
        user_id: Discord user ID of the speaker.
        text: Final transcription string.

    Returns:
        None.

    Side Effects:
        Posts to a transcript channel and/or forwards to the main bot HTTP API.

    Async:
        This function is a coroutine and must be awaited.
    """
    if config.TRANSCRIPT_CHANNEL_NAME:
        try:
            for guild in client.guilds:
                chan = discord.utils.get(
                    guild.text_channels, name=config.TRANSCRIPT_CHANNEL_NAME
                )
                if chan:
                    member = guild.get_member(user_id)
                    name = _name_for(user_id, member)
                    await chan.send(f"🎙️ **{name}:** {text}")
                    break
        except Exception as e:
            log.warning(f"text-channel post failed: {e}")

    if config.ENABLE_HTTP_FORWARD:
        try:
            session = await _get_http()
            headers = {}
            if config.BOT_API_SECRET:
                headers["X-API-Secret"] = config.BOT_API_SECRET
            await session.post(
                f"{config.BOT_API_BASE}/transcript",
                json={"user_id": str(user_id), "text": text, "language": "es"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            )
        except Exception as e:
            log.warning(f"HTTP forward failed: {e}")


# ---------- Discord client + auto-join logic -------------------------------

client = discord.Client(chunk_guilds_at_startup=False)


def _guild_allowed(guild_id: int) -> bool:
    """Return True if the guild is allowed by the allowlist.

    Args:
        guild_id: Discord guild ID.

    Returns:
        True when allowlisted or no allowlist is configured.
    """
    return config.GUILD_ALLOWLIST is None or guild_id in config.GUILD_ALLOWLIST


def _vc_for_guild(guild: discord.Guild) -> Optional[voice_recv.VoiceRecvClient]:
    """Return the active VoiceRecvClient for a guild if present.

    Args:
        guild: Discord guild instance.

    Returns:
        VoiceRecvClient if connected; otherwise None.
    """
    for vc in client.voice_clients:
        if vc.guild.id == guild.id:
            return vc  # type: ignore[return-value]
    return None


async def _start_listening(vc: voice_recv.VoiceRecvClient):
    """Ensure the sink is attached once the voice client is connected.

    Args:
        vc: Voice client to attach the sink to.

    Async:
        This function is a coroutine and must be awaited.
    """
    if vc.is_listening():
        return
    for _ in range(40):
        if vc.is_connected():
            break
        await asyncio.sleep(0.5)
    else:
        log.warning(f"[VOICE] Timeout waiting for connection in {vc.channel.name}")
        return
    await asyncio.sleep(1.0)
    log.info(f"[VOICE] Starting listener in {vc.channel.name}")
    try:
        vc.listen(TranscriberSink(client))
    except Exception as e:
        log.exception(f"[VOICE] listen() failed: {e}")


async def _join_channel(channel: discord.VoiceChannel):
    """Join or move to a voice channel and start listening.

    Args:
        channel: Voice channel to join.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not _guild_allowed(channel.guild.id):
        return
    existing = _vc_for_guild(channel.guild)
    try:
        if existing:
            if existing.channel.id == channel.id and existing.is_connected():
                vc = existing
            else:
                log.info(f"[VOICE] Reconnecting: {existing.channel.name} → {channel.name}")
                try:
                    if existing.is_listening():
                        existing.stop_listening()
                except Exception:
                    pass
                try:
                    await existing.disconnect(force=True)
                except Exception as e:
                    log.warning(f"[VOICE] disconnect error (ignored): {e}")
                await asyncio.sleep(0.5)
                vc = await channel.connect(
                    cls=voice_recv.VoiceRecvClient, reconnect=True, timeout=20.0
                )
        else:
            log.info(f"[VOICE] Connecting to {channel.name} ({channel.guild.name})")
            vc = await channel.connect(
                cls=voice_recv.VoiceRecvClient, reconnect=True, timeout=20.0
            )
    except Exception as e:
        log.exception(f"[VOICE] Failed to join {channel.name}: {e}")
        return
    await _start_listening(vc)


async def _leave_if_empty(guild: discord.Guild):
    """Disconnect from the guild voice channel if no humans remain.

    Args:
        guild: Discord guild instance.

    Async:
        This function is a coroutine and must be awaited.
    """
    vc = _vc_for_guild(guild)
    if not vc:
        return
    humans = [
        m for m in vc.channel.members
        if not m.bot and m.id != client.user.id
    ]
    if not humans:
        log.info(f"[VOICE] Channel {vc.channel.name} empty — leaving")
        try:
            if vc.is_listening():
                vc.stop_listening()
        except Exception:
            pass
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            log.warning(f"[VOICE] Disconnect error (ignored): {e}")


@client.event
async def on_ready():
    log.info(f"Userbot online as {client.user} (id={client.user.id})")
    await asyncio.sleep(2)
    for guild in client.guilds:
        if not _guild_allowed(guild.id):
            continue
        for channel in guild.voice_channels:
            humans = [
                m for m in channel.members
                if not m.bot and m.id != client.user.id
            ]
            if humans:
                await _join_channel(channel)
                break


@client.event
async def on_voice_state_update(member, before, after):
    if member.id == client.user.id:
        return
    if member.bot or member.id in config.IGNORE_USER_IDS:
        return

    guild = (after.channel or before.channel).guild
    if not _guild_allowed(guild.id):
        return

    if after.channel and (not before.channel or before.channel.id != after.channel.id):
        await _join_channel(after.channel)

    if before.channel and (not after.channel or after.channel.id != before.channel.id):
        await _leave_if_empty(guild)


# ---------- Local relay HTTP server ---------------------------------------
# Lets the main bot ask the userbot to post a message as the real user.
# Used by /indio so the reply appears to come from "el indio" instead of
# the vapls bot. Bound to localhost; secret-gated.

_DISCORD_MSG_LIMIT = 2000


def _split_for_relay(text: str) -> list[str]:
    if not text:
        return []
    if len(text) <= _DISCORD_MSG_LIMIT:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > _DISCORD_MSG_LIMIT:
            if buf:
                chunks.append(buf)
                buf = ""
            while len(line) > _DISCORD_MSG_LIMIT:
                chunks.append(line[:_DISCORD_MSG_LIMIT])
                line = line[_DISCORD_MSG_LIMIT:]
        buf += line
    if buf:
        chunks.append(buf)
    return chunks


async def _relay_say(request: web.Request) -> web.Response:
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        content = str(data["content"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    reply_to_id = data.get("reply_to_message_id")

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not hasattr(channel, "send"):
        return web.json_response({"error": "channel not sendable"}, status=400)

    chunks = _split_for_relay(content)
    if not chunks:
        return web.json_response({"error": "empty content"}, status=400)

    reference = None
    if reply_to_id is not None:
        try:
            reference = discord.MessageReference(
                message_id=int(reply_to_id),
                channel_id=channel_id,
                fail_if_not_exists=False,
            )
        except Exception:
            reference = None

    message_ids: list[int] = []
    try:
        for i, chunk in enumerate(chunks):
            kwargs = {}
            if i == 0 and reference is not None:
                kwargs["reference"] = reference
            msg = await channel.send(chunk, **kwargs)
            message_ids.append(msg.id)
    except Exception as e:
        log.exception("[RELAY] send failed")
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"sent": len(message_ids), "message_ids": message_ids})


async def _start_relay() -> Optional[web.AppRunner]:
    if not config.RELAY_SECRET:
        log.warning("RELAY_SECRET not set — local relay HTTP endpoint disabled.")
        return None
    app = web.Application()
    app.router.add_post("/say", _relay_say)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.RELAY_HOST, port=config.RELAY_PORT)
    await site.start()
    log.info(f"[RELAY] HTTP listening on http://{config.RELAY_HOST}:{config.RELAY_PORT}")
    return runner


async def main():
    """Start the userbot client and clean up HTTP resources on exit.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not config.USER_TOKEN:
        log.error("USER_TOKEN is not set. See .env.example for setup instructions.")
        sys.exit(1)
    relay_runner = await _start_relay()
    try:
        await client.start(config.USER_TOKEN)
    finally:
        if relay_runner is not None:
            try:
                await relay_runner.cleanup()
            except Exception:
                pass
        if _http_session and not _http_session.closed:
            await _http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")

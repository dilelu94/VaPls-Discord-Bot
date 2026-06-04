"""
VaPls userbot: listens to Discord voice channels using a real user account
(so DAVE E2EE works naturally) and transcribes Spanish speech with
faster-whisper. When the wake word "indio" / "che indio" is detected at the
start of a transcript, the pregunta is forwarded to the main bot's /indio
endpoint so the indio persona can reply.

Runs separately from the main Discord bot — the main bot still handles
/play, /soundpad, slash commands, etc. This userbot is voice-input-only.

Library stack: discord.py-self (user-token client) + discord-ext-voice-recv
(voice receive extension) + faster-whisper (CTranslate2-based ASR).
"""

import asyncio
import audioop
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import aiohttp
from aiohttp import web
import discord  # discord.py-self
from discord.ext import voice_recv

import config
import greeting
from transcript_channel import (
    resolve_transcript_channel as _resolve_transcript_channel_impl,
)
from recording import (
    INPUT_SAMPLE_RATE as _REC_INPUT_SAMPLE_RATE,
    INPUT_WIDTH as _REC_INPUT_WIDTH,
    mix_pcm_frames,
    trim_trailing_silence as _trim_trailing_silence,
    pcm_to_ogg_opus,
)

# Import the main bot's user mapping (parent directory) so we can show
# friendly names instead of Discord display_name fallbacks.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from users import USERS as _USERS
except Exception:
    _USERS = {}
import webhookLogger  # parent dir; wired after basicConfig() below


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
            # Return empty PCM so the sink's `if not pcm_data: return` guard
            # drops the packet entirely instead of feeding silence into VOSK,
            # which would otherwise break the recognizer's context.
            return packet, b""

    _vr_opus.PacketDecoder._decode_packet = safe_decode_packet


# ---------- Logging --------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("userbot")

import analytics
import posthog_client

posthog_client.init_observability(service_name="vapls-userbot")

# Webhook forwarding (LOG_WEBHOOK_URL). Installed *after* basicConfig so the
# stdout StreamHandler still gets added — otherwise basicConfig sees our
# handler already attached and skips its own setup, killing journalctl logs.
_webhook_log_handler = webhookLogger.install_from_env("userbot")
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


# ---------- Whisper setup --------------------------------------------------

import re
import threading
from collections import defaultdict, deque

import numpy as np
from faster_whisper import WhisperModel

log.info(
    f"Loading faster-whisper model '{config.WHISPER_MODEL}' "
    f"(compute_type={config.WHISPER_COMPUTE_TYPE}, "
    f"cpu_threads={config.WHISPER_CPU_THREADS}) ..."
)
whisper_model = WhisperModel(
    config.WHISPER_MODEL,
    device="cpu",
    compute_type=config.WHISPER_COMPUTE_TYPE,
    cpu_threads=config.WHISPER_CPU_THREADS,
    num_workers=1,
    download_root=config.WHISPER_CACHE_DIR or None,
)
log.info("✅ Whisper model loaded.")

import unicodedata


def _normalize(s: str) -> str:
    """Lowercase + strip diacritics for substring matching."""
    n = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


# Phonetic variants of "indio" that Whisper tends to produce. Used by
# ``_has_text_beyond_wake_word`` to strip the wake-word out of the
# transcript when deciding whether anything substantive is left to dispatch.
_WAKE_WORD_TOKENS = (
    "indio",
    "indyo",
    "endio",
    "endyo",
    "yndio",
    "yndyo",
    "yendio",
    "yendyo",
    "seinio",
    "seindio",
    "cendio",
    "ceindio",
    "sendio",
    "sendyo",
)


# ---------- Sink: Whisper transcription per speaking user ----------------


class TranscriberSink(voice_recv.AudioSink):
    """Buffer voice frames per user, transcribe with Whisper on utterance end.

    Each speaker gets a rolling buffer that accumulates 16k-mono PCM. We detect
    end-of-utterance with a simple RMS-based VAD: once we've seen voice and
    then SILENCE_FINAL_SECONDS of near-silence, we hand the buffer off to
    Whisper in a background thread.

    Concurrency is capped via a counter that is checked against a dynamic
    limit (5 normally, 3 while the main bot is playing audio). When the limit
    is exceeded, the utterance is dropped with a log entry rather than queued.
    """

    SILENCE_RMS_THRESHOLD = 15
    SILENCE_FINAL_SECONDS = 0.8
    # Skip transcription if total speech accumulated for this user is shorter
    # than this many seconds — usually breath/laughter noise, not words.
    MIN_SPEECH_SECONDS = 0.3
    # Hard upper bound on a single utterance buffer (Whisper handles 30s but
    # we don't want one runaway buffer).
    MAX_UTTERANCE_SECONDS = 20.0

    def __init__(self, client_ref: discord.Client):
        super().__init__()
        self._client_ref = client_ref
        # Per-user mono-16k PCM accumulators.
        self.buffers: dict[int, bytearray] = defaultdict(bytearray)
        self.resample_states: dict[int, object] = {}
        self.last_voice_ts: dict[int, float] = {}
        self.had_voice: dict[int, bool] = defaultdict(bool)
        self.packet_count = 0
        self.start_time = time.time()
        self._active_lock = threading.Lock()
        self._active_count = 0
        # Diagnostic: log RMS for the first packets per user to verify the
        # silence threshold is sensible for the actual mic levels.
        self._rms_seen: dict[int, int] = {}
        self._stopped = False
        self._idle_loop_started = False

    def wants_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        log.info(f"[WHISPER] Sink cleanup. Total packets: {self.packet_count}")
        self._stopped = True
        self.buffers.clear()
        self.resample_states.clear()
        self.last_voice_ts.clear()
        self.had_voice.clear()

    def _start_idle_watcher_once(self) -> None:
        """Lazy-start the idle watcher on first packet.

        voice_recv only calls write() when packets arrive, so the in-write
        silence-detection branch never fires after a speaker stops talking
        (no further packets = no further checks). This background task
        polls every 250ms and finalizes buffers whose last voice frame is
        older than SILENCE_FINAL_SECONDS.
        """
        if self._idle_loop_started:
            return
        self._idle_loop_started = True
        try:
            asyncio.run_coroutine_threadsafe(
                self._idle_watcher(), self._client_ref.loop
            )
        except Exception as e:
            log.exception("[WHISPER] failed to start idle watcher")
            analytics.capture_exception(
                e, properties={"action": "whisper_start_idle_watcher_failed"}
            )

    async def _idle_watcher(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(0.25)
                now = time.time()
                for uid in list(self.last_voice_ts.keys()):
                    last = self.last_voice_ts.get(uid)
                    if (
                        last
                        and self.had_voice.get(uid)
                        and (now - last) > self.SILENCE_FINAL_SECONDS
                    ):
                        self._finalize_user(uid)
            except Exception as e:
                log.exception("[WHISPER] idle watcher error")
                analytics.capture_exception(
                    e, properties={"action": "whisper_idle_watcher_error"}
                )

    def _concurrency_limit(self) -> int:
        """Pick 3 while the main bot plays audio, else 5."""
        return (
            config.MAX_CONCURRENT_WHILE_PLAYING
            if _main_bot_is_playing()
            else config.MAX_CONCURRENT_IDLE
        )

    def write(self, source, data: voice_recv.VoiceData) -> None:
        user_id = getattr(source, "id", None)
        if user_id is None or user_id in config.IGNORE_USER_IDS:
            return
        # Mute non-requesters while a music vote is open in this guild.
        guild_id = getattr(getattr(source, "guild", None), "id", None)
        if not _is_speaker_allowed(guild_id, user_id):
            return
        pcm_data = data.pcm
        if not pcm_data:
            return

        self.packet_count += 1
        if self.packet_count == 1:
            log.info(
                f"[WHISPER] First packet received "
                f"(user_id={user_id}, bytes={len(pcm_data)})"
            )
            self._start_idle_watcher_once()
        elif self.packet_count % 1000 == 0:
            elapsed = time.time() - self.start_time
            log.info(f"[WHISPER] {self.packet_count} packets in {elapsed:.1f}s")

        try:
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            rms = audioop.rms(mono, 2)
            now = time.time()

            # Diagnostic: log RMS for first few packets per user.
            seen = self._rms_seen.get(user_id, 0)
            if seen < 20:
                log.info(
                    f"[WHISPER-RMS] user={user_id} rms={rms} "
                    f"threshold={self.SILENCE_RMS_THRESHOLD}"
                )
                self._rms_seen[user_id] = seen + 1

            if rms < self.SILENCE_RMS_THRESHOLD:
                # Silence frame: check if we should finalize a pending utterance.
                last = self.last_voice_ts.get(user_id)
                if (
                    last
                    and self.had_voice.get(user_id)
                    and (now - last) > self.SILENCE_FINAL_SECONDS
                ):
                    self._finalize_user(user_id)
                return

            # Voice: downsample + append to buffer.
            data_16k, new_state = audioop.ratecv(
                mono, 2, 1, 48000, 16000, self.resample_states.get(user_id)
            )
            self.resample_states[user_id] = new_state
            self.buffers[user_id].extend(data_16k)
            self.last_voice_ts[user_id] = now
            self.had_voice[user_id] = True

            # Force flush if a single utterance grew past the safety cap.
            secs = len(self.buffers[user_id]) / (16000 * 2)
            if secs > self.MAX_UTTERANCE_SECONDS:
                self._finalize_user(user_id)
        except Exception as e:
            log.exception("[WHISPER] write error")
            analytics.capture_exception(e, properties={"action": "whisper_write_error"})

    def _finalize_user(self, user_id: int) -> None:
        """Hand the user's buffer to a background transcription task."""
        buf = bytes(self.buffers.pop(user_id, b""))
        self.resample_states.pop(user_id, None)
        self.last_voice_ts.pop(user_id, None)
        self.had_voice[user_id] = False
        if not buf:
            log.info(f"[WHISPER-FINAL] user={user_id} buf=empty, skip")
            return
        secs = len(buf) / (16000 * 2)
        if secs < self.MIN_SPEECH_SECONDS:
            log.info(
                f"[WHISPER-FINAL] user={user_id} too short ({secs:.2f}s "
                f"< {self.MIN_SPEECH_SECONDS}s), skip"
            )
            return
        log.info(f"[WHISPER-FINAL] user={user_id} finalizing {secs:.2f}s")

        with self._active_lock:
            limit = self._concurrency_limit()
            if self._active_count >= limit:
                log.info(
                    f"[WHISPER] capacity full ({self._active_count}/"
                    f"{limit}); dropping {secs:.1f}s from user {user_id}"
                )
                return
            self._active_count += 1

        asyncio.run_coroutine_threadsafe(
            self._transcribe_and_dispatch(user_id, buf, secs),
            self._client_ref.loop,
        )

    async def _transcribe_and_dispatch(
        self, user_id: int, pcm_16k: bytes, duration: float
    ) -> None:
        try:
            t0 = time.monotonic()
            text = await asyncio.to_thread(_run_whisper, pcm_16k)
            dt = time.monotonic() - t0
            if not text:
                return
            log.info(
                f"[WHISPER][es] user_id={user_id} "
                f"({duration:.1f}s audio, {dt * 1000:.0f}ms transcribe): {text}"
            )
            analytics.capture(
                "whisper_transcription",
                properties={
                    "speaker_id": user_id,
                    "text": text,
                    "duration_seconds": duration,
                    "transcribe_ms": dt * 1000,
                },
            )
            await on_transcript(user_id, text)
        except Exception as e:
            log.exception("[WHISPER] transcribe failed")
            analytics.capture_exception(
                e, properties={"action": "whisper_transcribe_failed"}
            )
        finally:
            with self._active_lock:
                self._active_count -= 1


def _run_whisper(pcm_16k_bytes: bytes) -> str:
    """Run Whisper on s16le 16k mono bytes. Returns concatenated text."""
    audio = np.frombuffer(pcm_16k_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _info = whisper_model.transcribe(
        audio,
        language="es",
        beam_size=1,
        vad_filter=True,
        initial_prompt=(
            "Conversación en español rioplatense con voseo. "
            "Se menciona a veces a 'indio' o 'che indio'."
        ),
        condition_on_previous_text=False,
    )
    return " ".join(s.text.strip() for s in segments if s.text).strip()


# ---------- VOSK wake-word recognizer (gating layer for Whisper) -----------
# VOSK is loaded lazily and only when WAKE_WORD_ENABLED is true. A KaldiRecognizer
# is created per speaker with a restricted grammar so it only ever discriminates
# between the "indio" variants and "[unk]" (anything else). That keeps the CPU
# cost negligible — VOSK runs constantly while Whisper stays idle until the
# wake word fires.

_vosk_model = None  # type: ignore[assignment]
_vosk_load_lock = threading.Lock()

# Grammar fed to KaldiRecognizer. Includes "indio" + filler words + the
# specific command verbs the wake-word matcher cares about ("ponete",
# "reproduci", …). Filler words give VOSK somewhere to map sounds that
# aren't the wake word (without them, VOSK forces vowel-rich gibberish
# into "indio" and floods Whisper with false positives). The command
# verbs need to be in here too, otherwise VOSK collapses them to [unk]
# and the matcher never sees "indio ponete".
# ---------------------------------------------------------------------------
# Sensitivity presets — controlled at runtime via /sensibilidad (main bot)
# or POST /sensibilidad (relay). The preset is in-memory only and resets to
# 1 on userbot restart.
# ---------------------------------------------------------------------------

# Preset 1: all invocation particles + all command verbs (current behavior).
_PRESET_1_PATTERNS: tuple[tuple[str, str], ...] = (
    ("che", "indio"),
    ("que", "indio"),  # VOSK-small often hears "che" as "que"
    ("eh", "indio"),  # seen on speakers where "che" comes out as "eh"
    ("indio", "ponete"),
    ("indio", "poneme"),
    ("indio", "por"),  # VOSK-small collapses "ponete"/"poneme" → "por"
    ("indio", "reproduci"),
    ("indio", "reproduce"),
    ("indio", "tirate"),
    ("indio", "tira"),  # VOSK-small drops trailing "te" → "tira"
    ("indio", "dale"),
)

# Preset 2: only "che indio" as invocation; all command-verb pairs kept.
# Removes ("que","indio") and ("eh","indio") — the dominant false-positive
# driver was "que", a very common Spanish word VOSK-small confuses with "che".
_PRESET_2_PATTERNS: tuple[tuple[str, str], ...] = (
    ("che", "indio"),
    ("indio", "ponete"),
    ("indio", "poneme"),
    ("indio", "por"),
    ("indio", "reproduci"),
    ("indio", "reproduce"),
    ("indio", "tirate"),
    ("indio", "tira"),
    ("indio", "dale"),
)

# Preset 3: enlarged grammar-pool preset — same wake words as preset 1
# (re-enables "que indio"/"eh indio") but with a large decoy filler in VOSK's
# grammar so ambient speech has many buckets to land in instead of collapsing
# into a wake-word phrase. Tune the pool via _PRESET_3_FILLER below.
_PRESET_3_PATTERNS: tuple[tuple[str, str], ...] = _PRESET_1_PATTERNS

# Preset 4: same VOSK gating as preset 2 (only "che indio" + command-verb
# patterns, small grammar pool), but adds a second post-VOSK verification
# layer: after VOSK fires, a dedicated short Whisper pass over the prebuffer
# region must confirm the word "indio" is present. If Whisper can't hear
# "indio", the whole event is discarded. Strict by design.
_PRESET_4_PATTERNS: tuple[tuple[str, str], ...] = _PRESET_2_PATTERNS

_PRESETS: dict[int, tuple[tuple[str, str], ...]] = {
    1: _PRESET_1_PATTERNS,
    2: _PRESET_2_PATTERNS,
    3: _PRESET_3_PATTERNS,
    4: _PRESET_4_PATTERNS,
}

# Active sensitivity preset. Default 2: only "che indio" invokes (the "que"/"eh"
# variants were the dominant false-positive source). Preset 1 is more sensitive
# (adds "que indio"/"eh indio"); preset 3 re-enables those variants but uses a
# large decoy grammar pool to reduce false positives. Preset 4 uses the same
# VOSK gating as preset 2 (only "che indio" + command-verb patterns, small
# grammar pool) and adds a post-VOSK Whisper confirmation layer: after VOSK
# fires, a short Whisper pass over the prebuffer must detect "indio"; if not,
# the event is discarded. In-memory only — resets to this default (4) on
# userbot restart.
_SENSITIVITY_PRESET: int = 4

# Generation counter — incremented by _set_sensitivity so that live per-user
# VOSK recognizers (which embed the old grammar) are detected and rebuilt.
_vosk_grammar_generation: int = 0

# ---- Preset 3 manual-tuning decoy pool ------------------------------------
# Decoys give VOSK somewhere to map ambient speech instead of collapsing it
# into a wake-word. When a phrase keeps mis-firing (see [WAKE]/[VOSK] logs),
# ADD IT HERE by hand so VOSK has a bucket for it.
_PRESET_3_FILLER: list[str] = [
    "che",
    "ey",
    "hola",
    "buenas",
    "dale",
    "vamos",
    "que",
    "qué",
    "como",
    "cómo",
    "cual",
    "cuál",
    "donde",
    "dónde",
    "cuando",
    "cuándo",
    "porque",
    "por qué",
    "si",
    "sí",
    "no",
    "ah",
    "eh",
    "uh",
    "oh",
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "uno",
    "yo",
    "vos",
    "tu",
    "tú",
    "ella",
    "nosotros",
    "ser",
    "estar",
    "tener",
    "hacer",
    "decir",
    "ver",
    "bien",
    "mal",
    "todo",
    "nada",
    "algo",
    "mucho",
    "poco",
    "boludo",
    "loco",
    "posta",
    "ahre",
    "viste",
    "mira",
    "escucha",
    "anda",
    "vení",
]


def _build_vosk_grammar() -> str:
    """Build the restricted JSON grammar VOSK uses for wake-word detection.

    Only includes:
      - Multi-token wake-word phrases (lets VOSK lock onto them directly).
      - The leading particles that pair with "indio" ("che","que","eh","ey","hola").
      - The verbs / collapsed-verb tokens that follow "indio".
      - "[unk]" so anything outside the list still lands somewhere (otherwise
        VOSK forces noise into a wake-word and we get false positives).

    Grammar is built from the active sensitivity preset so that VOSK is
    less likely to collapse noise into phrases that are disabled in that preset.
    Everything else (filler like "boludo", interrogatives, articles, generic
    verbs) was removed: it bloated the language model without contributing to
    any _WAKE_PATTERNS pair. Smaller grammar → VOSK is more decisive on the
    tokens we actually care about.
    """
    preset = _SENSITIVITY_PRESET
    # Base phrase set shared by all presets.
    phrases = [
        # Command-verb wake phrases (always active).
        "indio ponete",
        "indio poneme",
        "indio reproduci",
        "indio reproducí",
        "indio reproduce",
        "indio tirate",
        "indio dale",
        "indio por",  # collapsed "indio ponete/poneme"
        "indio tira",  # collapsed "indio tirate"
        # Third-person mentions ("el/él indio") — kept so VOSK doesn't
        # collapse them into a wake-word; the matcher vetoes them via
        # _WAKE_ANTI_PATTERNS.
        "el indio",
        "él indio",
        # Lone tokens.
        "indio",
        "che",
        "ey",
        "hola",
        "el",
        "él",
        "ponete",
        "poneme",
        "reproduci",
        "reproducí",
        "reproduce",
        "tirate",
        "tira",
        "dale",
        "por",
        "y",
        "i",
        "o",
        "a",
        "si",
        "no",
        "[unk]",
    ]
    # Invocation phrases — added only for presets that include them.
    if preset == 1:
        phrases = [
            "che indio",
            "que indio",
            "eh indio",
            "ey indio",
            "hola indio",
        ] + phrases
        phrases.insert(phrases.index("che"), "que")
        phrases.insert(phrases.index("che") + 1, "eh")
    elif preset == 3:
        # Preset 3: re-enables "que indio"/"eh indio" like preset 1, but adds
        # a large decoy pool so VOSK has many buckets for ambient speech.
        invocations = [
            "che indio",
            "que indio",
            "eh indio",
            "ey indio",
            "hola indio",
        ]
        extra_lone = ["que", "eh"]
        combined = invocations + phrases + extra_lone + _PRESET_3_FILLER
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for token in combined:
            if token not in seen:
                seen.add(token)
                deduped.append(token)
        return json.dumps(deduped)
    else:
        # Presets 2 and 4 share the small grammar pool: only "che indio" as
        # invocation phrase.  Preset 4 adds a post-VOSK Whisper confirmation
        # pass (handled in _transcribe_and_dispatch), so no grammar change needed.
        phrases = ["che indio", "ey indio", "hola indio"] + phrases
    return json.dumps(phrases)


_VOSK_GRAMMAR = _build_vosk_grammar()

# Adjacent token pairs that count as a wake-word hit. We only fire when one
# of these specific patterns appears in the VOSK output — "che indio" for
# invocation, "indio ponete" / "indio reproducí" for commands. Bare "indio"
# and "indio + random word" don't trigger anymore; that "indio + any word"
# loophole produced most of the historical false positives.
# Compared against accent-stripped lowercase tokens (see ``_normalize``).
_WAKE_PATTERNS: tuple[tuple[str, str], ...] = _PRESET_1_PATTERNS

# Patterns that explicitly VETO a match even if some other alternative would
# fire. Useful for third-person mentions ("el indio", "él indio") that sound
# similar to "eh/che indio" — when VOSK's N-best contains the anti-pattern,
# we trust that the speaker was talking ABOUT the indio, not TO it.
# Compared against accent-stripped lowercase tokens (so "él" → "el").
_WAKE_ANTI_PATTERNS: tuple[tuple[str, str], ...] = (("el", "indio"),)


def _load_vosk_model():
    """Idempotently load the VOSK model from disk. Returns None if the model
    path is empty or the load fails — in that case the caller falls back to the
    legacy TranscriberSink so the userbot still works (just without gating)."""
    global _vosk_model
    if _vosk_model is not None:
        return _vosk_model
    with _vosk_load_lock:
        if _vosk_model is not None:
            return _vosk_model
        if not config.VOSK_MODEL_PATH:
            log.warning("[VOSK] VOSK_MODEL_PATH is empty; wake-word disabled.")
            return None
        if not os.path.isdir(config.VOSK_MODEL_PATH):
            log.warning(
                "[VOSK] model dir not found at %s; wake-word disabled.",
                config.VOSK_MODEL_PATH,
            )
            return None
        try:
            import vosk

            vosk.SetLogLevel(-1)  # silence VOSK's own stdout chatter
            log.info(f"Loading VOSK model from {config.VOSK_MODEL_PATH} ...")
            _vosk_model = vosk.Model(config.VOSK_MODEL_PATH)
            log.info("✅ VOSK model loaded.")
            return _vosk_model
        except Exception as e:
            log.exception("[VOSK] failed to load model")
            analytics.capture_exception(
                e, properties={"action": "vosk_load_model_failed"}
            )
            return None


def _new_vosk_recognizer():
    """Create a per-user KaldiRecognizer with the current grammar. Returns
    None if VOSK isn't available so callers can fall back gracefully."""
    model = _load_vosk_model()
    if model is None:
        return None
    try:
        import vosk

        rec = vosk.KaldiRecognizer(model, 16000, _VOSK_GRAMMAR)
        # Tag the recognizer with the grammar generation it was built from.
        rec._grammar_generation = _vosk_grammar_generation
        if config.VOSK_MAX_ALTERNATIVES > 0:
            try:
                rec.SetMaxAlternatives(config.VOSK_MAX_ALTERNATIVES)
            except Exception as e:
                log.exception("[VOSK] SetMaxAlternatives failed")
                analytics.capture_exception(
                    e, properties={"action": "vosk_set_max_alternatives_failed"}
                )
        return rec
    except Exception as e:
        log.exception("[VOSK] failed to create recognizer")
        analytics.capture_exception(
            e, properties={"action": "vosk_create_recognizer_failed"}
        )
        return None


def _set_sensitivity(preset: int) -> None:
    """Switch the VOSK wake-word sensitivity preset at runtime.

    Validates preset is in _PRESETS (1-4), updates the module-level
    ``_SENSITIVITY_PRESET``, rebuilds ``_VOSK_GRAMMAR``, and bumps
    ``_vosk_grammar_generation`` so that live per-user recognizers are
    detected as stale and rebuilt on next use.

    The preset is in-memory only — it resets to the default (2) on userbot restart.
    """
    global _SENSITIVITY_PRESET, _VOSK_GRAMMAR, _vosk_grammar_generation
    if preset not in _PRESETS:
        raise ValueError(
            f"Invalid sensitivity preset {preset!r}; must be 1, 2, 3, or 4."
        )
    _SENSITIVITY_PRESET = preset
    _VOSK_GRAMMAR = _build_vosk_grammar()
    _vosk_grammar_generation += 1
    log.info(
        "[VOSK] sensitivity preset set to %d (grammar generation %d)",
        preset,
        _vosk_grammar_generation,
    )


def _active_wake_patterns() -> tuple[tuple[str, str], ...]:
    """Return the wake-word pattern set for the currently active preset.

    Pure accessor — useful in tests and for the matcher.
    """
    return _PRESETS[_SENSITIVITY_PRESET]


def _text_matches_wake_pattern(text: str) -> bool:
    """True when accent-stripped ``text`` contains one of the active preset's
    wake patterns as an adjacent token pair. Pure function — easy to unit-test
    without loading VOSK."""
    norm = _normalize(text or "")
    tokens = [t for t in norm.split() if t]
    if len(tokens) < 2:
        return False
    pairs = set(zip(tokens, tokens[1:]))
    return any(p in pairs for p in _active_wake_patterns())


def _text_has_anti_pattern(text: str) -> bool:
    """True when ``text`` contains a ``_WAKE_ANTI_PATTERNS`` adjacent pair
    (e.g. ``("el","indio")``). Used to veto a match when VOSK's N-best
    suggests the speaker said "el indio" (third-person), not "che indio"."""
    norm = _normalize(text or "")
    tokens = [t for t in norm.split() if t]
    if len(tokens) < 2:
        return False
    pairs = set(zip(tokens, tokens[1:]))
    return any(p in pairs for p in _WAKE_ANTI_PATTERNS)


def _vosk_heard_wake_word(rec, accepted: bool) -> tuple[bool, Optional[dict]]:
    """Return (True, result) when VOSK finalized a segment matching one of the explicit
    wake-word phrases (``_WAKE_PATTERNS``).

    With ``VOSK_MAX_ALTERNATIVES > 0`` the recognizer emits N-best hypotheses
    (``{"alternatives": [{"text": ..., "confidence": ...}, ...]}``) and we
    gatillamos si **cualquiera** matchea un pattern — covers the case where
    VOSK ranks "indio" #1 but "indio dale" #2. When single-best (legacy),
    only ``{"text": "..."}`` comes in.

    Logs "near-miss" segments (top-1 contains "indio" but no pattern matches)
    so we can see whether VOSK is collapsing the verb or the user phrased
    something unexpected.
    """
    if not accepted:
        return False, None
    try:
        result = json.loads(rec.Result() or "{}")
        if "alternatives" in result:
            candidates = [alt.get("text", "") for alt in result["alternatives"]]
        elif "text" in result:
            candidates = [result["text"]]
        else:
            return False, None
        # If ANY alternative suggests an anti-pattern ("el indio"), the
        # speaker was talking ABOUT the indio, not TO it. Veto the match.
        for text in candidates:
            if text and _text_has_anti_pattern(text):
                log.info(
                    f"[VOSK] vetoed by anti-pattern: {text!r} "
                    f"(top-1 was {candidates[0]!r})"
                )
                return False, None
        for idx, text in enumerate(candidates):
            if text and _text_matches_wake_pattern(text):
                if idx > 0:
                    log.info(
                        f"[VOSK] matched via alternative #{idx + 1}: "
                        f"{text!r} (top-1 was {candidates[0]!r})"
                    )
                if result is not None:
                    result["_matched_text"] = text
                analytics.capture(
                    "vosk_transcription",
                    properties={
                        "text": text,
                        "matched": True,
                        "alternatives": candidates,
                        "top_confidence": result.get("alternatives", [{}])[0].get(
                            "confidence"
                        )
                        if result and "alternatives" in result
                        else None,
                    },
                )
                return True, result
        top1 = candidates[0] if candidates else ""
        if top1 and "indio" in _normalize(top1).split():
            extra = ""
            if len(candidates) > 1:
                extra = f", alts={candidates[1:]!r}"
            log.info(f"[VOSK] near-miss, no pattern matched (text={top1!r}{extra})")
        return False, None
    except Exception as e:
        log.exception("[VOSK] failed to read recognizer state")
        analytics.capture_exception(
            e, properties={"action": "vosk_read_recognizer_state_failed"}
        )
        return False, None


class WakeWordSink(voice_recv.AudioSink):
    """VOSK-gated sink: Whisper only runs after VOSK hears "indio".

    Per speaker we maintain:
      - A VOSK KaldiRecognizer with a restricted grammar so it can only
        recognize "indio" variants (or [unk] for everything else).
      - A circular pre-buffer (last ``WAKE_WORD_PREBUFFER_SECONDS`` of mono
        16k PCM) so when the wake word fires we have the audio leading up
        to it, not just what comes after.
      - A capture buffer that, once triggered, keeps growing until either
        sustained silence or the max-capture timeout closes it.

    Once a capture closes, the full PCM (pre-buffer + capture) is handed off
    to Whisper in a background thread. If Whisper returns text containing a
    wake-word token, ``on_transcript`` is invoked the same way the legacy
    sink does — so the downstream ``askIndio`` pipeline is untouched.
    """

    def __init__(self, client_ref: discord.Client):
        super().__init__()
        self._client_ref = client_ref
        self._stopped = False
        self.packet_count = 0
        self.start_time = time.time()
        # Per-user state.
        self.recognizers: dict[int, Any] = {}
        self.resample_states: dict[int, object] = {}
        # Circular prebuffer of (timestamp, mono16k_chunk). We trim from the
        # front whenever the oldest entry exceeds the prebuffer window AND we
        # reset it whenever we detect sustained silence — that way the buffer
        # only ever contains audio from the CURRENT utterance, not anything
        # the speaker said in a prior sentence. Without this reset, a wake
        # word in the middle of a longer conversation would drag unrelated
        # context into the indio's prompt.
        self.prebuffers: dict[int, deque] = {}
        # Per-user voice-activity tracking: the last timestamp we saw a frame
        # above the silence threshold. Used to decide when to reset the
        # prebuffer and the VOSK recognizer.
        self.last_voice_ts: dict[int, float] = {}
        # Active captures: user_id -> {"buf": bytearray, "started_at": float,
        #                              "last_voice_ts": float}
        self.captures: dict[int, dict] = {}
        self._active_lock = threading.Lock()
        self._active_count = 0
        self._idle_loop_started = False
        # While a wake-word is in flight (capture open OR Whisper still
        # transcribing), pause VOSK feed for every OTHER user. The triggerer
        # keeps feeding into its capture buffer; everyone else is dropped on
        # the floor so we don't burn CPU running per-user recognizers we're
        # not going to act on anyway. Cleared in _transcribe_and_dispatch().
        self._wake_in_progress = False
        self._wake_triggerer_id: Optional[int] = None

    def wants_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        log.info(
            f"[WAKE] Sink cleanup. Total packets: {self.packet_count}, "
            f"active captures: {len(self.captures)}"
        )
        self._stopped = True
        self.recognizers.clear()
        self.resample_states.clear()
        self.prebuffers.clear()
        self.captures.clear()

    # ---- packet ingestion -------------------------------------------------

    def write(self, source, data: voice_recv.VoiceData) -> None:
        user_id = getattr(source, "id", None)
        if user_id is None or user_id in config.IGNORE_USER_IDS:
            return
        # Mute non-requesters while a music vote is open in this guild — the
        # main bot toggles _vote_restrictions via the /restrict_speaker relay
        # endpoint. Sits before any audio work so non-requesters cost zero.
        guild_id = getattr(getattr(source, "guild", None), "id", None)
        if not _is_speaker_allowed(guild_id, user_id):
            return
        pcm_data = data.pcm
        if not pcm_data:
            return
        # Pause processing for everyone except the user that fired the wake
        # word while we're still capturing + transcribing. Saves CPU on the
        # 4-vCPU ARM box when 4-5 people happen to talk simultaneously.
        if self._wake_in_progress and user_id != self._wake_triggerer_id:
            return

        self.packet_count += 1
        if self.packet_count == 1:
            log.info(
                f"[WAKE] First packet received "
                f"(user_id={user_id}, bytes={len(pcm_data)})"
            )
            self._start_idle_watcher_once()
        elif self.packet_count % 1000 == 0:
            elapsed = time.time() - self.start_time
            log.info(
                f"[WAKE] {self.packet_count} packets in {elapsed:.1f}s, "
                f"active_captures={len(self.captures)}"
            )

        try:
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(
                mono, 2, 1, 48000, 16000, self.resample_states.get(user_id)
            )
            self.resample_states[user_id] = new_state
            rms = audioop.rms(data_16k, 2)
            now = time.time()

            is_voice = rms >= TranscriberSink.SILENCE_RMS_THRESHOLD
            if is_voice:
                self.last_voice_ts[user_id] = now
            else:
                # If the speaker has been silent long enough, this is a NEW
                # utterance boundary. Drop prebuffer + reset VOSK so the wake
                # word doesn't drag the previous sentence into the capture.
                self._maybe_reset_on_silence(user_id, now)

            capture = self.captures.get(user_id)
            if capture is not None:
                self._extend_capture(user_id, capture, data_16k, rms, now)
                # While capturing we don't need to keep feeding VOSK — the
                # wake word already fired. Whisper sees the full phrase.
                return

            # Buffer ALL frames (voice + short silences) so the audio stays
            # continuous when concatenated. The silence reset above already
            # guarantees the prebuffer doesn't span a real utterance boundary,
            # so a few intra-phrase pauses are fine and actually help Whisper.
            self._push_prebuffer(user_id, data_16k, now)

            # Idle: feed VOSK and check for wake word.
            rec = self._recognizer_for(user_id)
            if rec is None:
                return
            accepted = rec.AcceptWaveform(data_16k)
            matched, vosk_result = _vosk_heard_wake_word(rec, accepted)
            if matched:
                _matched_text = (
                    vosk_result.get("_matched_text", "") if vosk_result else ""
                )
                log.info(
                    "[WAKE] user=%s VOSK detected wake word=%r, "
                    "starting capture (prebuf_chunks=%d)",
                    user_id,
                    _matched_text,
                    len(self.prebuffers.get(user_id, ())),
                )
                analytics.capture(
                    "wake_word_detected",
                    properties={
                        "speaker_id": user_id,
                        "matched_text": _matched_text,
                        "guild_id": getattr(getattr(source, "guild", None), "id", None),
                    },
                )
                self._wake_triggerer_id = user_id
                self._wake_in_progress = True
                self._start_capture(user_id, now, vosk_result)
                self._schedule_wake_sound(user_id)
                try:
                    rec.Reset()
                except Exception:
                    log.warning("[WAKE] rec.Reset() failed")
        except Exception as e:
            log.exception("[WAKE] write error")
            analytics.capture_exception(e, properties={"action": "wake_write_error"})

    def _maybe_reset_on_silence(self, user_id: int, now: float) -> None:
        """Drop the prebuffer + reset VOSK once the speaker has been silent
        for ``WAKE_WORD_SILENCE_FINAL_SECONDS``. Without this, the prebuffer
        accumulates audio across multiple sentences and the wake-word capture
        ends up containing whatever the speaker said BEFORE the actual
        question — confusing the indio."""
        last = self.last_voice_ts.get(user_id)
        if last is None:
            return
        if now - last < config.WAKE_WORD_SILENCE_FINAL_SECONDS:
            return
        buf = self.prebuffers.get(user_id)
        if buf:
            buf.clear()
        rec = self.recognizers.get(user_id)
        if rec is not None:
            try:
                rec.Reset()
            except Exception:
                log.warning("[VOSK] rec.Reset() failed in silence reset")
        # Push the marker forward so we don't keep resetting every frame.
        self.last_voice_ts[user_id] = now

    # ---- prebuffer / capture helpers -------------------------------------

    def _push_prebuffer(self, user_id: int, chunk: bytes, ts: float) -> None:
        buf = self.prebuffers.get(user_id)
        if buf is None:
            buf = deque()
            self.prebuffers[user_id] = buf
        buf.append((ts, chunk))
        # Evict frames older than the prebuffer window.
        cutoff = ts - config.WAKE_WORD_PREBUFFER_SECONDS
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _recognizer_for(self, user_id: int):
        rec = self.recognizers.get(user_id)
        # Discard recognizer if it was built with a different grammar generation
        # (i.e. the sensitivity preset was switched via /sensibilidad).
        if (
            rec is not None
            and getattr(rec, "_grammar_generation", -1) != _vosk_grammar_generation
        ):
            log.debug(
                "[VOSK] rebuilding stale recognizer for user %s (preset changed)",
                user_id,
            )
            rec = None
            self.recognizers.pop(user_id, None)
        if rec is None:
            rec = _new_vosk_recognizer()
            if rec is not None:
                self.recognizers[user_id] = rec
        return rec

    def _start_capture(
        self, user_id: int, now: float, vosk_result: Optional[dict] = None
    ) -> None:
        # Seed the capture buffer with the prebuffer contents so Whisper sees
        # whatever the user said leading up to and including the wake word.
        seed = bytearray()
        for _ts, chunk in self.prebuffers.get(user_id, ()):  # iterate as-is
            seed.extend(chunk)
        self.captures[user_id] = {
            "buf": seed,
            "started_at": now,
            "last_voice_ts": now,
            "vosk_result": vosk_result,
            "prebuffer_len": len(seed),  # bytes of prebuffer seeded into buf
        }

    def _extend_capture(
        self, user_id: int, capture: dict, chunk: bytes, rms: int, now: float
    ) -> None:
        capture["buf"].extend(chunk)
        if rms >= TranscriberSink.SILENCE_RMS_THRESHOLD:
            capture["last_voice_ts"] = now

        # Close conditions: sustained silence OR max-capture timeout.
        silence_for = now - capture["last_voice_ts"]
        duration = now - capture["started_at"]
        if silence_for > config.WAKE_WORD_SILENCE_FINAL_SECONDS:
            log.info(
                f"[WAKE] user={user_id} closing capture on silence "
                f"({silence_for:.2f}s, dur={duration:.2f}s)"
            )
            self._finalize_capture(user_id)
        elif duration >= config.WAKE_WORD_MAX_CAPTURE_SECONDS:
            log.info(
                f"[WAKE] user={user_id} closing capture on timeout ({duration:.2f}s)"
            )
            self._finalize_capture(user_id)

    def _schedule_wake_sound(self, user_id: int) -> None:
        """Hand off wake-sound playback to the main loop. Sink runs in a
        background thread; play_wake_sound() needs to interact with the
        VoiceClient via the event loop."""
        if not _wake_sound_enabled:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                greeting.play_wake_sound(self._client_ref, user_id=user_id),
                self._client_ref.loop,
            )
        except Exception as e:
            log.exception("[WAKE] failed to schedule wake sound")
            analytics.capture_exception(
                e, properties={"action": "wake_schedule_wake_sound_failed"}
            )

    def _finalize_capture(self, user_id: int) -> None:
        capture = self.captures.pop(user_id, None)
        if not capture:
            self._wake_in_progress = False
            self._wake_triggerer_id = None
            return
        pcm = bytes(capture["buf"])
        if not pcm:
            self._wake_in_progress = False
            self._wake_triggerer_id = None
            return
        secs = len(pcm) / (16000 * 2)
        vosk_result = capture.get("vosk_result")
        prebuffer_len = capture.get("prebuffer_len", 0)
        with self._active_lock:
            limit = (
                config.MAX_CONCURRENT_WHILE_PLAYING
                if _main_bot_is_playing()
                else config.MAX_CONCURRENT_IDLE
            )
            if self._active_count >= limit:
                log.info(
                    f"[WAKE] capacity full ({self._active_count}/"
                    f"{limit}); dropping {secs:.1f}s from user {user_id}"
                )
                self._wake_in_progress = False
                self._wake_triggerer_id = None
                return
            self._active_count += 1
        asyncio.run_coroutine_threadsafe(
            self._transcribe_and_dispatch(
                user_id, pcm, secs, vosk_result, prebuffer_len
            ),
            self._client_ref.loop,
        )

    # ---- idle watcher (closes captures when speaker stops sending) -------

    def _start_idle_watcher_once(self) -> None:
        if self._idle_loop_started:
            return
        self._idle_loop_started = True
        try:
            asyncio.run_coroutine_threadsafe(
                self._idle_watcher(), self._client_ref.loop
            )
        except Exception as e:
            log.exception("[WAKE] failed to start idle watcher")
            analytics.capture_exception(
                e, properties={"action": "wake_start_idle_watcher_failed"}
            )

    async def _idle_watcher(self) -> None:
        """voice_recv stops calling write() once the speaker goes silent, so
        we poll every 250ms to close captures whose last voice frame is older
        than the silence threshold (mirrors TranscriberSink's idle_watcher)."""
        while not self._stopped:
            try:
                await asyncio.sleep(0.25)
                now = time.time()
                for uid in list(self.captures.keys()):
                    cap = self.captures.get(uid)
                    if not cap:
                        continue
                    silence_for = now - cap["last_voice_ts"]
                    duration = now - cap["started_at"]
                    if (
                        silence_for > config.WAKE_WORD_SILENCE_FINAL_SECONDS
                        or duration >= config.WAKE_WORD_MAX_CAPTURE_SECONDS
                    ):
                        log.info(
                            f"[WAKE] idle-watcher closing capture for "
                            f"user={uid} (silence={silence_for:.2f}s, "
                            f"dur={duration:.2f}s)"
                        )
                        self._finalize_capture(uid)
            except Exception as e:
                log.exception("[WAKE] idle watcher error")
                analytics.capture_exception(
                    e, properties={"action": "wake_idle_watcher_error"}
                )

    # ---- Whisper handoff --------------------------------------------------

    async def _transcribe_and_dispatch(
        self,
        user_id: int,
        pcm_16k: bytes,
        duration: float,
        vosk_result: Optional[dict] = None,
        prebuffer_len: int = 0,
    ) -> None:
        try:
            t0 = time.monotonic()
            text = await asyncio.to_thread(_run_whisper, pcm_16k)
            dt = time.monotonic() - t0
            if not text:
                log.info(
                    f"[WAKE] user={user_id} Whisper returned empty "
                    f"({duration:.1f}s audio, {dt * 1000:.0f}ms); skip"
                )
                return

            # Preset 4: confirm "indio" appears in the Whisper transcript
            # using the full audio (prebuffer + capture), not just the
            # prebuffer alone — Whisper is unreliable on short ~1.5s clips.
            if _SENSITIVITY_PRESET == 4 and not _whisper_confirms_indio(text):
                log.info(
                    "[WAKE] user=%s preset4: 'indio' NOT confirmed in transcript (%r); discard",
                    user_id,
                    text,
                )
                analytics.capture(
                    "wake_word_rejected",
                    properties={
                        "speaker_id": user_id,
                        "reason": "preset4_no_indio",
                        "text": text,
                    },
                )
                return

            log.info(
                f"[WAKE][es] user_id={user_id} "
                f"({duration:.1f}s audio, {dt * 1000:.0f}ms): {text}"
            )
            analytics.capture(
                "whisper_transcription",
                properties={
                    "speaker_id": user_id,
                    "text": text,
                    "duration_seconds": duration,
                    "transcribe_ms": dt * 1000,
                    "via_wake_word": True,
                },
            )
            # VOSK already matched a restrictive _WAKE_PATTERNS pair before we
            # got here, so we trust the trigger and forward whatever Whisper
            # transcribed — typically the verb + object ("ponete un tema de
            # Queen"), since the "indio" itself often lands outside the
            # prebuffer window. Only drop when Whisper produced nothing
            # substantive (empty or pure filler).
            if not _has_text_beyond_wake_word(text):
                log.info(f"[WAKE] user={user_id} only wake word / no question; skip")
                return
            await on_transcript(
                user_id, text, via_wake_word=True, vosk_result=vosk_result
            )
        except Exception as e:
            log.exception("[WAKE] transcribe failed")
            analytics.capture_exception(
                e, properties={"action": "wake_transcribe_failed"}
            )
        finally:
            with self._active_lock:
                self._active_count -= 1
            # Re-arm: other users (and this one) can fire the wake word again.
            self._wake_in_progress = False
            self._wake_triggerer_id = None


def _has_text_beyond_wake_word(text: str) -> bool:
    """True if the transcript contains more than just the wake word itself.

    "che indio" → False (no follow-up question).
    "che indio cómo andás" → True.
    """
    if not text:
        return False
    norm = _normalize(text)
    for tok in _WAKE_WORD_TOKENS:
        norm = norm.replace(tok, " ")
    # Strip leftover punctuation and common filler words ("che", "ey", "el").
    cleaned = re.sub(r"[^a-zñü ]+", " ", norm)
    cleaned = re.sub(r"\b(che|ey|el|hey|oye|ehh?)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return bool(cleaned)


def _whisper_confirms_indio(text: str) -> bool:
    """True iff the normalized transcript contains the token "indio".

    Used by preset 4 to verify the prebuffer region actually contains the wake
    word "indio" before committing to a full command transcription.

    Matching rule: the normalized (accent-stripped, lowercased) text must
    contain "indio" as a substring of at least one whitespace-delimited token
    (so "indios," and "indio." also count, but "el_indo" does not).  Empty or
    None input returns False.

    Examples:
        >>> _whisper_confirms_indio("che indio ponete un tema")
        True
        >>> _whisper_confirms_indio("ponete algo indio")
        True
        >>> _whisper_confirms_indio("INDIO")
        True
        >>> _whisper_confirms_indio("indio,")
        True
        >>> _whisper_confirms_indio("el indo")  # typo — no "indio"
        False
        >>> _whisper_confirms_indio("")
        False
    """
    if not text:
        return False
    norm = _normalize(text)
    return any("indio" in tok for tok in norm.split())


# Cached "is the main bot playing audio" check — polled cheaply.
# Runtime toggle for the wake-word confirmation sound ("huh").
# Controlled via POST /toggle_wake_sound relay endpoint. Starts at the
# config default and resets on restart (no persistence).
_wake_sound_enabled = getattr(config, "WAKE_SOUND_ENABLED", True)

_play_state = {"is_playing": False, "checked_at": 0.0}
_play_state_lock = threading.Lock()


def _main_bot_is_playing() -> bool:
    """Best-effort cached check of the main bot's /playing endpoint."""
    with _play_state_lock:
        if time.monotonic() - _play_state["checked_at"] < 1.0:
            return _play_state["is_playing"]
    # Schedule a refresh in the event loop without blocking the audio thread.
    try:
        asyncio.run_coroutine_threadsafe(_refresh_play_state(), client.loop)
    except Exception:
        log.warning("[PLAY-STATE] failed to schedule refresh")
    with _play_state_lock:
        return _play_state["is_playing"]


async def _refresh_play_state() -> None:
    if not config.MAIN_BOT_API_BASE or not config.MAIN_BOT_API_SECRET:
        return
    try:
        session = await _get_http()
        async with session.get(
            f"{config.MAIN_BOT_API_BASE}/playing",
            headers={"X-API-Secret": config.MAIN_BOT_API_SECRET},
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                with _play_state_lock:
                    _play_state["is_playing"] = bool(data.get("is_playing"))
                    _play_state["checked_at"] = time.monotonic()
    except Exception:
        log.warning("[PLAY-STATE] refresh HTTP request failed")


# ---------- Sink: short raw-PCM recorder (mixed across speakers) ----------


def trim_trailing_silence(pcm: bytes) -> bytes:
    """Project wrapper that pulls the threshold from config."""
    return _trim_trailing_silence(pcm, threshold=config.RECORD_RMS_THRESHOLD)


class RecorderSink(voice_recv.AudioSink):
    """Captures up to ``max_seconds`` of mixed PCM across all speakers.

    The sink stays cheap during write() — it only buffers PCM with arrival
    timestamps. The actual mixing into a fixed-length output buffer happens
    when :meth:`finalize` is called (either by the orchestrator after the
    window elapses, or by voice_recv's cleanup hook when the sink is
    detached). ``finalize`` returns the same payload on subsequent calls,
    so it's safe to invoke it from multiple paths.
    """

    def __init__(
        self,
        max_seconds: float,
        ignore_user_ids: Optional[set[int]] = None,
        rms_threshold: Optional[int] = None,
    ):
        """Initialize the recorder.

        Args:
            max_seconds: Hard cap on captured duration. The buffer length
                is always exactly this many seconds, with trailing silence
                where no audio arrived.
            ignore_user_ids: User IDs to drop entirely (e.g. the main bot
                so its own playback doesn't leak into the recording).
            rms_threshold: Optional override for the VAD threshold; default
                is :data:`config.RECORD_RMS_THRESHOLD`.
        """
        super().__init__()
        self.max_seconds = max_seconds
        self.ignore_user_ids = ignore_user_ids or set()
        self.rms_threshold = (
            rms_threshold if rms_threshold is not None else config.RECORD_RMS_THRESHOLD
        )
        self.start_time = time.monotonic()
        self._frames: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self._finalized = False
        self._result: Optional[bytes] = None
        self.had_voice = False
        self.frame_count = 0

    def wants_opus(self) -> bool:
        return False

    def write(self, source, data: voice_recv.VoiceData) -> None:
        if self._finalized:
            return
        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.max_seconds:
            return
        user_id = getattr(source, "id", None)
        if user_id is None or user_id in self.ignore_user_ids:
            return
        pcm = data.pcm
        if not pcm:
            return
        try:
            mono = audioop.tomono(pcm, _REC_INPUT_WIDTH, 0.5, 0.5)
        except Exception as e:
            log.exception("[REC] tomono failed")
            analytics.capture_exception(e, properties={"action": "rec_tomono_failed"})
            return
        try:
            if audioop.rms(mono, _REC_INPUT_WIDTH) >= self.rms_threshold:
                self.had_voice = True
        except Exception:
            log.warning("[REC] rms check failed")
        with self._lock:
            self._frames.append((elapsed, mono))
            self.frame_count += 1

    def finalize(self) -> bytes:
        """Mix collected frames into a single PCM buffer and return it."""
        with self._lock:
            if self._finalized:
                return self._result or b""
            self._finalized = True
            frames = list(self._frames)
        self._result = mix_pcm_frames(frames, self.max_seconds)
        return self._result

    def cleanup(self) -> None:
        """voice_recv calls this when the sink is detached."""
        try:
            self.finalize()
        except Exception as e:
            log.exception("[REC] finalize from cleanup failed")
            analytics.capture_exception(
                e, properties={"action": "rec_finalize_from_cleanup_failed"}
            )


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


def _resolve_transcript_channel():
    """Locate the transcript channel from config.

    Thin wrapper alrededor de ``transcript_channel.resolve_transcript_channel``
    (extraído a un módulo aparte para que sea testeable sin levantar
    discord.py-self).
    """
    return _resolve_transcript_channel_impl(client, config)


async def on_transcript(
    user_id: int,
    text: str,
    *,
    via_wake_word: bool = False,
    vosk_result: Optional[dict] = None,
):
    """Handle a completed transcription: post to transcript channel + optionally
    forward to the main bot and trigger the indio on wake word.

    ``via_wake_word`` is set by WakeWordSink so we can skip the unconditional
    channel post (the user explicitly asked us to stop spamming the transcript
    channel with every utterance). When the wake-word path triggers, we still
    post the transcript so the question is visible in the channel right above
    the indio's reply, but we never post anything that didn't pass the
    wake-word gate.
    """
    should_post = (
        via_wake_word or config.DEBUG_TRANSCRIBE_ALL or not config.WAKE_WORD_ENABLED
    )
    posted_channel_id: Optional[int] = None
    posted_guild_id: Optional[int] = None
    posted_message_id: Optional[int] = None
    speaker_name: Optional[str] = None

    chan = _resolve_transcript_channel()

    if chan is not None and should_post:
        try:
            guild = getattr(chan, "guild", None)
            member = guild.get_member(user_id) if guild else None
            speaker_name = _name_for(user_id, member)
            posted = await chan.send(f"🎙️ **{speaker_name}:** {text}")
            posted_channel_id = chan.id
            posted_guild_id = guild.id if guild else None
            # Capture the message id so the main bot can attach
            # ASR-quality feedback reactions to this transcript
            # (see decifrarVoting.record).
            posted_message_id = getattr(posted, "id", None)
        except Exception as e:
            log.warning(f"text-channel post failed: {e}")

    # Resolve the destination guild/channel even when we skipped the post (so
    # the wake-word path can still dispatch to /indio).
    if posted_channel_id is None and chan is not None:
        guild = getattr(chan, "guild", None)
        member = guild.get_member(user_id) if guild else None
        speaker_name = _name_for(user_id, member)
        posted_channel_id = chan.id
        posted_guild_id = guild.id if guild else None

    # Wake word: when this transcript came from the WakeWordSink (VOSK already
    # matched a restrictive _WAKE_PATTERNS pair upstream), hand the entire raw
    # transcript to the main bot's /indio endpoint. The server prefixes the
    # text with "[voz] " so the indio knows to tolerate ASR errors.
    if via_wake_word and posted_channel_id is not None and posted_guild_id is not None:
        log.info(f"[INDIO-WAKE] user_id={user_id} raw={text!r}")
        asyncio.create_task(
            _dispatch_to_indio(
                guild_id=posted_guild_id,
                channel_id=posted_channel_id,
                pregunta=text,
                speaker_name=speaker_name,
                user_id=user_id,
                transcript_message_id=posted_message_id,
                vosk_result=vosk_result,
            )
        )

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


async def _dispatch_to_indio(
    *,
    guild_id: int,
    channel_id: int,
    pregunta: str,
    speaker_name: Optional[str],
    is_voice: bool = True,
    user_id: int = 0,
    transcript_message_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
    vosk_result: Optional[dict] = None,
    replied_content: Optional[str] = None,
    replied_author: Optional[str] = None,
    attachment_urls: Optional[list[dict]] = None,
) -> None:
    """POST the raw transcript to the main bot's /indio endpoint.

    Voice wake-word callers leave ``is_voice=True`` so the main bot prefixes
    the text with "[voz] " before handing it to the indio (which knows to
    tolerate ASR errors on that marker). Text-chat callers pass
    ``is_voice=False`` because the message is already clean.

    ``user_id`` is the speaker's Discord id; the main bot uses it to key
    pending music choices so only the requester can resolve them.

    ``transcript_message_id`` is the id of the userbot's "🎙️ **Name:** raw"
    message; the main bot may add ASR-quality feedback reactions on it
    (1-in-N sampler in ``decifrarVoting``)."""
    if not config.MAIN_BOT_API_BASE or not config.MAIN_BOT_API_SECRET:
        log.warning("[INDIO-WAKE] MAIN_BOT_API_BASE/SECRET missing, skipping")
        return
    try:
        session = await _get_http()
        payload = {
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "pregunta": pregunta,
            "speaker_name": speaker_name,
            "is_voice": is_voice,
            "user_id": str(user_id),
        }
        if transcript_message_id is not None:
            payload["transcript_message_id"] = str(transcript_message_id)
        if source_message_id is not None:
            payload["source_message_id"] = str(source_message_id)
        if vosk_result is not None:
            payload["vosk_result"] = vosk_result
        if replied_content is not None:
            payload["replied_content"] = replied_content
        if replied_author is not None:
            payload["replied_author"] = replied_author
        if attachment_urls is not None:
            payload["attachment_urls"] = attachment_urls
        async with session.post(
            f"{config.MAIN_BOT_API_BASE}/indio",
            json=payload,
            headers={"X-API-Secret": config.MAIN_BOT_API_SECRET},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.warning(f"[INDIO-WAKE] HTTP {resp.status}: {body[:200]}")
    except Exception as e:
        log.exception("[INDIO-WAKE] dispatch failed")
        analytics.capture_exception(
            e, properties={"action": "indio_wake_dispatch_failed"}
        )


# ---------- Discord client + auto-join logic -------------------------------

# discord.py-self is pinned to v2.1.0 (see userbot/requirements.txt), whose
# Client API takes no `intents` argument. Newer upstream builds both require
# `intents` AND fail user login (HTTP 401) against current Discord, so we stay
# on the pinned version that authenticates.
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


# Per-guild pending idle-leave tasks. The userbot stays in the channel for
# IDLE_LEAVE_SECONDS after the guild goes quiet; any human (re)joining a VC
# of the guild cancels the task before it fires.
_idle_leave_tasks: dict[int, asyncio.Task] = {}


# Per-guild "only-this-speaker is heard" lock used while a music vote is open
# in the main bot. The main bot toggles it via the /restrict_speaker relay
# endpoint: while ``_vote_restrictions[guild_id] == user_id`` the sinks drop
# every other speaker's audio before VOSK / Whisper even runs. Cleared when
# the vote closes. Defence-in-depth lives on the main bot side, so a missing
# or stale entry here only impacts efficiency, not correctness.
_vote_restrictions: dict[int, int] = {}


def _is_speaker_allowed(guild_id: Optional[int], user_id: Optional[int]) -> bool:
    """Return False if a vote-restriction excludes this speaker for the guild.

    Returns True (allow) whenever guild/user ids are missing or no restriction
    is set — i.e. legacy behaviour outside of an active music vote.
    """
    if not guild_id or not user_id:
        return True
    restricted = _vote_restrictions.get(int(guild_id))
    if restricted is None:
        return True
    return int(user_id) == int(restricted)


def _guild_has_humans(guild: discord.Guild) -> bool:
    """Return True if any voice channel of ``guild`` has a non-bot,
    non-self, non-ignored member currently connected.

    Scans every voice channel — not just the one the userbot sits in —
    so we can detect "the guild is empty" vs "someone moved to another VC".
    """
    self_id = client.user.id if client.user else None
    for ch in guild.voice_channels:
        if _channel_has_humans(ch, self_id=self_id):
            return True
    return False


def _channel_has_humans(channel, *, self_id: Optional[int] = None) -> bool:
    """Return True if ``channel`` has any non-bot, non-self, non-ignored,
    non-muted, non-deafened member currently connected. Muted or deafened
    members (self- or server-imposed) are treated as not-really-present —
    the bot shouldn't anchor in a channel where the only humans left are
    silent or can't hear anything."""
    if channel is None:
        return False
    if self_id is None:
        self_id = client.user.id if client.user else None
    for m in channel.members:
        if m.bot:
            continue
        if self_id is not None and m.id == self_id:
            continue
        if m.id in config.IGNORE_USER_IDS:
            continue
        voice = getattr(m, "voice", None)
        if voice is not None and (
            getattr(voice, "self_mute", False)
            or getattr(voice, "mute", False)
            or getattr(voice, "self_deaf", False)
            or getattr(voice, "deaf", False)
        ):
            continue
        return True
    return False


def _should_follow_user(
    current_channel, target_channel, *, self_id: Optional[int] = None
) -> bool:
    """Decide whether the userbot should move to ``target_channel`` when a
    user just joined/switched there.

    Returns False (stay put) when the userbot is already in a different
    channel of the same guild and that channel still has at least one human
    — abandoning the people still there to follow a single mover is wrong.

    Returns True when:
    - The userbot is not in any channel yet (first join).
    - The userbot is already in ``target_channel`` (no-op / re-greet).
    - The userbot's current channel has no other humans (everyone left).
    - The userbot is sitting in the guild's AFK channel. The AFK channel is
      a parking spot for idle users; the bot should never anchor there at
      the cost of ignoring active movers elsewhere.
    """
    if current_channel is None:
        return True
    if target_channel is None:
        return False
    if current_channel.id == target_channel.id:
        return True
    afk = getattr(getattr(current_channel, "guild", None), "afk_channel", None)
    if afk is not None and getattr(afk, "id", None) == current_channel.id:
        return True
    return not _channel_has_humans(current_channel, self_id=self_id)


def _cancel_idle_leave(guild_id: int) -> None:
    """Cancel a pending idle-leave task for the guild (idempotent)."""
    task = _idle_leave_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


async def _idle_leave_after_delay(guild: discord.Guild) -> None:
    """Sleep IDLE_LEAVE_SECONDS, re-check, disconnect if still empty.

    Re-checking inside the callback (instead of at scheduling time) is the
    race-safety net: someone may have joined a millisecond before we woke
    up, in which case the cancellation from ``on_voice_state_update``
    arrives racey-late and we'd otherwise still disconnect.
    """
    try:
        await asyncio.sleep(config.IDLE_LEAVE_SECONDS)
    except asyncio.CancelledError:
        return
    try:
        if _guild_has_humans(guild):
            return
        vc = _vc_for_guild(guild)
        if vc is None:
            return
        log.info(
            f"[VOICE] No humans in {guild.name} for "
            f"{config.IDLE_LEAVE_SECONDS:.0f}s — leaving"
        )
        try:
            if vc.is_listening():
                vc.stop_listening()
        except Exception:
            log.warning("[VOICE] stop_listening during idle leave failed")
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            log.warning(f"[VOICE] Idle disconnect error (ignored): {e}")
    finally:
        _idle_leave_tasks.pop(guild.id, None)


def _schedule_idle_leave(guild: discord.Guild) -> None:
    """Schedule a one-shot idle-leave task, replacing any previous one."""
    _cancel_idle_leave(guild.id)
    _idle_leave_tasks[guild.id] = asyncio.create_task(
        _idle_leave_after_delay(guild),
        name=f"idle-leave-{guild.id}",
    )


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
    sink = _make_voice_sink()
    log.info(
        f"[VOICE] Starting listener in {vc.channel.name} (sink={type(sink).__name__})"
    )
    try:
        vc.listen(sink)
    except Exception as e:
        log.exception(f"[VOICE] listen() failed: {e}")
        analytics.capture_exception(e, properties={"action": "voice_listen_failed"})


def _make_voice_sink() -> voice_recv.AudioSink:
    """Pick the right sink based on config flags.

    Default: WakeWordSink (VOSK-gated Whisper, no spam in the transcript channel).
    DEBUG_TRANSCRIBE_ALL=true or WAKE_WORD_ENABLED=false or VOSK unavailable:
    fall back to the legacy TranscriberSink so the userbot still works.
    """
    if config.WAKE_WORD_ENABLED and not config.DEBUG_TRANSCRIBE_ALL:
        if _load_vosk_model() is not None:
            return WakeWordSink(client)
        log.warning(
            "[VOICE] WAKE_WORD_ENABLED but VOSK unavailable; "
            "falling back to TranscriberSink"
        )
    return TranscriberSink(client)


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
                log.info(
                    f"[VOICE] Reconnecting: {existing.channel.name} → {channel.name}"
                )
                try:
                    if existing.is_listening():
                        existing.stop_listening()
                except Exception:
                    log.warning("[VOICE] stop_listening during reconnect failed")
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
        analytics.capture_exception(e, properties={"action": "voice_join_failed"})
        return
    await _start_listening(vc)


async def _leave_if_empty(guild: discord.Guild):
    """Schedule an idle-leave if no humans remain in any VC of the guild.

    Instead of disconnecting immediately, give people IDLE_LEAVE_SECONDS to
    come back or move between channels. If anyone shows up in the meantime,
    ``on_voice_state_update`` will cancel the pending task.
    """
    vc = _vc_for_guild(guild)
    if vc is None:
        _cancel_idle_leave(guild.id)
        return
    if _guild_has_humans(guild):
        _cancel_idle_leave(guild.id)
        return
    log.info(
        f"[VOICE] {guild.name} empty — scheduling leave in "
        f"{config.IDLE_LEAVE_SECONDS:.0f}s"
    )
    _schedule_idle_leave(guild)


@client.event
async def on_ready():
    log.info(f"Userbot online as {client.user} (id={client.user.id})")
    if not config.VAPLS_BOT_ID:
        # Without this id, _pick_vapls_command can't disambiguate /play and
        # /soundpad from other bots in the guild and every relay invocation
        # silently 404s. Surface it loudly so operators don't spend hours
        # wondering why the indio's music never plays.
        log.error(
            "VAPLS_BOT_ID is unset (0) — userbot relay /invoke_* endpoints "
            "will not be able to identify the VaPls bot's slash commands; "
            "indio playback will not work until this is configured.",
        )
    if _webhook_log_handler is not None:
        _webhook_log_handler.start(asyncio.get_running_loop())
    await asyncio.sleep(2)
    for guild in client.guilds:
        if not _guild_allowed(guild.id):
            continue
        for channel in guild.voice_channels:
            humans = [
                m for m in channel.members if not m.bot and m.id != client.user.id
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
        _cancel_idle_leave(guild.id)
        # Don't follow the moving user if the userbot is already sitting in a
        # different channel of this guild that still has humans. Following
        # would abandon the people still in the original channel.
        current_vc = _vc_for_guild(guild)
        current_channel = current_vc.channel if current_vc is not None else None
        if not _should_follow_user(current_channel, after.channel):
            log.info(
                "[VOICE] staying in %s — not following %s to %s "
                "(current channel still has humans)",
                current_channel.name,
                member.display_name,
                after.channel.name,
            )
        else:
            await _join_channel(after.channel)
            # After the userbot is in the channel, play the per-user greeting
            # (only for users with an explicit `greeting` in users.py — no default).
            try:
                vc = _vc_for_guild(guild)
                if vc is not None and vc.channel.id == after.channel.id:
                    asyncio.create_task(
                        greeting.play_user_greeting(
                            vc,
                            user_id=member.id,
                            channel_id=after.channel.id,
                        )
                    )
            except Exception as e:
                log.exception("[GREETING] schedule failed")
                analytics.capture_exception(
                    e, properties={"action": "greeting_schedule_failed"}
                )

    if before.channel and (not after.channel or after.channel.id != before.channel.id):
        await _leave_if_empty(guild)


# ---------- Auto-reply when someone says "indio" in chat -------------------

import re as _re

_INDIO_TEXT_WAKE_RE = _re.compile(r"\bindio\b", _re.IGNORECASE)
_INDIO_AUTO_COOLDOWN: dict[int, float] = {}  # channel_id -> last fired ts
_INDIO_AUTO_HOURLY: dict[int, list[float]] = {}  # guild_id -> recent fire ts


def _autoreply_rate_ok(guild_id: int, channel_id: int) -> bool:
    """Return True if we may fire an auto-reply for this guild+channel right
    now. Enforces a short per-channel cooldown and a per-guild hourly cap so the
    Gemini free tier doesn't get hammered by chatty conversations."""
    now = time.time()
    last_fired = _INDIO_AUTO_COOLDOWN.get(channel_id, 0.0)
    if now - last_fired < config.INDIO_AUTO_REPLY_COOLDOWN_SEC:
        return False
    window_start = now - 3600
    hits = _INDIO_AUTO_HOURLY.setdefault(guild_id, [])
    # GC old entries before checking the cap.
    while hits and hits[0] < window_start:
        hits.pop(0)
    if len(hits) >= config.INDIO_AUTO_REPLY_GUILD_HOURLY_CAP:
        return False
    return True


def _autoreply_mark_fired(guild_id: int, channel_id: int) -> None:
    now = time.time()
    _INDIO_AUTO_COOLDOWN[channel_id] = now
    _INDIO_AUTO_HOURLY.setdefault(guild_id, []).append(now)


_GEMINI_KEY_DM_RE = _re.compile(r"\b(?:AIza[\w-]{20,80}|AQ\.[A-Za-z0-9_\-]{20,120})")


async def _handle_gemini_key_dm(message) -> bool:
    """If ``message`` is a DM that contains Gemini-shaped API keys, POST the
    raw text to the main bot's /gemini-key endpoint and reply with a short
    confirmation. Returns True when handled (so the caller skips the
    auto-reply path), False otherwise."""
    guild = getattr(message, "guild", None)
    if guild is not None:
        return False
    text = message.content or ""
    if not _GEMINI_KEY_DM_RE.search(text):
        return False
    if not config.MAIN_BOT_API_BASE or not config.MAIN_BOT_API_SECRET:
        log.warning("[GEMINI-KEY-DM] main bot API not configured, can't forward")
        return True  # handled (silenciamos al user, no le pedimos retry)
    owner_id = str(message.author.id)
    owner_name = getattr(message.author, "display_name", None) or getattr(
        message.author, "name", "unknown"
    )
    payload = {
        "text": text,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "source": "dm:userbot",
    }
    try:
        session = await _get_http()
        async with session.post(
            f"{config.MAIN_BOT_API_BASE}/gemini-key",
            json=payload,
            headers={"X-API-Secret": config.MAIN_BOT_API_SECRET},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.warning(f"[GEMINI-KEY-DM] HTTP {resp.status}: {body[:200]}")
                return True
            data = await resp.json(content_type=None)
    except Exception as e:
        log.exception("[GEMINI-KEY-DM] forward failed")
        analytics.capture_exception(
            e, properties={"action": "gemini_key_dm_forward_failed"}
        )
        return True
    results = (data or {}).get("results") or []
    added = sum(1 for r in results if r.get("ok"))
    dupes = sum(
        1 for r in results if not r.get("ok") and r.get("reason") == "already in pool"
    )
    failed = len(results) - added - dupes
    lines: list[str] = []
    if added:
        lines.append(f"✅ Sumé {added} key(s) al pool. ¡Gracias {owner_name}!")
    if dupes:
        lines.append(f"ℹ️ {dupes} key(s) ya estaban cargadas.")
    if failed:
        lines.append(f"❌ {failed} key(s) no las pude sumar (formato inválido?).")
    if lines:
        try:
            await message.channel.send("\n".join(lines))
        except Exception as e:
            log.exception("[GEMINI-KEY-DM] reply failed")
            analytics.capture_exception(
                e, properties={"action": "gemini_key_dm_reply_failed"}
            )
    log.info(
        f"[GEMINI-KEY-DM] from {owner_name} ({owner_id}): "
        f"added={added} dupes={dupes} failed={failed}"
    )
    return True


@client.event
async def on_message(message):
    """Auto-reply trigger: when any human (not the userbot, not other bots,
    not a slash command typed as text) writes a message that contains the
    word "indio", forward the full text to the main bot's /indio endpoint
    so the persona answers in the same channel.

    Also: DMs containing Gemini API keys get forwarded to the main bot's
    /gemini-key endpoint, with no auto-reply firing."""
    if message.author is None:
        return
    if message.author.id == client.user.id:
        return
    if getattr(message.author, "bot", False):
        return
    if message.author.id in config.IGNORE_USER_IDS:
        return
    # DM con keys de Gemini: lo procesamos y cortamos.
    if await _handle_gemini_key_dm(message):
        return
    if not config.INDIO_AUTO_REPLY_ENABLED:
        return
    content = (message.content or "").strip()
    if not content:
        return
    # Skip slash-command-shaped messages.
    if content.startswith("/"):
        return
    if not _INDIO_TEXT_WAKE_RE.search(content):
        return
    guild = getattr(message, "guild", None)
    if guild is None:
        return
    if not _guild_allowed(guild.id):
        return
    channel_id = getattr(message.channel, "id", None)
    if channel_id is None:
        return
    if not _autoreply_rate_ok(guild.id, channel_id):
        log.info(f"[INDIO-AUTO] rate-limited (channel={channel_id})")
        return
    _autoreply_mark_fired(guild.id, channel_id)
    speaker_name = _name_for(message.author.id, message.author)
    log.info(
        f"[INDIO-AUTO] match in #{getattr(message.channel, 'name', '?')}"
        f" by {speaker_name}: {content[:100]!r}"
    )
    # ---- Extract replied-to message context for the indio ----
    replied_content = None
    replied_author = None
    attachment_urls = None
    ref = message.reference
    if ref is not None:
        ref_msg = getattr(message, "referenced_message", None)
        if ref_msg is None and ref.message_id is not None:
            try:
                ref_msg = await message.channel.fetch_message(ref.message_id)
            except Exception:
                log.warning("[AUTOREPLY] fetch_message failed for autoreply reference")
        if (
            ref_msg is not None
            and ref_msg.author is not None
            and ref_msg.author.id != client.user.id
        ):
            replied_content = (ref_msg.content or "")[:500]
            replied_author = _name_for(ref_msg.author.id, ref_msg.author)
            images = [
                a
                for a in (ref_msg.attachments or [])
                if a.content_type and a.content_type.startswith("image/")
            ][:3]
            if images:
                attachment_urls = [
                    {"url": a.url, "mime_type": a.content_type, "filename": a.filename}
                    for a in images
                ]
            else:
                videos = [
                    a
                    for a in (ref_msg.attachments or [])
                    if a.content_type and a.content_type.startswith("video/")
                ]
                if videos:
                    attachment_urls = [
                        {
                            "url": a.url,
                            "mime_type": a.content_type,
                            "filename": a.filename,
                        }
                        for a in videos[:1]
                    ]
    asyncio.create_task(
        _dispatch_to_indio(
            guild_id=guild.id,
            channel_id=channel_id,
            pregunta=content,
            speaker_name=speaker_name,
            is_voice=False,
            user_id=message.author.id,
            source_message_id=message.id,
            replied_content=replied_content,
            replied_author=replied_author,
            attachment_urls=attachment_urls,
        )
    )


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
        analytics.capture_exception(e, properties={"action": "relay_send_failed"})
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"sent": len(message_ids), "message_ids": message_ids})


async def _relay_edit(request: web.Request) -> web.Response:
    """Edit a message previously posted by the userbot.

    We can only edit messages we own (Discord rule) — the transcript line
    posted by the userbot at speak-time qualifies; anything else 403s.

    Body: ``{channel_id, message_id, content}``. Replaces the message content
    entirely with ``content``.
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        message_id = int(data["message_id"])
        content = str(data["content"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not hasattr(channel, "fetch_message"):
        return web.json_response({"error": "channel not messageable"}, status=400)
    try:
        msg = await channel.fetch_message(message_id)
    except Exception as e:
        return web.json_response({"error": f"message not found: {e}"}, status=404)
    try:
        await msg.edit(content=content[:1990])
    except discord.Forbidden:
        return web.json_response({"error": "cannot edit (not author)"}, status=403)
    except Exception as e:
        log.exception("[RELAY] edit failed")
        analytics.capture_exception(e, properties={"action": "relay_edit_failed"})
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "message_id": message_id})


# ---------- Voice recording orchestration ---------------------------------
# Triggered by the main bot after a Telegram audio is played. We move the
# userbot into the same voice channel, capture up to N seconds, and POST the
# encoded result to a callback URL (typically a Telegram bridge endpoint
# that wires it back into the original chat as a voice reply).

_recording_locks: dict[int, asyncio.Lock] = {}


def _record_lock_for(guild_id: int) -> asyncio.Lock:
    lock = _recording_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _recording_locks[guild_id] = lock
    return lock


async def _run_recording(
    channel: discord.VoiceChannel,
    duration: float,
    callback_url: Optional[str],
    callback_secret: Optional[str],
    callback_metadata: Any,
) -> None:
    """Move into ``channel``, capture audio, then POST it to ``callback_url``.

    The transcriber sink is detached for the duration of the recording and
    re-attached afterwards so normal Spanish transcription resumes. If no
    speech was detected (or no callback URL was provided), nothing is sent.
    """
    guild = channel.guild
    lock = _record_lock_for(guild.id)
    if lock.locked():
        log.info(f"[REC] another recording in flight for {guild.name}; skipping")
        return
    async with lock:
        try:
            await _join_channel(channel)
        except Exception as e:
            log.exception("[REC] join failed")
            analytics.capture_exception(e, properties={"action": "rec_join_failed"})
            return
        vc = _vc_for_guild(guild)
        if vc is None or not vc.is_connected():
            log.warning("[REC] not connected after join; abort")
            return

        # Detach the transcriber so its sink stops receiving frames.
        try:
            if vc.is_listening():
                vc.stop_listening()
        except Exception as e:
            log.exception("[REC] stop_listening failed")
            analytics.capture_exception(
                e, properties={"action": "rec_stop_listening_failed"}
            )

        sink = RecorderSink(
            max_seconds=duration,
            ignore_user_ids=set(config.IGNORE_USER_IDS) | {client.user.id},
        )
        try:
            vc.listen(sink)
        except Exception as e:
            log.exception("[REC] listen(RecorderSink) failed")
            analytics.capture_exception(
                e, properties={"action": "rec_listen_recordersink_failed"}
            )
            # Best-effort: restart transcription and bail.
            await _start_listening(vc)
            return

        log.info(f"[REC] capturing up to {duration:.1f}s in {channel.name}")
        try:
            await asyncio.sleep(duration)
        finally:
            try:
                if vc.is_listening():
                    vc.stop_listening()
            except Exception as e:
                log.exception("[REC] post-record stop_listening failed")
                analytics.capture_exception(
                    e, properties={"action": "rec_post_record_stop_listening_failed"}
                )

        pcm = sink.finalize()
        had_voice = sink.had_voice
        log.info(
            f"[REC] done frames={sink.frame_count} had_voice={had_voice} "
            f"raw_bytes={len(pcm)}"
        )

        # Resume normal transcription before doing slower work below.
        try:
            await _start_listening(vc)
        except Exception as e:
            log.exception("[REC] resume transcriber failed")
            analytics.capture_exception(
                e, properties={"action": "rec_resume_transcriber_failed"}
            )

        if not had_voice:
            log.info("[REC] no voice activity detected; skipping callback")
            return
        if not callback_url:
            log.info("[REC] no callback_url provided; discarding recording")
            return

        trimmed = trim_trailing_silence(pcm)
        min_bytes = int(
            config.RECORD_MIN_SECONDS * _REC_INPUT_SAMPLE_RATE * _REC_INPUT_WIDTH
        )
        if len(trimmed) < min_bytes:
            log.info(
                f"[REC] trimmed audio {len(trimmed)}B < min {min_bytes}B; "
                f"skipping callback"
            )
            return

        try:
            ogg = await pcm_to_ogg_opus(trimmed)
        except Exception as e:
            log.exception("[REC] OGG/Opus encode failed; skipping callback")
            analytics.capture_exception(
                e, properties={"action": "rec_ogg_opus_encode_failed"}
            )
            return

        try:
            session = await _get_http()
            form = aiohttp.FormData()
            if callback_metadata is not None:
                if not isinstance(callback_metadata, str):
                    callback_metadata = json.dumps(callback_metadata)
                form.add_field("metadata", callback_metadata)
            form.add_field("guild_id", str(guild.id))
            form.add_field("channel_id", str(channel.id))
            form.add_field(
                "duration_seconds",
                f"{len(trimmed) / (_REC_INPUT_SAMPLE_RATE * _REC_INPUT_WIDTH):.2f}",
            )
            form.add_field("file", ogg, filename="reply.ogg", content_type="audio/ogg")
            headers = {}
            if callback_secret:
                headers["X-API-Secret"] = callback_secret
            async with session.post(
                callback_url,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(f"[REC] callback HTTP {resp.status}: {body[:200]}")
                else:
                    log.info(f"[REC] callback delivered ({len(ogg)}B)")
        except Exception as e:
            log.exception("[REC] callback POST failed")
            analytics.capture_exception(
                e, properties={"action": "rec_callback_post_failed"}
            )


async def _relay_record(request: web.Request) -> web.Response:
    """Trigger a voice recording in a specific channel.

    Body (JSON):
      - guild_id (required)
      - channel_id (required) — voice channel to record from
      - duration (optional) — seconds; clamped to [1, RECORD_MAX_SECONDS]
      - callback_url (optional) — where to POST the encoded OGG file
      - callback_secret (optional) — X-API-Secret value for that POST
      - callback_metadata (optional) — JSON object passed through verbatim
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)

    duration_raw = data.get("duration", config.RECORD_MAX_SECONDS)
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid duration"}, status=400)
    duration = max(1.0, min(duration, config.RECORD_MAX_SECONDS))

    callback_url = data.get("callback_url") or None
    callback_secret = data.get("callback_secret") or None
    callback_metadata = data.get("callback_metadata")

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    guild = client.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild not found"}, status=404)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return web.json_response(
            {"error": "channel not found or not voice"}, status=404
        )

    asyncio.create_task(
        _run_recording(
            channel, duration, callback_url, callback_secret, callback_metadata
        )
    )
    return web.json_response({"started": True, "duration": duration})


async def _relay_members(request: web.Request) -> web.Response:
    """List every member of a guild as seen by the user account. Lets the
    main bot enumerate users without needing the privileged "members" intent
    enabled on its bot token."""
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        guild_id = int(request.query["guild_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing or invalid guild_id"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)
    guild = client.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild not found"}, status=404)

    role_name = (request.query.get("role_name") or "").strip().lower()

    members = list(guild.members)
    # If the cache looks suspiciously empty, ask the gateway for the full list.
    if len(members) < 5:
        try:
            members = []
            async for m in guild.fetch_members(limit=None):
                members.append(m)
        except Exception as e:
            log.warning(f"[RELAY] fetch_members fallback failed: {e}")
            members = list(guild.members)

    if role_name:
        members = [
            m
            for m in members
            if any((r.name or "").lower() == role_name for r in (m.roles or []))
        ]

    # The userbot is itself a Discord user account, but for any "roster of
    # friends" use case the relay should not return the userbot's own entry.
    self_id = getattr(getattr(client, "user", None), "id", None)
    if self_id is not None:
        members = [m for m in members if m.id != self_id]

    payload = [
        {
            "id": str(m.id),
            "name": getattr(m, "name", None) or "",
            "global_name": getattr(m, "global_name", None) or "",
            "display_name": getattr(m, "display_name", None) or "",
            "is_bot": bool(getattr(m, "bot", False)),
            "roles": [
                r.name for r in (m.roles or []) if r.name and r.name != "@everyone"
            ],
        }
        for m in members
    ]
    return web.json_response(
        {
            "guild_id": guild_id,
            "role_filter": role_name or None,
            "members": payload,
        }
    )


def _command_owner_id(cmd) -> Optional[int]:
    """Extract the owning bot's user/application id from a SlashCommand.
    discord.py-self surfaces this as either ``application_id`` directly or
    ``application.id`` depending on the cached path."""
    app_id = getattr(cmd, "application_id", None)
    if app_id is not None:
        try:
            return int(app_id)
        except (TypeError, ValueError):
            log.warning("[RELAY] failed to parse application_id")
    app = getattr(cmd, "application", None)
    if app is not None:
        app_id = getattr(app, "id", None)
        if app_id is not None:
            try:
                return int(app_id)
            except (TypeError, ValueError):
                log.warning("[RELAY] failed to parse application.id")
    return None


def _pick_vapls_command(cmds, name: str):
    """Pick the slash command named ``name`` that belongs to the VaPls bot.
    Other bots in the guild (e.g. legacy music bots) may expose commands
    with the same name; filtering by owning bot id keeps us from invoking
    the wrong one. Returns ``None`` if no command owned by VaPls is found —
    we'd rather fail loudly than invoke another bot's slash command by
    accident."""
    matches_by_name = [c for c in cmds if getattr(c, "name", None) == name]
    for c in matches_by_name:
        if _command_owner_id(c) == config.VAPLS_BOT_ID:
            return c
    if matches_by_name:
        # log.error (not warning): without a matching VaPls command id every
        # relay invocation is doomed to 404 and the operator needs to see
        # this loudly in journalctl, not bury it among the routine warnings.
        log.error(
            "[RELAY] /%s exists in channel but no instance is owned by VaPls "
            "bot %s (candidates: %s)",
            name,
            config.VAPLS_BOT_ID,
            [_command_owner_id(c) for c in matches_by_name],
        )
    return None


async def _resolve_slash_commands(channel, name: str, timeout: float):
    """Fetch slash commands matching ``name`` in ``channel`` with a hard
    timeout.

    Usa ``Messageable.application_commands()`` (discord.py-self 2.1+) que
    trae los comandos con sus options completas — ``slash_commands(query=…)``
    (deprecated) devolvía un AsyncIterator filtrado server-side pero sin
    el detalle de options, lo cual hacía que ``SlashCommand._parse_kwargs``
    descartara el ``query=...`` que le pasamos y Discord rechazara la
    invocación con 50035 "Invalid Form Body". Filtramos client-side por
    name después del fetch.

    Cae a ``slash_commands`` solo si la nueva API no está disponible
    (versiones viejas del package). discord.py-self puede stallear bajo
    rate-limit o un fetch lento de cache, así que envolvemos en
    ``asyncio.wait_for``: TimeoutError sube al caller que decide cancelar.
    """

    async def _fetch():
        if hasattr(channel, "application_commands"):
            all_cmds = await channel.application_commands()
            return [c for c in all_cmds if getattr(c, "name", None) == name]
        # Fallback para versiones anteriores a 2.1
        cmds_iter = channel.slash_commands(query=name)
        if hasattr(cmds_iter, "__aiter__"):
            return [c async for c in cmds_iter]
        return await cmds_iter

    return await asyncio.wait_for(_fetch(), timeout=timeout)


async def _relay_invoke_play(request: web.Request) -> web.Response:
    """Ask the userbot (a real Discord user account) to invoke VaPls's /play
    slash command in a given text channel, using the query string supplied by
    the indio persona. Because the call originates from a real user account,
    Discord shows the full "Indio used /play" interaction flow with VaPls's
    download-progress message, queue controls, etc."""
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        query = str(data["query"]).strip()
    except Exception as e:
        log.warning("[RELAY-PLAY] rejected invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)
    if not query:
        return web.json_response({"error": "empty query"}, status=400)
    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not (
        hasattr(channel, "application_commands") or hasattr(channel, "slash_commands")
    ):
        return web.json_response(
            {"error": "channel has no slash command API"}, status=400
        )

    try:
        cmds = await _resolve_slash_commands(
            channel,
            "play",
            timeout=config.INDIO_RELAY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "[RELAY-PLAY] slash_commands() timed out for channel %s", channel_id
        )
        return web.json_response({"error": "slash_commands() timed out"}, status=504)
    except Exception as e:
        log.exception("[RELAY-PLAY] slash_commands() failed")
        analytics.capture_exception(
            e, properties={"action": "relay_play_slash_commands_failed"}
        )
        return web.json_response({"error": f"slash_commands() failed: {e}"}, status=500)

    play_cmd = _pick_vapls_command(cmds, "play")
    if play_cmd is None:
        log.warning(
            "[RELAY-PLAY] VaPls /play not found in channel %s (saw %d candidates)",
            channel_id,
            len(cmds),
        )
        return web.json_response(
            {"error": "play command not found in channel"}, status=404
        )

    try:
        await play_cmd(query=query)
        log.info(f"[RELAY-PLAY] invoked /play query={query!r} in channel={channel_id}")
        return web.json_response({"invoked": True, "query": query})
    except discord.HTTPException as e:
        # Discord rate-limit (429) vs anything else: signal 429 distinctly
        # so the caller can decide on backoff. Other HTTPExceptions get
        # the same 500 treatment as before.
        if getattr(e, "status", None) == 429:
            log.warning("[RELAY-PLAY] Discord rate-limited /play invocation: %s", e)
            return web.json_response({"error": f"rate-limited: {e}"}, status=429)
        log.exception("[RELAY-PLAY] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_play_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)
    except Exception as e:
        log.exception("[RELAY-PLAY] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_play_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)


async def _relay_invoke_soundpad(request: web.Request) -> web.Response:
    """Ask the userbot to invoke VaPls's /soundpad slash command with a
    ``query`` argument so the clip plays under the real user account.
    Mirrors :func:`_relay_invoke_play` so the indio can choose a clip and
    have it look like a real slash invocation in the channel."""
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        query = str(data["query"]).strip()
    except Exception as e:
        log.warning("[RELAY-SOUNDPAD] rejected invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)
    if not query:
        return web.json_response({"error": "empty query"}, status=400)
    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not (
        hasattr(channel, "application_commands") or hasattr(channel, "slash_commands")
    ):
        return web.json_response(
            {"error": "channel has no slash command API"}, status=400
        )

    try:
        cmds = await _resolve_slash_commands(
            channel,
            "soundpad",
            timeout=config.INDIO_RELAY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "[RELAY-SOUNDPAD] slash_commands() timed out for channel %s", channel_id
        )
        return web.json_response({"error": "slash_commands() timed out"}, status=504)
    except Exception as e:
        log.exception("[RELAY-SOUNDPAD] slash_commands() failed")
        analytics.capture_exception(
            e, properties={"action": "relay_soundpad_slash_commands_failed"}
        )
        return web.json_response({"error": f"slash_commands() failed: {e}"}, status=500)

    sp_cmd = _pick_vapls_command(cmds, "soundpad")
    if sp_cmd is None:
        log.warning(
            "[RELAY-SOUNDPAD] VaPls /soundpad not found in channel %s (saw %d candidates)",
            channel_id,
            len(cmds),
        )
        return web.json_response(
            {"error": "soundpad command not found in channel"}, status=404
        )

    try:
        await sp_cmd(query=query)
        log.info(
            f"[RELAY-SOUNDPAD] invoked /soundpad query={query!r} in channel={channel_id}"
        )
        return web.json_response({"invoked": True, "query": query})
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            log.warning(
                "[RELAY-SOUNDPAD] Discord rate-limited /soundpad invocation: %s", e
            )
            return web.json_response({"error": f"rate-limited: {e}"}, status=429)
        log.exception("[RELAY-SOUNDPAD] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_soundpad_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)
    except Exception as e:
        log.exception("[RELAY-SOUNDPAD] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_soundpad_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)


async def _relay_invoke_generarimagen(request: web.Request) -> web.Response:
    """Ask the userbot to invoke VaPls's /generarimagen slash command with a
    ``query`` (used as prompt) argument so the image is generated under the
    real user account. Mirrors :func:`_relay_invoke_play`."""
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        query = str(data["query"]).strip()
    except Exception as e:
        log.warning("[RELAY-GENIMAGEN] rejected invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)
    if not query:
        return web.json_response({"error": "empty query"}, status=400)
    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not (
        hasattr(channel, "application_commands") or hasattr(channel, "slash_commands")
    ):
        return web.json_response(
            {"error": "channel has no slash command API"}, status=400
        )

    try:
        cmds = await _resolve_slash_commands(
            channel,
            "generarimagen",
            timeout=config.INDIO_RELAY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "[RELAY-GENIMAGEN] slash_commands() timed out for channel %s", channel_id
        )
        return web.json_response({"error": "slash_commands() timed out"}, status=504)
    except Exception as e:
        log.exception("[RELAY-GENIMAGEN] slash_commands() failed")
        analytics.capture_exception(
            e, properties={"action": "relay_genimagen_slash_commands_failed"}
        )
        return web.json_response({"error": f"slash_commands() failed: {e}"}, status=500)

    gen_cmd = _pick_vapls_command(cmds, "generarimagen")
    if gen_cmd is None:
        log.warning(
            "[RELAY-GENIMAGEN] VaPls /generarimagen not found in channel %s (saw %d candidates)",
            channel_id,
            len(cmds),
        )
        return web.json_response(
            {"error": "generarimagen command not found in channel"}, status=404
        )

    try:
        await gen_cmd(prompt=query)
        log.info(
            f"[RELAY-GENIMAGEN] invoked /generarimagen prompt={query!r} in channel={channel_id}"
        )
        return web.json_response({"invoked": True, "query": query})
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            log.warning(
                "[RELAY-GENIMAGEN] Discord rate-limited /generarimagen invocation: %s", e
            )
            return web.json_response({"error": f"rate-limited: {e}"}, status=429)
        log.exception("[RELAY-GENIMAGEN] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_genimagen_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)
    except Exception as e:
        log.exception("[RELAY-GENIMAGEN] invocation failed")
        analytics.capture_exception(
            e, properties={"action": "relay_genimagen_invocation_failed"}
        )
        return web.json_response({"error": f"invocation failed: {e}"}, status=500)


async def _relay_join(request: web.Request) -> web.Response:
    """Make the userbot join (or move to) a specific voice channel.

    Body: ``{"channel_id": <int>}``. Reuses :func:`_join_channel`, which
    handles reconnects, guild allowlist, and listening sink setup.
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not isinstance(channel, discord.VoiceChannel):
        return web.json_response(
            {"error": "channel is not a voice channel"}, status=400
        )
    if not _guild_allowed(channel.guild.id):
        return web.json_response({"error": "guild not allowed"}, status=403)

    try:
        await _join_channel(channel)
    except Exception as e:
        log.exception("[RELAY-JOIN] join failed")
        analytics.capture_exception(e, properties={"action": "relay_join_failed"})
        return web.json_response({"error": f"join failed: {e}"}, status=500)
    return web.json_response(
        {
            "joined": True,
            "channel_id": channel_id,
            "channel_name": channel.name,
        }
    )


async def _relay_edit(request: web.Request) -> web.Response:
    """Edit a message that the userbot previously posted in a text channel.

    Used by the main bot to update an Indio reply in-place after an action
    has completed (e.g. replacing "thinking…" with the final answer).

    Body: ``{"channel_id": <int>, "message_id": <int>, "content": <str>}``.
    Auth: ``X-API-Secret`` header must equal ``config.RELAY_SECRET``.
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        message_id = int(data["message_id"])
        content = str(data["content"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)

    if not hasattr(channel, "fetch_message"):
        return web.json_response({"error": "channel not messageable"}, status=400)

    try:
        msg = await channel.fetch_message(message_id)
    except Exception as e:
        return web.json_response({"error": f"message not found: {e}"}, status=404)

    try:
        await msg.edit(content=content)
    except Exception as e:
        log.exception("[RELAY] edit failed")
        analytics.capture_exception(e, properties={"action": "relay_edit_failed"})
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"ok": True, "message_id": msg.id})


async def _relay_restrict_speaker(request: web.Request) -> web.Response:
    """Set or clear the per-guild "only this speaker is heard" lock used by
    the sinks while a music vote is open in the main bot.

    Body: ``{"guild_id": <int>, "user_id": <int|null>}``. ``user_id=null``
    (or omitted) clears the restriction for ``guild_id``.
    Auth: ``X-API-Secret`` must equal ``config.RELAY_SECRET``.
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    raw_user_id = data.get("user_id")
    if raw_user_id in (None, "", 0, "0"):
        _vote_restrictions.pop(guild_id, None)
        log.info(f"[VOTE-RESTRICT] cleared for guild={guild_id}")
        return web.json_response({"ok": True, "guild_id": guild_id, "user_id": None})
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid user_id"}, status=400)
    _vote_restrictions[guild_id] = user_id
    log.info(f"[VOTE-RESTRICT] guild={guild_id} → only user {user_id} is heard")
    return web.json_response({"ok": True, "guild_id": guild_id, "user_id": user_id})


async def _relay_dm(request: web.Request) -> web.Response:
    """Send a DM as the userbot (the "real Indio").

    Body: ``{user_id, content}``. Resolves the user, opens the DM channel,
    and posts ``content``. Used so the cuenta-real avisa al user que le
    respondio en otro canal — el "alert" que el main bot no puede mandar
    desde su propia cuenta (DMs entre cuenta-real y user se sienten mas
    naturales que un mensaje del bot vapls).
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        user_id = int(data["user_id"])
        content = str(data["content"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "userbot not ready"}, status=503)

    user = client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except Exception as e:
            return web.json_response({"error": f"user not found: {e}"}, status=404)

    chunks = _split_for_relay(content)
    if not chunks:
        return web.json_response({"error": "empty content"}, status=400)

    message_ids: list[int] = []
    try:
        for chunk in chunks:
            msg = await user.send(chunk)
            message_ids.append(msg.id)
    except Exception as e:
        log.info(f"[RELAY-DM] send to {user_id} failed: {e}")
        return web.json_response({"error": str(e)}, status=502)

    return web.json_response({"sent": len(message_ids), "message_ids": message_ids})


async def _relay_sensibilidad(request: web.Request) -> web.Response:
    """Switch the VOSK wake-word sensitivity preset at runtime.

    Body: ``{"preset": <int 1..3>}``.
    Auth: ``X-API-Secret`` header must equal ``config.RELAY_SECRET``.
    Returns ``{"preset": n}`` on success, 400 on invalid body/range.
    """
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        preset = int(data["preset"])
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    try:
        _set_sensitivity(preset)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"preset": preset})


async def _relay_toggle_wake_sound(request: web.Request) -> web.Response:
    """Toggle the wake-word confirmation sound (the "huh" audio) on/off.

    Body is ignored — the endpoint simply flips the in-memory
    ``_wake_sound_enabled`` flag and returns the new state.
    Auth: ``X-API-Secret`` header must equal ``config.RELAY_SECRET``.
    """
    global _wake_sound_enabled
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    _wake_sound_enabled = not _wake_sound_enabled
    log.info("[WAKE-SOUND] toggle → %s", _wake_sound_enabled)
    return web.json_response({"enabled": _wake_sound_enabled})


async def _start_relay() -> Optional[web.AppRunner]:
    if not config.RELAY_SECRET:
        log.warning("RELAY_SECRET not set — local relay HTTP endpoint disabled.")
        return None
    app = web.Application()
    app.router.add_post("/say", _relay_say)
    app.router.add_post("/edit", _relay_edit)
    app.router.add_post("/dm", _relay_dm)
    app.router.add_post("/record", _relay_record)
    app.router.add_get("/members", _relay_members)
    app.router.add_post("/invoke_play", _relay_invoke_play)
    app.router.add_post("/invoke_soundpad", _relay_invoke_soundpad)
    app.router.add_post("/invoke_generarimagen", _relay_invoke_generarimagen)
    app.router.add_post("/join", _relay_join)
    app.router.add_post("/restrict_speaker", _relay_restrict_speaker)
    app.router.add_post("/sensibilidad", _relay_sensibilidad)
    app.router.add_post("/toggle_wake_sound", _relay_toggle_wake_sound)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.RELAY_HOST, port=config.RELAY_PORT)
    await site.start()
    log.info(
        f"[RELAY] HTTP listening on http://{config.RELAY_HOST}:{config.RELAY_PORT}"
    )
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
                log.warning("[MAIN] relay runner cleanup failed")
        if _http_session and not _http_session.closed:
            await _http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")

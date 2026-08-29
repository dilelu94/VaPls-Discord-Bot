"""TTS generation module using Piper TTS and FFmpeg voice filtering."""

import os
import sys
import re
import time
import hashlib
import logging
import subprocess
import urllib.request

log = logging.getLogger("bot.tts")

VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "piper_voices")
MODEL_NAME = "es_ES-davefx-medium"
MODEL_PATH = os.path.join(VOICE_DIR, f"{MODEL_NAME}.onnx")
CONFIG_PATH = os.path.join(VOICE_DIR, f"{MODEL_NAME}.onnx.json")

# Regex patterns for cleaning text prior to TTS synthesis
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_CUSTOM_EMOJI_MARKUP_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
_EMOJI_SHORTCODE_RE = re.compile(r"(?<!\w):[A-Za-z0-9_]{2,}:(?!\w)")
_DISCORD_MENTION_RE = re.compile(r"<[@#][&!]?\d+>")
_MARKDOWN_RE = re.compile(r"[*_~`#>]")
_UNICODE_EMOJI_RE = re.compile(
    r"["
    r"\U0001F600-\U0001F64F"  # Emoticons
    r"\U0001F300-\U0001F5FF"  # Misc Symbols & Pictographs
    r"\U0001F680-\U0001F6FF"  # Transport & Map Symbols
    r"\U0001F1E0-\U0001F1FF"  # Regional Indicator Symbols / Flags
    r"\U0001F900-\U0001F9FF"  # Supplemental Symbols & Pictographs
    r"\U0001FA70-\U0001FAFF"  # Symbols & Pictographs Extended-A
    r"\U00002702-\U000027B0"  # Dingbats
    r"\U000024C2-\U0001F251"  # Enclosed Characters
    r"\u2600-\u26FF"         # Misc Symbols
    r"\u2700-\u27BF"         # Dingbats
    r"\u2300-\u23FF"         # Misc Technical
    r"\u2B50\u2B55\u200D\uFE0F\u20E3"  # Stars, selectors, keycaps
    r"\U0001F000-\U0010FFFF"  # High plane emojis
    r"]+",
    flags=re.UNICODE,
)
_MULTIBLANK_RE = re.compile(r"\s+")
_PUNCTUATION_SPACE_RE = re.compile(r"\s+([,.!?:;])")


def clean_text_for_tts(text: str) -> str:
    """Clean text before TTS synthesis by stripping emojis, URLs, mentions, markdown, and excess spaces."""
    if not text:
        return ""
    out = _URL_RE.sub("", text)
    out = _CUSTOM_EMOJI_MARKUP_RE.sub("", out)
    out = _EMOJI_SHORTCODE_RE.sub("", out)
    out = _DISCORD_MENTION_RE.sub("", out)
    out = _UNICODE_EMOJI_RE.sub("", out)
    out = _MARKDOWN_RE.sub("", out)
    out = _MULTIBLANK_RE.sub(" ", out)
    out = _PUNCTUATION_SPACE_RE.sub(r"\1", out)
    return out.strip()

MODEL_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/{MODEL_NAME}.onnx"
CONFIG_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/{MODEL_NAME}.onnx.json"

# Audio filter to add a subtle old man vocal texture (slightly lower pitch, warm bass, soft treble, subtle tremor)
FFMPEG_FILTER = "asetrate=22050*0.90,aresample=22050,atempo=1.111,equalizer=f=160:g=3.5:width_type=h:width=120,equalizer=f=3000:g=-3.5:width_type=h:width=400,tremolo=f=4.5:d=0.10,volume=1.8,dynaudnorm=p=0.95:f=150"


def ensure_model_exists() -> bool:
    """Ensure the Piper voice model and config files exist locally.
    
    Downloads them if missing.
    """
    os.makedirs(VOICE_DIR, exist_ok=True)
    try:
        if not os.path.exists(MODEL_PATH):
            log.info("Downloading Piper TTS model %s...", MODEL_NAME)
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            log.info("Piper model downloaded to %s", MODEL_PATH)

        if not os.path.exists(CONFIG_PATH):
            log.info("Downloading Piper TTS config for %s...", MODEL_NAME)
            urllib.request.urlretrieve(CONFIG_URL, CONFIG_PATH)
            log.info("Piper config downloaded to %s", CONFIG_PATH)
        return True
    except Exception as e:
        log.error("Failed to ensure Piper TTS model: %s", e)
        return False


def _get_piper_cmd() -> list[str]:
    """Find a python executable or piper binary that can run piper-tts."""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "piper", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if res.returncode == 0:
            return [sys.executable, "-m", "piper"]
    except Exception:
        pass

    base_dir = os.path.dirname(os.path.abspath(__file__))
    main_venv_py = os.path.join(base_dir, "venv", "bin", "python3")
    if os.path.exists(main_venv_py):
        try:
            res = subprocess.run(
                [main_venv_py, "-m", "piper", "--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if res.returncode == 0:
                return [main_venv_py, "-m", "piper"]
        except Exception:
            pass

    import shutil
    piper_bin = shutil.which("piper")
    if piper_bin:
        return [piper_bin]

    return [sys.executable, "-m", "piper"]


def generate_tts_wav(text: str, output_path: str | None = None) -> str | None:
    """Synthesize text using Piper TTS and process audio with FFmpeg voice filter.

    Args:
        text: Text to synthesize into speech.
        output_path: Optional explicit output WAV path. If None, a temporary path is generated.

    Returns:
        Absolute path to the resulting WAV file, or None if generation failed.
    """
    cleaned_text = clean_text_for_tts(text)
    if not cleaned_text:
        log.warning("Empty text passed to generate_tts_wav")
        return None

    if not ensure_model_exists():
        log.error("Piper model missing and could not be downloaded")
        return None

    if not output_path:
        text_hash = hashlib.md5(cleaned_text.encode("utf-8")).hexdigest()[:10]
        output_path = f"/tmp/tts_indio_{text_hash}.wav"

    piper_cmd = _get_piper_cmd() + [
        "--model", MODEL_PATH,
        "--config", CONFIG_PATH,
        "--output-raw"
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-f", "s16le",
        "-ar", "22050",
        "-ac", "1",
        "-i", "pipe:0",
    ]
    if FFMPEG_FILTER:
        ffmpeg_cmd.extend(["-af", FFMPEG_FILTER])
    ffmpeg_cmd.append(output_path)

    try:
        # Pipeline: piper_proc (stdout) -> ffmpeg_proc (stdin)
        piper_proc = subprocess.Popen(
            piper_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=piper_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Allow piper_proc to receive SIGPIPE if ffmpeg_proc exits
        if piper_proc.stdout:
            piper_proc.stdout.close()

        # Send text to piper
        piper_proc.stdin.write(cleaned_text.encode("utf-8"))
        piper_proc.stdin.close()

        ffmpeg_out, ffmpeg_err = ffmpeg_proc.communicate(timeout=15)
        piper_proc.wait(timeout=5)

        if ffmpeg_proc.returncode != 0:
            log.error("FFmpeg failed during TTS processing: %s", ffmpeg_err.decode())
            return None

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        else:
            log.error("Output WAV file is missing or empty: %s", output_path)
            return None
    except Exception as e:
        log.exception("Error generating TTS audio: %s", e)
        return None


def generate_indio_tts(text: str, output_dir: str = "/tmp/tts_audios") -> str | None:
    """Synthesize Indio text into an OGG/Opus audio file for Telegram voice notes.

    Args:
        text: Text to synthesize.
        output_dir: Directory where the output audio file will be saved.

    Returns:
        The generated audio filename (e.g. 'indio_resp_abc123.ogg'), or None on failure.
    """
    cleaned_text = clean_text_for_tts(text)
    if not cleaned_text:
        log.warning("Empty text passed to generate_indio_tts")
        return None

    if not ensure_model_exists():
        log.error("Piper model missing and could not be downloaded")
        return None

    os.makedirs(output_dir, exist_ok=True)
    text_hash = hashlib.md5((cleaned_text + str(time.time())).encode("utf-8")).hexdigest()[:10]
    filename = f"indio_resp_{text_hash}.ogg"
    output_path = os.path.join(output_dir, filename)

    piper_cmd = _get_piper_cmd() + [
        "--model", MODEL_PATH,
        "--config", CONFIG_PATH,
        "--output-raw"
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-f", "s16le",
        "-ar", "22050",
        "-ac", "1",
        "-i", "pipe:0",
    ]
    if FFMPEG_FILTER:
        ffmpeg_cmd.extend(["-af", FFMPEG_FILTER])
    ffmpeg_cmd.extend(["-c:a", "libopus", "-b:a", "32k", output_path])

    try:
        piper_proc = subprocess.Popen(
            piper_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=piper_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if piper_proc.stdout:
            piper_proc.stdout.close()

        piper_proc.stdin.write(cleaned_text.encode("utf-8"))
        piper_proc.stdin.close()

        ffmpeg_out, ffmpeg_err = ffmpeg_proc.communicate(timeout=15)
        piper_proc.wait(timeout=5)

        if ffmpeg_proc.returncode != 0:
            # Fallback if libopus encoder flag fails for any reason
            fallback_cmd = [
                "ffmpeg",
                "-y",
                "-f", "s16le",
                "-ar", "22050",
                "-ac", "1",
                "-i", "pipe:0",
            ]
            if FFMPEG_FILTER:
                fallback_cmd.extend(["-af", FFMPEG_FILTER])
            fallback_cmd.append(output_path)

            piper_proc2 = subprocess.Popen(piper_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ffmpeg_proc2 = subprocess.Popen(fallback_cmd, stdin=piper_proc2.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if piper_proc2.stdout:
                piper_proc2.stdout.close()
            piper_proc2.stdin.write(cleaned_text.encode("utf-8"))
            piper_proc2.stdin.close()
            ffmpeg_proc2.communicate(timeout=15)
            piper_proc2.wait(timeout=5)
            if ffmpeg_proc2.returncode != 0:
                log.error("FFmpeg failed during OGG TTS processing: %s", ffmpeg_err.decode())
                return None

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return filename
        else:
            log.error("Output OGG file is missing or empty: %s", output_path)
            return None
    except Exception as e:
        log.exception("Error generating OGG TTS audio: %s", e)
        return None


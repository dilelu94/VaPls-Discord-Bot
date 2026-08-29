"""TTS generation module using Piper TTS and FFmpeg voice filtering."""

import os
import sys
import hashlib
import logging
import subprocess
import urllib.request

log = logging.getLogger("bot.tts")

VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "piper_voices")
MODEL_NAME = "es_ES-davefx-medium"
MODEL_PATH = os.path.join(VOICE_DIR, f"{MODEL_NAME}.onnx")
CONFIG_PATH = os.path.join(VOICE_DIR, f"{MODEL_NAME}.onnx.json")

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
    cleaned_text = (text or "").strip()
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

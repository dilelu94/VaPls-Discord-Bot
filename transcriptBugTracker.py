"""Auto-bug tracker for voice transcripts in Discord.

Detects when a user replies (`message.reference`) to a voice transcript clip
message (`audio_escuchado_*.wav` attachment or `🎙️ **...**` content) or to an
Indio persona response generated for a transcript.

Extracts:
- User correction (what was actually said in the audio clip).
- Erroneous STT transcript.
- Indio persona response text.
- Direct audio clip download URL & attachment metadata.

Submits an auto-bug Issue to GitHub via `githubIssues.create_issue(...)` and
reacts to the Discord message with 🐛.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

import discord

import config
import githubIssues

logger = logging.getLogger("bot.transcript_bug_tracker")

_known_transcript_issues: dict[int, int] = {}


def is_audio_transcript_message(msg: Optional[discord.Message]) -> bool:
    """Return True if ``msg`` represents a voice transcript clip message."""
    if msg is None:
        return False
    content = (getattr(msg, "content", "") or "").strip()
    if content.startswith("🎙️ **") or content.startswith("🎙️"):
        return True
    attachments = getattr(msg, "attachments", []) or []
    for att in attachments:
        fn = (getattr(att, "filename", "") or "").lower()
        ct = (getattr(att, "content_type", "") or "").lower()
        if "audio_escuchado" in fn or ct.startswith("audio/") or fn.endswith((".wav", ".ogg", ".mp3", ".m4a")):
            return True
    return False


def get_audio_attachment_url(msg: discord.Message) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Extract (filename, url, size_bytes) for the audio clip attached to ``msg``."""
    attachments = getattr(msg, "attachments", []) or []
    for att in attachments:
        fn = getattr(att, "filename", "") or ""
        ct = getattr(att, "content_type", "") or ""
        url = getattr(att, "url", "") or ""
        size = getattr(att, "size", None)
        if "audio_escuchado" in fn.lower() or ct.lower().startswith("audio/") or fn.lower().endswith((".wav", ".ogg", ".mp3", ".m4a")):
            return fn, url, size
    if attachments:
        first = attachments[0]
        return getattr(first, "filename", "audio.wav"), getattr(first, "url", None), getattr(first, "size", None)
    return None, None, None


def parse_transcript_text(content: str) -> Tuple[str, str]:
    """Parse speaker and raw transcript from message content (e.g., '🎙️ **Miles:** ¡Dobre indiado!')."""
    if not content:
        return "Desconocido", ""
    m = re.search(r"🎙️\s*\*\*([^*]+):\*\*\s*(.*)", content, re.DOTALL)
    if m:
        speaker = m.group(1).strip()
        text = m.group(2).strip()
        return speaker, text
    clean = re.sub(r"^🎙️\s*", "", content).strip()
    return "Desconocido", clean


def format_issue_title(erroneous_transcription: str) -> str:
    """Format a clean title for the GitHub Issue."""
    short = erroneous_transcription.strip()
    if len(short) > 60:
        short = short[:57] + "..."
    if not short:
        short = "Audio sin texto"
    return f"[Auto-Bug] Transcripción errónea STT: \"{short}\""


def format_issue_body(
    *,
    user_reply_text: str,
    erroneous_transcription: str,
    indio_response: str,
    audio_filename: Optional[str],
    audio_url: Optional[str],
    audio_size_bytes: Optional[int] = None,
    speaker: str,
    reporter_name: str,
    reporter_id: int,
    guild_name: str,
    channel_name: str,
    transcript_message_id: int,
    reply_message_id: int,
    transcript_jump_url: Optional[str] = None,
    reply_jump_url: Optional[str] = None,
) -> str:
    """Format markdown body for the STT Auto-Bug GitHub Issue."""
    size_str = f" ({audio_size_bytes / 1024:.2f} KB)" if audio_size_bytes else ""
    audio_md = f"[{audio_filename or 'Descargar Audio'}]({audio_url}){size_str}" if audio_url else "*No adjuntado*"
    indio_text = indio_response.strip() if indio_response else "(Sin respuesta del Indio)"
    err_text = erroneous_transcription.strip() if erroneous_transcription else "(Transcripción vacía)"
    corr_text = user_reply_text.strip() if user_reply_text else "(Sin corrección especificada)"

    transcript_link = f" [`{transcript_message_id}`]({transcript_jump_url})" if transcript_jump_url else f" `{transcript_message_id}`"
    reply_link = f" [`{reply_message_id}`]({reply_jump_url})" if reply_jump_url else f" `{reply_message_id}`"

    fingerprint = f"transcript-{transcript_message_id}"

    return f"""<!-- error-fingerprint: {fingerprint} -->
<!-- transcript-bug-id: {transcript_message_id} -->
## 🎙️ Auto-Bug: Transcripción de Audio Errónea

### 🗣️ Lo que realmente se dijo (Respuesta del usuario)
> **{corr_text}**

### ❌ Transcripción errónea (STT)
> **{err_text}**

### 🤖 Respuesta del Indio
> **{indio_text}**

### 🎵 Audio del clip
- **Archivo de audio**: {audio_md}

### 🛠️ Instrucción de Depuración
> 💡 **Acción requerida**: Descargar el archivo de audio adjunto (`{audio_filename or 'clip.wav'}`) y procesarlo / enviarlo a **Grok** u otros modelos de STT para comparar, depurar y mejorar la precisión del reconocimiento de voz.

### 📌 Información de Contexto
- **Usuario que habló**: {speaker}
- **Reportado por**: {reporter_name} (ID: `{reporter_id}`)
- **Servidor / Canal**: `{guild_name}` / `#{channel_name}`
- **ID Mensaje Transcripción**:{transcript_link}
- **ID Mensaje Respuesta Usuario**:{reply_link}
"""


async def fetch_message_safe(channel: Any, message_id: int) -> Optional[discord.Message]:
    """Safely fetch a message from channel without throwing exceptions."""
    try:
        if hasattr(channel, "fetch_message"):
            return await channel.fetch_message(message_id)
    except Exception as e:
        logger.debug("Failed to fetch message %s: %s", message_id, e)
    return None


async def find_indio_response_for_transcript(channel: Any, transcript_msg: discord.Message) -> str:
    """Find the Indio's response corresponding to a transcript message."""
    try:
        if hasattr(channel, "history"):
            async for msg in channel.history(after=transcript_msg, limit=5):
                ref = getattr(msg, "reference", None)
                if ref and getattr(ref, "message_id", None) == transcript_msg.id:
                    return msg.content or ""
                author = getattr(msg, "author", None)
                if author and (getattr(author, "bot", False) or getattr(author, "id", None) == config.USERBOT_USER_ID):
                    return msg.content or ""
    except Exception as e:
        logger.debug("Error searching indio response: %s", e)
    return ""


async def check_and_report_transcript_bug(message: discord.Message) -> Optional[int]:
    """Inspect message for reply to audio transcript and report auto-bug to GitHub."""
    userbot_id = getattr(config, "USERBOT_USER_ID", 519594605520486428)
    vapls_id = getattr(config, "VAPLS_BOT_ID", None)
    author_id = getattr(message.author, "id", None)
    if (
        message is None
        or message.author is None
        or getattr(message.author, "bot", False)
        or (author_id is not None and author_id in {userbot_id, vapls_id})
    ):
        return None

    ref = getattr(message, "reference", None)
    if ref is None or getattr(ref, "message_id", None) is None:
        return None

    user_reply_text = (message.content or "").strip()
    if not user_reply_text:
        return None

    channel = getattr(message, "channel", None)
    if channel is None:
        return None

    ref_msg = getattr(message, "referenced_message", None)
    if ref_msg is None:
        ref_msg = await fetch_message_safe(channel, ref.message_id)

    if ref_msg is None:
        return None

    transcript_msg: Optional[discord.Message] = None
    indio_response: str = ""

    # Case A: Direct reply to the transcript audio message
    if is_audio_transcript_message(ref_msg):
        transcript_msg = ref_msg
        indio_response = await find_indio_response_for_transcript(channel, ref_msg)
    else:
        # Case B: Reply to Indio's response message whose parent is the transcript audio message
        parent_ref = getattr(ref_msg, "reference", None)
        parent_msg = getattr(ref_msg, "referenced_message", None)
        if parent_msg is None and parent_ref and getattr(parent_ref, "message_id", None) is not None:
            parent_msg = await fetch_message_safe(channel, parent_ref.message_id)

        if parent_msg and is_audio_transcript_message(parent_msg):
            transcript_msg = parent_msg
            indio_response = ref_msg.content or ""

    if transcript_msg is None:
        return None

    speaker, erroneous_transcription = parse_transcript_text(transcript_msg.content or "")
    audio_fn, audio_url, audio_size = get_audio_attachment_url(transcript_msg)

    reporter_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "desconocido")
    reporter_id = getattr(message.author, "id", 0)
    guild = getattr(message, "guild", None)
    guild_name = getattr(guild, "name", "DM / Servidor Desconocido") if guild else "DM"
    channel_name = getattr(channel, "name", "desconocido")

    # In-memory deduplication check
    existing_in_mem = _known_transcript_issues.get(transcript_msg.id)
    if existing_in_mem:
        logger.info("Transcript bug for msg %s already cached in issue #%d", transcript_msg.id, existing_in_mem)
        comment = f"### 🔄 Nueva corrección reportada por {reporter_name}\n> **{user_reply_text}**"
        await githubIssues.add_comment(existing_in_mem, body=comment)
        try:
            await message.add_reaction("🐛")
        except Exception:
            pass
        return existing_in_mem

    # GitHub API deduplication check via fingerprint
    fingerprint = f"transcript-{transcript_msg.id}"
    try:
        existing = await githubIssues.find_issue_by_fingerprint(fingerprint)
        if existing and existing.get("number"):
            issue_num = existing["number"]
            _known_transcript_issues[transcript_msg.id] = issue_num
            logger.info("Transcript bug for msg %s already logged in issue #%d", transcript_msg.id, issue_num)
            comment = f"### 🔄 Nueva corrección reportada por {reporter_name}\n> **{user_reply_text}**"
            await githubIssues.add_comment(issue_num, body=comment)
            try:
                await message.add_reaction("🐛")
            except Exception:
                pass
            return issue_num
    except Exception as e:
        logger.warning("Error searching fingerprint for transcript bug: %s", e)

    title = format_issue_title(erroneous_transcription)
    body = format_issue_body(
        user_reply_text=user_reply_text,
        erroneous_transcription=erroneous_transcription,
        indio_response=indio_response,
        audio_filename=audio_fn,
        audio_url=audio_url,
        audio_size_bytes=audio_size,
        speaker=speaker,
        reporter_name=reporter_name,
        reporter_id=reporter_id,
        guild_name=guild_name,
        channel_name=channel_name,
        transcript_message_id=transcript_msg.id,
        reply_message_id=message.id,
        transcript_jump_url=getattr(transcript_msg, "jump_url", None),
        reply_jump_url=getattr(message, "jump_url", None),
    )

    error_label = getattr(config, "GITHUB_ERROR_LABEL", "bot-error")
    labels = list(set([error_label, "autobug", "stt-error"]))

    issue_number = await githubIssues.create_issue(
        title=title,
        body=body,
        labels=labels,
    )

    if issue_number:
        _known_transcript_issues[transcript_msg.id] = issue_number
        logger.info("Created STT Auto-Bug issue #%d for transcript msg %s", issue_number, transcript_msg.id)
        try:
            await message.add_reaction("🐛")
        except Exception as e:
            logger.debug("Could not add 🐛 reaction: %s", e)

    return issue_number

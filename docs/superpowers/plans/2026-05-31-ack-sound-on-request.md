# Ack Sound on Audio Request — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The main bot plays a short "received" blip in the voice channel when it gets an audio request (Indio music or Telegram), so the requester knows it's working.

**Architecture:** A single new helper `play_ack_clip(vc)` in `soundpadCommand.py` resolves a configured clip (`ACK_SOUND_QUERY`) via the existing `find_best_match` and plays it fire-and-forget on an already-connected voice client, but only when idle. Two call sites (`playFromIndio` in `playCommand.py`, `playAudio` in `apiServer.py`) invoke it in the silent gap before the real audio starts. Empty/unmatched config = silent no-op.

**Tech Stack:** Python 3.10+, py-cord (`discord.FFmpegOpusAudio`), pytest + pytest-asyncio. Follow the `behavioral-testing` skill: mock only at the Discord boundary (voice client + `FFmpegOpusAudio`), assert on observable outcomes.

---

## File Structure

- `config.py` — add `ACK_SOUND_QUERY` env var (main bot config).
- `soundpadCommand.py` — add `play_ack_clip(vc)` helper (owns clip resolution + playback).
- `playCommand.py` — call the helper in `playFromIndio`; add an idempotent `is_playing → stop` guard in `startPlayingCurrent` so a still-playing blip is cut before the real song.
- `apiServer.py` — call the helper in `playAudio`, right after connect and before the existing `is_playing → stop` block (which cleanly cuts the blip).
- `tests/test_ack_sound.py` — behavioral tests for `play_ack_clip`.
- `docs/configuration.md`, `.env.example` — document `ACK_SOUND_QUERY`.

---

## Task 1: Config var + `play_ack_clip` helper

**Files:**
- Modify: `config.py` (after the soundpad/audio block, ~line 17)
- Modify: `soundpadCommand.py` (add helper after `find_best_match`, ~line 70)
- Test: `tests/test_ack_sound.py` (create)

- [ ] **Step 1: Add the config var**

In `config.py`, immediately after the `CUSTOM_AUDIO_PATH` line (line 17), add:

```python
# Soundpad clip (fuzzy query, matched against CUSTOM_AUDIO_PATH) played as a
# short "request received" blip when the bot gets a music/audio request while
# idle. Empty (default) disables the feature entirely — silent no-op.
ACK_SOUND_QUERY = os.getenv("ACK_SOUND_QUERY", "")
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ack_sound.py`:

```python
"""Behavioral tests for the 'request received' acknowledgment blip.

Boundary mocked: the Discord voice client (a small fake) and
discord.FFmpegOpusAudio (so no real ffmpeg/file decode is needed). We assert on
the observable outcome: whether a clip was handed to the voice client to play.
"""
import discord
import pytest

import config
import soundpadCommand


class FakeVoiceClient:
    """Minimal stand-in for a connected py-cord voice client."""

    def __init__(self, playing=False):
        self._playing = playing
        self.played = []  # sources handed to play()

    def is_playing(self):
        return self._playing

    def play(self, source, *args, **kwargs):
        self.played.append(source)
        self._playing = True


@pytest.fixture(autouse=True)
def _stub_ffmpeg(monkeypatch):
    """Replace FFmpegOpusAudio with a marker so no real file/ffmpeg is touched."""
    monkeypatch.setattr(
        discord, "FFmpegOpusAudio", lambda path, *a, **k: ("ffmpeg", path)
    )


def _make_clip(tmp_path, category, filename):
    cat = tmp_path / category
    cat.mkdir(parents=True, exist_ok=True)
    (cat / filename).write_bytes(b"fake audio")


def test_plays_blip_when_idle_and_clip_configured(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is True
    assert len(vc.played) == 1
    assert vc.played[0][1].endswith("blip.mp3")


def test_skips_when_already_playing(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    vc = FakeVoiceClient(playing=True)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_query_empty(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_no_clip_matches(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "zzzzzzzz")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_vc_is_none(monkeypatch):
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    assert soundpadCommand.play_ack_clip(None) is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_ack_sound.py -v`
Expected: FAIL — `AttributeError: module 'soundpadCommand' has no attribute 'play_ack_clip'`.

- [ ] **Step 4: Implement the helper**

In `soundpadCommand.py`, add this function directly after `find_best_match` (ends ~line 69), before `_pick_populated_voice_channel`:

```python
def play_ack_clip(vc) -> bool:
    """Play the configured "request received" blip on an already-connected vc.

    Fire-and-forget: starts the clip and returns immediately; the caller's real
    audio cuts it off when ready. Idle-only and silent: returns ``False``
    (no-op) when ``vc`` is None, ``vc`` is already playing, ``ACK_SOUND_QUERY``
    is empty, no clip matches, or playback fails. Never raises.

    Returns:
        True if a clip was handed to ``vc`` to play; False otherwise.
    """
    if vc is None:
        return False
    try:
        if vc.is_playing():
            return False
    except Exception:
        return False
    query = (getattr(config, "ACK_SOUND_QUERY", "") or "").strip()
    if not query:
        return False
    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    path = find_best_match(query, output_dir)
    if path is None:
        return False
    try:
        vc.play(discord.FFmpegOpusAudio(path, options='-af "dynaudnorm=p=0.95:f=200"'))
    except Exception:
        return False
    return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_ack_sound.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add config.py soundpadCommand.py tests/test_ack_sound.py
git commit -m "feat(soundpad): add play_ack_clip helper + ACK_SOUND_QUERY config"
```

---

## Task 2: Wire into Indio music (`playFromIndio` + `startPlayingCurrent` guard)

**Files:**
- Modify: `playCommand.py` — `playFromIndio` (insert call after voice connect/move, before `_yt_dlp_search`, ~line 1137); `startPlayingCurrent` (add stop-guard before `self.vc.play(...)`, ~line 411)

No new automated test: these are thin glue lines over the helper that Task 1 fully covers. An integration test here would require mocking yt-dlp + voice connect and would assert wiring (call presence) rather than observable behavior — against this repo's testing philosophy, and `playCommand` is explicitly in the "pending tests" list. Verification is the full suite (no regressions) plus a manual smoke check.

- [ ] **Step 1: Add the stop-guard in `startPlayingCurrent`**

In `playCommand.py`, find (in `startPlayingCurrent`, ~line 406-411):

```python
            audioSource = discord.FFmpegOpusAudio(filepath)

            def afterCallback(error):
                asyncio.run_coroutine_threadsafe(self.onSongFinished(error), self.bot.loop)

            self.vc.play(audioSource, after=afterCallback)
```

Replace with (insert the two guard lines before `self.vc.play`):

```python
            audioSource = discord.FFmpegOpusAudio(filepath)

            def afterCallback(error):
                asyncio.run_coroutine_threadsafe(self.onSongFinished(error), self.bot.loop)

            # Cut off the "request received" blip (if still playing) before the song.
            if self.vc.is_playing():
                self.vc.stop()
            self.vc.play(audioSource, after=afterCallback)
```

- [ ] **Step 2: Insert the blip call in `playFromIndio`**

In `playCommand.py`, find (in `playFromIndio`, ~line 1133-1137):

```python
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] voice connect failed: {e}")
        return False, f"no pude conectarme a voz: {e}"

    songs = await _yt_dlp_search(query)
```

Replace with:

```python
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] voice connect failed: {e}")
        return False, f"no pude conectarme a voz: {e}"

    # Blip de "te escuché" mientras yt-dlp busca/descarga (gap silencioso).
    import soundpadCommand
    try:
        soundpadCommand.play_ack_clip(vc)
    except Exception:
        playLogger.exception("[PLAY-INDIO] ack blip failed (ignored)")

    songs = await _yt_dlp_search(query)
```

(The local `import soundpadCommand` mirrors the existing local-import pattern in this module, e.g. `from bot import safeEdit`, avoiding any import-order coupling.)

- [ ] **Step 3: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 4: Manual smoke verification**

With `ACK_SOUND_QUERY` set to a real clip name and the bot idle in a voice channel, ask the Indio to play a song. Expected: the blip plays right after the bot joins/while it searches, and gets cut when the song starts. With `ACK_SOUND_QUERY` empty: no blip, song plays as before.

- [ ] **Step 5: Commit**

```bash
git add playCommand.py
git commit -m "feat(play): play ack blip on Indio music request"
```

---

## Task 3: Wire into Telegram audio (`playAudio` in `apiServer.py`)

**Files:**
- Modify: `apiServer.py` — `playAudio` (insert call after vc connect, before the existing `is_playing → stop` block, ~line 466-468)

No new automated test: same rationale as Task 2 — thin glue over the Task 1 helper, and exercising `playAudio` requires mocking aiohttp multipart + voice connect (implementation wiring, not observable behavior). `apiServer` is in the repo's "pending tests" list. Verify via full suite + manual smoke.

Placement note: the blip goes **after** the connect block and **before** the existing `if vc.is_playing(): vc.stop()` (line ~468). That existing stop cleanly cuts the blip before the uploaded file plays. If music was already playing when the request arrived, `play_ack_clip`'s own `is_playing()` check skips the blip (matches "skip when busy"), and the existing stop then clears the prior audio as before.

- [ ] **Step 1: Insert the blip call**

In `apiServer.py`, find (end of the connect block + the existing stop, ~line 459-473):

```python
            try:
                vc = await channel.connect(reconnect=True, timeout=10.0)
            except Exception as e:
                try:
                    os.remove(uploadPath)
                except Exception:
                    pass
                return web.json_response({"error": f"failed to join: {e}"}, status=500)

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception:
            pass
```

Replace with (insert the blip block between the connect block and the existing stop):

```python
            try:
                vc = await channel.connect(reconnect=True, timeout=10.0)
            except Exception as e:
                try:
                    os.remove(uploadPath)
                except Exception:
                    pass
                return web.json_response({"error": f"failed to join: {e}"}, status=500)

        # "Request received" blip during the connect gap. Skipped if audio is
        # already playing; the stop below cleanly cuts it before the real clip.
        try:
            import soundpadCommand
            soundpadCommand.play_ack_clip(vc)
        except Exception:
            logger.exception("ack blip failed (ignored)")

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception:
            pass
```

- [ ] **Step 2: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 3: Manual smoke verification**

With `ACK_SOUND_QUERY` set and the bot idle, push an audio via the Telegram `/play-audio` path. Expected: the blip plays on join and is cut when the uploaded clip starts. With the bot already playing music: no blip. With `ACK_SOUND_QUERY` empty: no blip.

- [ ] **Step 4: Commit**

```bash
git add apiServer.py
git commit -m "feat(api): play ack blip on Telegram play-audio request"
```

---

## Task 4: Document `ACK_SOUND_QUERY`

**Files:**
- Modify: `docs/configuration.md` (audio config table, ~line 45)
- Modify: `.env.example` (soundpad/audio block, ~line 46)

- [ ] **Step 1: Add the docs table row**

In `docs/configuration.md`, after the `CUSTOM_AUDIO_PATH` row (~line 45), add:

```markdown
| `ACK_SOUND_QUERY` | Clip (búsqueda fuzzy sobre `CUSTOM_AUDIO_PATH`) que suena como blip de "pedido recibido" cuando el bot recibe un pedido de música del Indio o audio de Telegram estando idle. Vacío = desactivado. | _(vacío)_ |
```

- [ ] **Step 2: Add the `.env.example` entry**

In `.env.example`, in the `# Soundpad / audio clips` block, after the `CUSTOM_AUDIO_PATH` line, add:

```bash
# Blip de "pedido recibido" (búsqueda fuzzy contra CUSTOM_AUDIO_PATH). Vacío = off.
ACK_SOUND_QUERY=
```

- [ ] **Step 3: Commit**

```bash
git add docs/configuration.md .env.example
git commit -m "docs: document ACK_SOUND_QUERY"
```

---

## Self-Review Notes

- **Spec coverage:** main-bot ownership (Tasks 1-3); `ACK_SOUND_QUERY` + `find_best_match` reuse + no-op when unconfigured (Task 1); idle-only / skip-when-busy (helper `is_playing` check, tested); fire-and-forget + cut-off (Task 1 returns immediately; Task 2 guard + Task 3 existing stop); both call sites (Tasks 2-3); out-of-scope `/play` & `/soundpad` & userbot untouched; tests (Task 1); docs in `configuration.md` + `.env.example` (Task 4). All covered.
- **Fire-and-forget cut-off:** Indio path cut by new guard in `startPlayingCurrent`; Telegram path cut by the pre-existing `is_playing → stop` block.
- **Naming consistency:** helper `play_ack_clip(vc)`, config `ACK_SOUND_QUERY` used identically across all tasks and tests.

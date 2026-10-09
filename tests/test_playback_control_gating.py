"""Tests for playback control actions gating (PAUSE_MUSIC, RESUME_MUSIC, etc.)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from geminiCommand import _gate_play_music_actions


def test_gate_pause_music_suppressed_on_unrelated_text():
    actions = [("PAUSE_MUSIC", None)]
    unrelated_text = "[REACCIÓN ESPONTÁNEA: El usuario Suicidio acaba de compartir algo con banderas rojas...]"
    result = _gate_play_music_actions(actions, unrelated_text)
    assert result == []


def test_gate_pause_music_kept_on_explicit_order():
    actions = [("PAUSE_MUSIC", None)]
    explicit_text = "pausá la música por favor"
    result = _gate_play_music_actions(actions, explicit_text)
    assert result == [("PAUSE_MUSIC", None)]


def test_gate_resume_music_suppressed_and_kept():
    actions = [("RESUME_MUSIC", None)]
    assert _gate_play_music_actions(actions, "hola indio qué tal") == []
    assert _gate_play_music_actions(actions, "continuá con la música") == [("RESUME_MUSIC", None)]


def test_gate_skip_music_suppressed_and_kept():
    actions = [("SKIP_MUSIC", None)]
    assert _gate_play_music_actions(actions, "qué buena foto") == []
    assert _gate_play_music_actions(actions, "salteá este tema") == [("SKIP_MUSIC", None)]


def test_gate_stop_music_suppressed_and_kept():
    actions = [("STOP_MUSIC", None)]
    assert _gate_play_music_actions(actions, "cuándo jugamos?") == []
    assert _gate_play_music_actions(actions, "pará la música") == [("STOP_MUSIC", None)]


def test_gate_allows_voice_transcriptions():
    actions = [("PAUSE_MUSIC", None)]
    voice_text = "[voz] pausa"
    assert _gate_play_music_actions(actions, voice_text) == [("PAUSE_MUSIC", None)]

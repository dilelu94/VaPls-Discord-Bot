"""Behavioral tests for Indio's angry day mood state and 'cabecear el enano' berretines."""
from __future__ import annotations

import random
from geminiCommand import (
    INDIO_SYSTEM,
    _is_indio_angry_day,
    _get_indio_mood_block,
    _build_indio_system_instruction,
)


def test_indio_system_prompt_includes_cabecear_el_enano_berretines():
    """Verify that INDIO_SYSTEM instructs Indio on using 'cabecear el enano' berretines."""
    system = INDIO_SYSTEM.lower()
    assert "cabecear el enano" in system
    assert "berretines" in system


def test_is_indio_angry_day_deterministic_per_date():
    """Verify that _is_indio_angry_day produces deterministic results for a date string."""
    date_str = "2026-10-06"
    res1 = _is_indio_angry_day(date_str)
    res2 = _is_indio_angry_day(date_str)
    assert res1 == res2
    assert isinstance(res1, bool)


def test_is_indio_angry_day_frequency():
    """Verify that ~1 out of 7 days triggers the angry day state."""
    angry_days = 0
    total_days = 70
    for i in range(total_days):
        date_str = f"2026-01-{(i % 30) + 1:02d}-day{i}"
        if _is_indio_angry_day(date_str):
            angry_days += 1
    # Random distribution check (out of 70 days, expect ~10 angry days)
    assert 3 <= angry_days <= 20


def test_get_indio_mood_block_returns_instructions_on_angry_day():
    """Verify that _get_indio_mood_block returns the mood block on angry days and empty string otherwise."""
    # Find one date that is angry and one that is not
    angry_date = None
    normal_date = None
    for day in range(1, 30):
        d_str = f"2026-05-{day:02d}"
        if _is_indio_angry_day(d_str):
            angry_date = d_str
        else:
            normal_date = d_str
        if angry_date and normal_date:
            break

    assert angry_date is not None
    assert normal_date is not None

    angry_block = _get_indio_mood_block(angry_date)
    normal_block = _get_indio_mood_block(normal_date)

    assert normal_block == ""
    assert "MEDIO ENOJADO" in angry_block
    assert "cabecear el enano" in angry_block.lower()
    assert "haces burla" in angry_block.lower() or "hacés burla" in angry_block.lower()


def test_build_indio_system_instruction_injects_angry_mood_block():
    """Verify that _build_indio_system_instruction injects mood block on angry days."""
    angry_date = None
    for day in range(1, 30):
        d_str = f"2026-06-{day:02d}"
        if _is_indio_angry_day(d_str):
            angry_date = d_str
            break

    instruction = _build_indio_system_instruction(
        lt_block="[Notas long term]",
        emoji_block="[Emojis]",
        date_str=angry_date,
    )
    assert "[ESTADO DE ÁNIMO DEL DÍA: MEDIO ENOJADO / DÍA CRUZADO]" in instruction
    assert "[Notas long term]" in instruction
    assert "[Emojis]" in instruction

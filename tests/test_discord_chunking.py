"""Behavior: long Gemini replies must be split into Discord-postable chunks
(<= the per-message limit), capped at a maximum number of chunks, with the last
chunk marked as truncated when content overflows the cap."""
import geminiCommand as gc
from geminiCommand import _split_for_discord

LIMIT = gc._DISCORD_CHUNK_LIMIT
MAX = gc._MAX_CHUNKS


def test_short_text_is_single_unchanged_chunk():
    text = "una respuesta cortita"
    assert _split_for_discord(text) == [text]


def test_text_at_limit_stays_single():
    text = "x" * LIMIT
    assert _split_for_discord(text) == [text]


def test_splits_into_multiple_chunks_each_within_limit():
    text = "\n".join("linea " + str(i) + " " + "y" * 200 for i in range(40))
    chunks = _split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= LIMIT for c in chunks)


def test_single_overlong_line_is_hard_split():
    text = "z" * (LIMIT * 2 + 50)  # no newlines to split on
    chunks = _split_for_discord(text)
    assert all(len(c) <= LIMIT for c in chunks)
    assert len(chunks) >= 2


def test_overflow_is_capped_and_marked_truncated():
    # Far more content than MAX chunks can hold.
    text = "\n".join("p" * (LIMIT - 1) for _ in range(MAX * 3))
    chunks = _split_for_discord(text)
    assert len(chunks) == MAX
    assert all(len(c) <= LIMIT for c in chunks)
    assert chunks[-1].endswith("…(truncado)")


def test_content_preserved_up_to_truncation():
    # When not overflowing the cap, the concatenation reproduces the input.
    text = "\n".join("ab" * 400 for _ in range(3))  # ~2400 chars, 2 chunks
    chunks = _split_for_discord(text)
    assert len(chunks) <= MAX
    assert "".join(chunks) == text

"""Test save_memory when replying to a message with server IP / important data."""

import pytest
from unittest.mock import AsyncMock

import geminiCommand


@pytest.mark.asyncio
async def test_execute_save_memory_enriches_with_replied_content(monkeypatch):
    """_execute_save_memory should append replied_content if not already in content."""
    monkeypatch.setattr(geminiCommand, "_indio_long_term", {})
    monkeypatch.setattr(geminiCommand, "_persist_indio_state", AsyncMock(return_value=None))

    replied = "IP del Servidor: 163.176.114.14:2456 Pass: equisde Mundo: Fox"
    ok, msg = await geminiCommand._execute_save_memory(
        guild_id=999,
        arg="IP del server de Valheim",
        replied_content=replied,
    )

    assert ok is True
    lt_key = "guild-999"
    assert lt_key in geminiCommand._indio_long_term
    events = geminiCommand._indio_long_term[lt_key].get("eventos_del_grupo", [])
    assert len(events) == 1
    assert "163.176.114.14:2456" in events[0]
    assert "Valheim" in events[0]

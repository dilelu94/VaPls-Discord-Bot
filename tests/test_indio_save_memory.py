"""Tests for the save_memory Gemini tool and _execute_save_memory action."""

import json
import pytest
import geminiCommand


@pytest.mark.asyncio
async def test_execute_save_memory_user_anecdote(tmp_path, monkeypatch):
    """Saving memory for a specific target_user updates user's anecdotes."""
    memory_file = tmp_path / "indio_memory.json"
    monkeypatch.setattr(geminiCommand.config, "INDIO_MEMORY_PATH", str(memory_file))
    
    guild_id = 999111
    lt_key = f"guild-{guild_id}"
    geminiCommand._indio_long_term[lt_key] = {}

    arg_payload = json.dumps({
        "content": "Viny va a vender la peluca en 500 pesos",
        "target_user": "Viny",
        "category": "anecdota"
    })

    ok, msg = await geminiCommand._execute_save_memory(guild_id, arg_payload)
    assert ok is True
    assert "saved" in msg

    lt_data = geminiCommand._indio_long_term[lt_key]
    assert "users" in lt_data
    assert "Viny" in lt_data["users"]
    assert "Viny va a vender la peluca en 500 pesos" in lt_data["users"]["Viny"]["anecdotas"]

    # Verify atomic persistence to disk
    assert memory_file.exists()
    saved_disk = json.loads(memory_file.read_text())
    disk_anecdotes = saved_disk["entries"][lt_key]["long_term"]["users"]["Viny"]["anecdotas"]
    assert "Viny va a vender la peluca en 500 pesos" in disk_anecdotes


@pytest.mark.asyncio
async def test_execute_save_memory_internal_joke(tmp_path, monkeypatch):
    """Saving memory with category='chiste' adds to chistes_internos."""
    memory_file = tmp_path / "indio_memory.json"
    monkeypatch.setattr(geminiCommand.config, "INDIO_MEMORY_PATH", str(memory_file))
    
    guild_id = 999222
    lt_key = f"guild-{guild_id}"
    geminiCommand._indio_long_term[lt_key] = {}

    arg_payload = json.dumps({
        "content": "Pionono de Mila",
        "category": "chiste"
    })

    ok, msg = await geminiCommand._execute_save_memory(guild_id, arg_payload)
    assert ok is True
    
    lt_data = geminiCommand._indio_long_term[lt_key]
    assert "chistes_internos" in lt_data
    assert "Pionono de Mila" in lt_data["chistes_internos"]


@pytest.mark.asyncio
async def test_execute_save_memory_group_event(tmp_path, monkeypatch):
    """Saving memory with category='evento' adds to eventos_del_grupo."""
    memory_file = tmp_path / "indio_memory.json"
    monkeypatch.setattr(geminiCommand.config, "INDIO_MEMORY_PATH", str(memory_file))
    
    guild_id = 999333
    lt_key = f"guild-{guild_id}"
    geminiCommand._indio_long_term[lt_key] = {}

    arg_payload = json.dumps({
        "content": "Jugaron al Valheim hasta las 4 AM",
        "category": "evento"
    })

    ok, msg = await geminiCommand._execute_save_memory(guild_id, arg_payload)
    assert ok is True
    
    lt_data = geminiCommand._indio_long_term[lt_key]
    assert "eventos_del_grupo" in lt_data
    assert "Jugaron al Valheim hasta las 4 AM" in lt_data["eventos_del_grupo"]


@pytest.mark.asyncio
async def test_execute_save_memory_deduplication(tmp_path, monkeypatch):
    """Executing save_memory with duplicate content does not duplicate items."""
    memory_file = tmp_path / "indio_memory.json"
    monkeypatch.setattr(geminiCommand.config, "INDIO_MEMORY_PATH", str(memory_file))
    
    guild_id = 999444
    lt_key = f"guild-{guild_id}"
    geminiCommand._indio_long_term[lt_key] = {"chistes_internos": ["Pionono de Mila"]}

    arg_payload = json.dumps({
        "content": "Pionono de Mila",
        "category": "chiste"
    })

    ok, msg = await geminiCommand._execute_save_memory(guild_id, arg_payload)
    assert ok is True
    assert geminiCommand._indio_long_term[lt_key]["chistes_internos"].count("Pionono de Mila") == 1


def test_save_memory_tool_in_catalog():
    """Verify save_memory tool is in _INDIO_TOOLS catalog and mapped properly."""
    tool_names = [t["name"] for t in geminiCommand._INDIO_TOOLS]
    assert "save_memory" in tool_names

    mapping = geminiCommand._FUNCTION_CALL_TO_ACTION.get("save_memory")
    assert mapping == ("SAVE_MEMORY", None)

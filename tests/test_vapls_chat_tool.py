"""
Behavioral tests for /vapls chat history search tool integration.
"""

import time
import pytest
import chat_db
import geminiCommand
from geminiClient import GeminiReply


@pytest.fixture(autouse=True)
def setup_temp_db(tmp_path):
    db_file = str(tmp_path / "test_vapls_tool.db")
    chat_db.init_db(db_file)
    yield
    chat_db.close_db()


@pytest.mark.asyncio
async def test_vapls_executes_chat_search_tool(ctx_factory, monkeypatch):
    now = int(time.time())
    chat_db.save_message({
        "message_id": 999,
        "guild_id": 1,
        "channel_id": 10,
        "channel_name": "soreteposting",
        "author_id": 55,
        "author_name": "Seba",
        "content": "La IP de Valheim es 163.176.114.14:2456",
        "created_at": now,
    })

    ctx = ctx_factory(user_id=55, display_name="Seba")

    generate_calls = []

    async def fake_generate(*args, **kwargs):
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            # First turn: Gemini returns a functionCall for search_chat_history
            return GeminiReply(
                text="",
                finish_reason="STOP",
                prompt_tokens=10,
                response_tokens=5,
                model="gemini-2.0-flash",
                function_calls=[
                    {
                        "name": "search_chat_history",
                        "args": {"query": "valheim", "author_name": "seba"},
                    }
                ],
            )
        else:
            # Second turn: Gemini processes volatile_context with chat results and returns answer
            v_ctx = kwargs.get("volatile_context") or ""
            assert "163.176.114.14:2456" in v_ctx
            return GeminiReply(
                text="La IP del servidor de Valheim que mandó Seba es 163.176.114.14:2456.",
                finish_reason="STOP",
                prompt_tokens=30,
                response_tokens=15,
                model="gemini-2.0-flash",
                function_calls=[],
            )

    monkeypatch.setattr("geminiClient.generate", fake_generate)

    await geminiCommand.vaplsLogic(ctx, "te acordas la IP de valheim que mando seba?")

    assert len(generate_calls) == 2
    # Verify that followup.send received the final answer containing the IP
    all_sent = " ".join(call.args[0] for call in ctx.followup.send.call_args_list)
    assert "163.176.114.14:2456" in all_sent

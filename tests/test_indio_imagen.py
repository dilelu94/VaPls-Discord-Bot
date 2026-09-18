"""Behavioral tests for Indio /imagen tool integration (Text-to-Image & Image-to-Image)."""

import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import geminiCommand
import pollinationsImage


@pytest.mark.asyncio
async def test_indio_tool_mapping_generate_image():
    """Verify generate_image tool maps to GENERATE_IMAGE action in geminiCommand."""
    tool_calls = [
        {
            "name": "generate_image",
            "args": {
                "prompt": "un gato futurista con gafas de sol",
                "image_url": "https://cdn.discordapp.com/attachments/123/foto.jpg",
            },
        }
    ]
    actions = geminiCommand._actions_from_function_calls(tool_calls)
    assert len(actions) == 1
    action_type, arg_json = actions[0]
    assert action_type == "GENERATE_IMAGE"
    parsed = json.loads(arg_json)
    assert parsed["prompt"] == "un gato futurista con gafas de sol"
    assert parsed["image_url"] == "https://cdn.discordapp.com/attachments/123/foto.jpg"


@pytest.mark.asyncio
async def test_dispatch_indio_actions_generate_image_relay(monkeypatch):
    """Test GENERATE_IMAGE action dispatches via userbot relay when available."""
    bot = MagicMock()
    reply_handle = types.SimpleNamespace(channel_id=123456)
    captured_relay = []

    async def _mock_invoke_slash(endpoint, channel_id, query):
        captured_relay.append((endpoint, channel_id, query))
        return True, "ok"

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot", _mock_invoke_slash)

    arg = json.dumps(
        {
            "prompt": "un perro chef",
            "image_url": "https://example.com/base.jpg",
        }
    )

    statuses = await geminiCommand._dispatch_indio_actions(
        bot=bot,
        guild_id=999,
        actions=[("GENERATE_IMAGE", arg)],
        reply_handle=reply_handle,
    )

    assert len(statuses) == 1
    assert "imagen: ok" in statuses[0]
    assert len(captured_relay) == 1
    endpoint, cid, query = captured_relay[0]
    assert endpoint == "invoke_imagen"
    assert cid == 123456
    parsed_q = json.loads(query)
    assert parsed_q["prompt"] == "un perro chef"
    assert parsed_q["image_url"] == "https://example.com/base.jpg"


@pytest.mark.asyncio
async def test_dispatch_indio_actions_generate_image_attachment_fallback(monkeypatch):
    """Test GENERATE_IMAGE falls back to attachment_urls when image_url is missing from tool args."""
    bot = MagicMock()
    reply_handle = types.SimpleNamespace(channel_id=123456)
    captured_relay = []

    async def _mock_invoke_slash(endpoint, channel_id, query):
        captured_relay.append((endpoint, channel_id, query))
        return True, "ok"

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot", _mock_invoke_slash)

    # Tool call only has prompt, no explicit image_url
    arg = json.dumps({"prompt": "agregale un sombrero de mariachi"})
    attachments = [
        {"url": "https://cdn.discordapp.com/attachments/555/original.png", "mime_type": "image/png"}
    ]

    statuses = await geminiCommand._dispatch_indio_actions(
        bot=bot,
        guild_id=999,
        actions=[("GENERATE_IMAGE", arg)],
        reply_handle=reply_handle,
        attachment_urls=attachments,
    )

    assert len(statuses) == 1
    assert "imagen: ok" in statuses[0]
    parsed_q = json.loads(captured_relay[0][2])
    assert parsed_q["prompt"] == "agregale un sombrero de mariachi"
    assert parsed_q["image_url"] == "https://cdn.discordapp.com/attachments/555/original.png"


@pytest.mark.asyncio
async def test_dispatch_indio_actions_generate_image_direct_fallback(monkeypatch):
    """Test GENERATE_IMAGE falls back to direct pollinationsImage call if relay fails."""
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)

    reply_handle = types.SimpleNamespace(channel_id=777888)

    async def _mock_invoke_slash(endpoint, channel_id, query):
        return False, "relay connection refused"

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot", _mock_invoke_slash)
    monkeypatch.setattr(
        pollinationsImage,
        "generate_or_edit_image",
        AsyncMock(return_value=b"fake-generated-jpeg-bytes"),
    )

    arg = json.dumps({"prompt": "un dragon espacial"})

    statuses = await geminiCommand._dispatch_indio_actions(
        bot=bot,
        guild_id=999,
        actions=[("GENERATE_IMAGE", arg)],
        reply_handle=reply_handle,
    )

    assert len(statuses) == 1
    assert "imagen: ok" in statuses[0]
    assert channel.send.called
    kwargs = channel.send.call_args.kwargs
    assert "file" in kwargs
    assert "un dragon espacial" in kwargs.get("content", "")


@pytest.mark.asyncio
async def test_relay_invoke_imagen_handler(monkeypatch):
    """Test _relay_invoke_imagen in userbot finds /imagen and invokes it."""
    from pathlib import Path
    userbot_src = (Path(__file__).resolve().parent.parent / "userbot" / "bot.py").read_text().splitlines()

    def _extract(name: str) -> str:
        start = next(
            i for i, line in enumerate(userbot_src)
            if line.startswith(f"async def {name}(") or line.startswith(f"def {name}(")
        )
        end = next(
            i for i, line in enumerate(userbot_src[start + 1:], start=start + 1)
            if line.startswith(("async def ", "def ", "class "))
        )
        return "\n".join(userbot_src[start:end])

    owner_block = _extract("_command_owner_id")
    pick_block = _extract("_pick_vapls_command")
    resolve_block = _extract("_resolve_slash_commands")
    handler_block = _extract("_relay_invoke_imagen")

    vapls_bot_id = getattr(config, "VAPLS_BOT_ID", 1_489_830_543_074_918_482)

    mock_cmd = AsyncMock()
    mock_cmd.name = "imagen"
    mock_cmd.application_id = vapls_bot_id

    async def _mock_resolve_slash(channel, name, timeout):
        return [mock_cmd]

    channel = MagicMock()
    client = MagicMock()
    client.is_ready.return_value = True
    client.get_channel.return_value = channel

    import asyncio, logging, discord
    from aiohttp import web
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        RELAY_SECRET="secret123",
        INDIO_RELAY_TIMEOUT=2.0,
        VAPLS_BOT_ID=vapls_bot_id,
    )

    ns = {
        "config": cfg,
        "client": client,
        "log": logging.getLogger("test_imagen"),
        "web": web,
        "discord": discord,
        "asyncio": asyncio,
        "json": json,
        "analytics": MagicMock(),
    }

    exec(owner_block, ns)
    exec(pick_block, ns)
    exec(resolve_block, ns)
    exec(handler_block, ns)

    _relay_invoke_imagen = ns["_relay_invoke_imagen"]
    ns["_resolve_slash_commands"] = _mock_resolve_slash

    req = MagicMock(spec=web.Request)
    req.headers = {"X-API-Secret": "secret123"}
    req.json = AsyncMock(
        return_value={
            "channel_id": "123456",
            "query": json.dumps(
                {
                    "prompt": "make it cyberpunk",
                    "image_url": "https://example.com/input.jpg",
                }
            ),
        }
    )

    resp = await _relay_invoke_imagen(req)
    assert resp.status == 200
    mock_cmd.assert_called_once_with(
        prompt="make it cyberpunk",
        imagen_url="https://example.com/input.jpg",
    )

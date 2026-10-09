"""Behavioral tests for the weekly patch notes generator."""

import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock

import geminiClient
import patch_notes_generator


@pytest.fixture
def sample_commits():
    return [
        "9cc7b45 - fix(stt): aislar replies a audios de transcripcion para evitar respuestas del indio",
        "c1c83e2 - fix(jojo): eliminar ejemplos hardcodeados y filtrar falsos positivos",
        "7c79785 - feat(patch-notes): web ui temporal con tokens efimeros de 24h y headers de seguridad",
        "72f8702 - feat(userbot): agregar cooldown de 1 hora para pity de audios raros",
    ]


@pytest.mark.asyncio
async def test_get_recent_git_commits_runs_successfully():
    """get_recent_git_commits invokes git log and returns non-empty list of commits."""
    commits = await patch_notes_generator.get_recent_git_commits(days=30)
    assert isinstance(commits, list)
    assert len(commits) > 0
    # Commits have hash and message
    assert " - " in commits[0]


@pytest.mark.asyncio
async def test_generate_patch_notes_with_gemini_json(sample_commits, monkeypatch):
    """When Gemini returns a valid JSON structure, it is parsed and mapped directly."""
    mock_json = """{
      "version": "2.7",
      "title": "Notas de Parche (09/10/2026)",
      "discord_highlight": {
        "category_title": "🧠 RESPUESTAS Y COMPORTAMIENTO DEL INDIO",
        "items": [
          {
            "title": "Aislamiento de Respuestas STT",
            "tag": "Fix",
            "desc": "Se filtraron los replies a clips de voz para que el Indio no responda por error."
          }
        ]
      },
      "sections": [
        {
          "icon": "🧠",
          "title": "Comportamiento del Indio",
          "items": [
            {
              "tag": "Fix",
              "tag_type": "fix",
              "header": "Aislamiento de Respuestas",
              "desc": "Detalle técnico del fix."
            }
          ]
        }
      ]
    }"""

    async def fake_generate(**kwargs):
        return geminiClient.GeminiReply(
            text=f"```json\n{mock_json}\n```",
            finish_reason="STOP",
            prompt_tokens=100,
            response_tokens=150,
            model="gemini-2.5-flash",
        )

    monkeypatch.setattr(geminiClient, "generate", fake_generate)

    res = await patch_notes_generator.generate_patch_notes_with_gemini(sample_commits, "09/10/2026")
    assert res["version"] == "2.7"
    assert "RESPUESTAS Y COMPORTAMIENTO DEL INDIO" in res["discord_highlight"]["category_title"]
    assert len(res["discord_highlight"]["items"]) == 1
    assert res["discord_highlight"]["items"][0]["tag"] == "Fix"
    assert len(res["sections"]) == 1


@pytest.mark.asyncio
async def test_generate_patch_notes_fallback_on_gemini_failure(sample_commits, monkeypatch):
    """When Gemini throws an error or is unconfigured, fallback builds structured notes from commits."""
    async def fail_generate(**kwargs):
        raise geminiClient.GeminiError("API rate limit exceeded", kind="http", status=429)

    monkeypatch.setattr(geminiClient, "generate", fail_generate)

    res = await patch_notes_generator.generate_patch_notes_with_gemini(sample_commits, "09/10/2026")
    assert "version" in res
    assert "discord_highlight" in res
    assert len(res["discord_highlight"]["items"]) >= 1
    assert "sections" in res
    assert len(res["sections"]) >= 1


def test_format_discord_patch_notes_message():
    """The Discord message adheres strictly to the required format with bold title and link."""
    highlight_data = {
      "category_title": "📺 STREAMING & GOLIVE",
      "items": [
        {
          "title": "A/V Sync",
          "tag": "Mejora",
          "desc": "Se corrigió el desfasaje en streams.",
        },
        {
          "title": "Filtro AFK",
          "tag": "Fix",
          "desc": "El bot no sigue a usuarios a canales AFK.",
        },
      ],
    }
    url = "https://vapls.duckdns.org/patch-notes/xyz123"
    msg = patch_notes_generator.format_discord_patch_notes_message("09/10/2026", highlight_data, url)

    assert "**Notas del parche vapls (09/10/2026)**" in msg
    assert "📺 STREAMING & GOLIVE" in msg
    assert "• **A/V Sync (Mejora):** Se corrigió el desfasaje en streams." in msg
    assert "• **Filtro AFK (Fix):** El bot no sigue a usuarios a canales AFK." in msg
    assert f"🔗 [Ver notas del parche completo]({url})" in msg


def test_cron_state_persistence(tmp_path):
    """Cron state correctly saves and loads execution date."""
    state_file = str(tmp_path / "cron_state.json")
    state = patch_notes_generator.load_cron_state(state_file)
    assert state == {}

    patch_notes_generator.save_cron_state({"last_run_date": "2026-10-09"}, state_file)
    reloaded = patch_notes_generator.load_cron_state(state_file)
    assert reloaded.get("last_run_date") == "2026-10-09"


@pytest.mark.asyncio
async def test_generate_and_post_weekly_patch_notes_e2e(tmp_path, sample_commits, monkeypatch):
    """End-to-end execution creates token, formats message, posts to channel, and updates state."""
    state_file = str(tmp_path / "test_state.json")
    monkeypatch.setattr(patch_notes_generator.config, "PATCH_NOTES_CRON_STATE_PATH", state_file)

    async def fake_commits(days=7):
        return sample_commits

    monkeypatch.setattr(patch_notes_generator, "get_recent_git_commits", fake_commits)

    # Fake Discord channel & bot
    sent_messages = []
    fake_channel = MagicMock()
    fake_channel.id = 451580655650996236
    fake_channel.name = "soreteposting"

    async def fake_send(content):
        mock_msg = MagicMock()
        mock_msg.id = 123456789
        sent_messages.append(content)
        return mock_msg

    fake_channel.send = AsyncMock(side_effect=fake_send)

    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=fake_channel)

    # Force run
    result = await patch_notes_generator.generate_and_post_weekly_patch_notes(fake_bot, force=True)
    assert result is not None
    assert "token" in result
    assert "url" in result
    assert "vapls.duckdns.org/patch-notes/" in result["url"]
    assert len(sent_messages) == 1
    assert "**Notas del parche vapls (" in sent_messages[0]

    # Verify state was saved
    state = patch_notes_generator.load_cron_state(state_file)
    today = datetime.datetime.now(patch_notes_generator.TZ_ARG).strftime("%Y-%m-%d")
    assert state.get("last_run_date") == today

    # Running again without force should skip because already run today
    second_run = await patch_notes_generator.generate_and_post_weekly_patch_notes(fake_bot, force=False)
    assert second_run is None
    # No new messages sent
    assert len(sent_messages) == 1

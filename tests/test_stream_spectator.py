"""Behavior: StreamSpectatorManager tracks stream spectator sessions per guild,
executing stream inspections and generating Indio TTS commentary."""
import pytest
from unittest.mock import MagicMock
from userbot.stream_spectator import StreamSpectatorManager


@pytest.fixture
def spectator_mgr():
    vcs = {}
    def get_vc(guild_id):
        return vcs.get(guild_id)

    mgr = StreamSpectatorManager(voice_client_getter=get_vc)
    mgr._vcs = vcs
    return mgr


async def test_start_and_stop_watching_session(spectator_mgr, gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "SKIP"}]},
        }],
    })

    session = await spectator_mgr.start_watching(
        guild_id=12345,
        streamer_name="Miles",
        immediate_check=False,
    )
    assert spectator_mgr.is_watching(12345)
    assert session["streamer_name"] == "Miles"

    stopped = await spectator_mgr.stop_watching(12345)
    assert stopped is True
    assert not spectator_mgr.is_watching(12345)


async def test_inspect_now_generates_commentary(spectator_mgr, gemini_http, monkeypatch):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Qué tremenda jugada en el stream!"}]},
        }],
    })

    # Mock voice client
    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    spectator_mgr._vcs[12345] = mock_vc

    # Mock TTS wav generation
    monkeypatch.setattr("tts.generate_tts_wav", lambda text: "/tmp/fake.wav")
    monkeypatch.setattr("os.path.exists", lambda path: True)

    session = await spectator_mgr.start_watching(
        guild_id=12345,
        streamer_name="Miles",
        immediate_check=False,
    )

    comment = await spectator_mgr.inspect_now(12345)
    assert comment == "Qué tremenda jugada en el stream!"
    assert mock_vc.play.called

    await spectator_mgr.stop_watching(12345)

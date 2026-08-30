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


async def test_inspect_now_posts_to_text_channel(gemini_http, monkeypatch):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Tremendo stream che!"}]},
        }],
    })

    posted_texts = []
    async def fake_post(text):
        posted_texts.append(text)

    mgr = StreamSpectatorManager(
        voice_client_getter=lambda gid: None,
        post_text_fn=fake_post,
    )

    await mgr.start_watching(guild_id=999, streamer_name="Diego", immediate_check=False)
    comment = await mgr.inspect_now(999)

    assert comment == "Tremendo stream che!"
    assert posted_texts == ["Tremendo stream che!"]
    await mgr.stop_watching(999)


async def test_set_fast_mode(spectator_mgr):
    await spectator_mgr.start_watching(guild_id=777, streamer_name="Test", immediate_check=False)
    session = spectator_mgr.get_session(777)
    assert session["fast_mode"] is False

    ok = spectator_mgr.set_fast_mode(777, True)
    assert ok is True
    assert session["fast_mode"] is True

    await spectator_mgr.stop_watching(777)


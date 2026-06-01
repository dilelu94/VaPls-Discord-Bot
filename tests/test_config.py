"""Behavior: config.py turns environment variables into typed module constants,
applying defaults and parsing the comma-separated DEBUG_GUILD_IDS list.

We reload the module with a controlled environment. dotenv.load_dotenv is
neutralised so a stray .env on the machine can't leak into the assertions.
"""
import importlib

import pytest

_CONFIG_VARS = [
    "TOKEN", "MODEL_PATH_ES", "MODEL_PATH_EN", "AUDIO_DIR", "CUSTOM_AUDIO_PATH",
    "YT_DLP_PATH", "DEBUG_GUILD_IDS", "RAM_THRESHOLD_MB", "PLAY_COOLDOWN",
    "POSTHOG_API_KEY", "POSTHOG_HOST", "API_HOST", "API_PORT", "API_SECRET",
    "GEMINI_API_KEY", "GEMINI_MODEL", "INDIO_MEMORY_PATH",
    "INDIO_PLAY_CHANNEL_ID",
]


@pytest.fixture
def load_config(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)

    def _load(env: dict):
        for var in _CONFIG_VARS:
            monkeypatch.delenv(var, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import config
        return importlib.reload(config)

    return _load


@pytest.fixture(autouse=True)
def _restore_config_module():
    # Reload from the real environment after each test so other test modules
    # see the genuine config values (load_dotenv is restored by monkeypatch).
    yield
    import config
    importlib.reload(config)


def test_debug_guild_ids_unset_is_none(load_config):
    cfg = load_config({})
    assert cfg.DEBUG_GUILD_IDS is None


def test_debug_guild_ids_parsed_to_ints(load_config):
    cfg = load_config({"DEBUG_GUILD_IDS": "111,222,333"})
    assert cfg.DEBUG_GUILD_IDS == [111, 222, 333]


def test_debug_guild_ids_tolerates_blanks_and_trailing_comma(load_config):
    cfg = load_config({"DEBUG_GUILD_IDS": "111, ,222,"})
    assert cfg.DEBUG_GUILD_IDS == [111, 222]


def test_numeric_coercion_and_defaults(load_config):
    cfg = load_config({})
    assert cfg.RAM_THRESHOLD_MB == 300 and isinstance(cfg.RAM_THRESHOLD_MB, int)
    assert cfg.PLAY_COOLDOWN == 5.0 and isinstance(cfg.PLAY_COOLDOWN, float)
    assert cfg.API_PORT == 8080 and isinstance(cfg.API_PORT, int)


def test_numeric_values_from_env(load_config):
    cfg = load_config({"RAM_THRESHOLD_MB": "512", "PLAY_COOLDOWN": "2.5",
                       "API_PORT": "9000"})
    assert cfg.RAM_THRESHOLD_MB == 512
    assert cfg.PLAY_COOLDOWN == 2.5
    assert cfg.API_PORT == 9000


def test_string_defaults_present(load_config):
    cfg = load_config({})
    assert cfg.GEMINI_MODEL == "gemini-2.5-flash"
    assert cfg.INDIO_MEMORY_PATH == "data/indio_memory.json"


def test_string_overrides_from_env(load_config):
    cfg = load_config({"GEMINI_MODEL": "gemini-3-pro",
                       "INDIO_MEMORY_PATH": "/tmp/mem.json"})
    assert cfg.GEMINI_MODEL == "gemini-3-pro"
    assert cfg.INDIO_MEMORY_PATH == "/tmp/mem.json"


def test_indio_play_channel_id_unset_falls_to_prod_default(load_config):
    """When INDIO_PLAY_CHANNEL_ID is unset, falls back to the hardcoded prod
    music channel (451607097432604672). Same pattern as INDIO_REPLY_CHANNEL_ID:
    el default apunta a prod para que los wake-words de voz que disparan
    música caigan directo en el canal correcto sin tener que setear .env.
    """
    cfg = load_config({})
    assert cfg.INDIO_PLAY_CHANNEL_ID == 451607097432604672


def test_indio_play_channel_id_honors_env(load_config):
    cfg = load_config({"INDIO_PLAY_CHANNEL_ID": "9876543210"})
    assert cfg.INDIO_PLAY_CHANNEL_ID == 9876543210


async def test_invoke_slash_via_userbot_refuses_when_channel_id_is_zero(
        monkeypatch):
    """End-to-end safety: with INDIO_PLAY_CHANNEL_ID=0 the relay must
    refuse before touching the network, letting the caller fall back to
    the local playFromIndio path (which has its own channel-pick logic).
    """
    import geminiCommand
    monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_URL",
                        "http://127.0.0.1:1/say", raising=False)
    monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_SECRET",
                        "test-secret", raising=False)

    ok, msg = await geminiCommand._invoke_slash_via_userbot(
        "invoke_play", channel_id=0, query="x"
    )

    assert ok is False
    # The message must distinguish this from "relay not configured" so the
    # caller's log line can tell operator that the *channel* is the gap,
    # not the relay endpoint.
    assert "channel" in msg.lower()

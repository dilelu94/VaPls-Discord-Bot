"""Verify that every slash command has a valid flag defined."""

import re

import flags

_BOT_PATH = __import__("config").__file__.rsplit("/", 1)[0] + "/bot.py"


def _get_slash_command_names():
    with open(_BOT_PATH) as f:
        text = f.read()
    return re.findall(r'^\s*@bot\.slash_command[^)]*name="([^"]+)"', text, re.MULTILINE)


def test_all_commands_have_valid_flags():
    names = _get_slash_command_names()
    assert names, "no commands found in bot.py"
    for name in names:
        flag = flags.get_command_flag(name)
        assert flag is not None, f"/{name} has no flag defined"
        assert flag in flags.VALID_FLAGS, f"/{name} has invalid flag {flag!r}"


def test_no_orphan_flags():
    names = set(_get_slash_command_names())
    for name in flags.COMMAND_FLAGS:
        assert name in names, (
            f"flag entry '{name}' does not match any slash command in bot.py"
        )

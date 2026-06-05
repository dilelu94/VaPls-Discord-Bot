"""Lint-style regression tests: ensure no print() or silent except: pass escape.

These tests scan the project's Python source files to detect two anti-patterns
that bypass the OTLP logging pipeline:

1.  ``print(...)`` calls — logging goes through the structured pipeline,
    ``print`` output is invisible in PostHog.

2.  Silent ``except: pass`` or ``except Exception: pass`` — exceptions must
    be at least logged so they propagate to PostHog via the root logger.

Known exceptions are allowed via explicit allow-list entries (full-file or
line-pattern).
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories and files to skip entirely.
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    "venv",
    "userbot/venv",
    "migration",
    ".bun",
    "node_modules",
    ".env",
}

# Files that are allowed to contain print() calls (e.g. CLI tools, scripts).
_ALLOW_PRINT = {
    "posthog_client.py",  # has debug prints guarded by if/else
    "setup_gemini_session.py",  # standalone CLI utility for Gemini session setup
    "setup_gemini_auto.py",  # standalone CLI utility for Gemini auto-setup
    "dump_elements.py",  # one-off debug script
    "test_app_url.py",  # one-off debug script
    "test_app_url_headful.py",  # one-off debug script
}

# Patterns that are allowed after ``except`` without logging.
# These are cleanup/teardown blocks where failure is acceptable.
_ALLOW_SILENT_EXCEPT_RE = re.compile(
    r"except.*:\s*$"
    r"(?:\s*(?:#.*)?\n\s*)"
    r"(?:\s*(?:break|continue|return\s+(?:False|None|\[\])|vc\.cleanup\(\))\s*$)",
    re.MULTILINE,
)

# --- Helpers -----------------------------------------------------------------


def _iter_py_files(root: Path):
    """Yield all ``.py`` files under ``root``, skipping vendor dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        parts = set(rel.parts)
        if parts & _SKIP_DIRS:
            dirnames.clear()
            continue
        # prune hidden dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


# --- Tests -------------------------------------------------------------------


def _find_prints(filepath: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for every ``print(`` call found by AST."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            hits.append((node.lineno, ast.unparse(node)))
    return hits


def _find_silent_excepts(filepath: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for bare ``except.*: pass`` blocks."""
    lines = filepath.read_text(encoding="utf-8").splitlines()
    hits: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Match:  except X:  (possibly with trailing comment)
        m = re.match(r"^(\s*)except\s", line)
        if not m:
            i += 1
            continue
        # check if the next non-empty line is just "pass" at the same indentation
        indent = m.group(1)
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if (
            j < len(lines)
            and lines[j].strip() == "pass"
            and _indent_of(lines[j]) == len(indent)
        ):
            # Skip known-safe patterns
            code = line + "\n" + lines[j]
            if _is_known_safe_except(code, filepath.name):
                i = j + 1
                continue
            hits.append((i + 1, line.strip() + " " + lines[j].strip()))
        i += 1
    return hits


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


_KNOWN_SAFE_FILE_PATTERNS: dict[str, list[re.Pattern]] = {
    "users.py": [re.compile(r"except KeyError")],
    "geminiCommand.py": [
        re.compile(r"except Exception:\s*\n\s*voice_channel = None"),
    ],
}


def _is_known_safe_except(block: str, filename: str) -> bool:
    patterns = _KNOWN_SAFE_FILE_PATTERNS.get(filename, [])
    return any(p.search(block) for p in patterns)


# -----------------------------------------------------------------------------


def test_no_print_statements():
    """No ``print(`` call should exist outside explicitly-allowed files."""
    errors: list[str] = []
    for fp in _iter_py_files(PROJECT_ROOT):
        if fp.name in _ALLOW_PRINT:
            continue
        hits = _find_prints(fp)
        for lineno, code in hits:
            rel = fp.relative_to(PROJECT_ROOT)
            errors.append(f"{rel}:{lineno}: {code}")
    if errors:
        pytest.fail(
            f"Found {len(errors)} print() call(s) that bypass the OTLP pipeline.\n"
            "Use logger.*() instead so logs reach PostHog.\n" + "\n".join(errors)
        )


def test_no_silent_except_pass():
    """No silent ``except.*: pass`` outside known-safe cleanup blocks."""
    errors: list[str] = []
    for fp in _iter_py_files(PROJECT_ROOT):
        hits = _find_silent_excepts(fp)
        for lineno, code in hits:
            rel = fp.relative_to(PROJECT_ROOT)
            errors.append(f"{rel}:{lineno}: {code}")
    if errors:
        pytest.fail(
            f"Found {len(errors)} silent except:pass block(s).\n"
            "Add at least logger.warning() so the exception propagates to PostHog.\n"
            + "\n".join(errors)
        )

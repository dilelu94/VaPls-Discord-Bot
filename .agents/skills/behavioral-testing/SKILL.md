---
name: behavioral-testing
description: Use when writing or changing tests in VaPls-Discord-Bot — pin observable behavior (not implementation) and mock only at real boundaries, so the code stays refactorable
---

# Behavioral Testing (VaPls-Discord-Bot)

## Overview

A test pins **one observable promise the bot makes** — not a unit of code. Write
tests so the implementation can be rewritten underneath them without the tests
turning red, as long as the behavior the user sees is unchanged.

**Core principle:** Test what the code *does*, not how it's built.

If a test breaks when you rename a helper, reorder calls, or refactor internals
*without changing behavior*, the test is wrong — fix the test, not the code.

## The Three Rules

### 1. Assert on outcomes, never on wording or call counts

- ✅ "a reply containing the model's text reached the user"
- ✅ "history afterward holds the user turn + model turn"
- ❌ `followup.send.assert_called_once_with("⏱️ Gemini tardó demasiado…")` — exact
  Spanish string; reworded copy breaks it for no reason
- ❌ `assert mock.call_count == 3` — couples to internal structure

The user-facing strings and analytics events change often. Assert that *an* error
was shown, that it differs from a success reply, that it contains the HTTP status
— not the literal sentence. (See `tests/test_error_messages.py` for the pattern.)

### 2. Mock only at true process boundaries

Exercise our own helpers for real. Fake **only** what crosses a process edge.
Boundaries in this repo and the fixtures that fake them (`tests/conftest.py`):

| Boundary | How to fake it |
|---|---|
| Discord gateway / context | `ctx_factory` — fake `ApplicationContext`; `ctx.followup.send` records messages |
| Discord voice / `FFmpegOpusAudio` | `MagicMock` voice client; monkeypatch `discord.FFmpegOpusAudio` |
| Gemini **HTTP** (`geminiClient.generate`) | `gemini_http` — fakes `aiohttp` (status + payload, or timeout/parse error) |
| Gemini at the **call** level (command logic) | `patch_generate` + `reply_factory` — fake `geminiClient.generate` directly |
| PostHog analytics | `stub_analytics` (autouse) — no-op; analytics is fire-and-forget infra, never assert on it |
| Filesystem | `tmp_path` + monkeypatch the config path (`INDIO_MEMORY_PATH`, `CUSTOM_AUDIO_PATH`) |
| Subprocess (`yt-dlp`) / Vosk | fake the subprocess / recognizer (not yet covered — see deferred list) |

Don't mock our own functions to "isolate" them — call them. If something is hard
to test without mocking everything, that's a design signal: the code is too
coupled, not the test too hard.

### 3. Fakes must mirror reality completely

When you fake an external response, include **all** fields the real one returns,
not just the ones today's test reads. A partial fake passes while real integration
fails. (See the Gemini payloads in `tests/test_gemini_client.py`:
`candidates`, `finishReason`, `content.parts`, `usageMetadata`.)

Never add a method to production code that only tests use. Put test-only helpers
in the test file or `conftest.py`.

## This repo's setup

- **Framework:** `pytest` + `pytest-asyncio` (`asyncio_mode = auto`, so `async def
  test_*` needs no marker). Config in `pytest.ini`.
- **Location & naming:** `tests/`, snake_case `test_*.py` (the one legacy
  `testSoundpad.py` keeps its name; both are collected).
- **Shared fakes:** `tests/conftest.py`. Reuse the fixtures above before inventing
  new ones.
- **Run it:**
  ```bash
  pip install -r requirements-dev.txt
  pytest
  ```
  `requirements-dev.txt` installs the plain `py-cord` **wheel** (not the production
  git `[voice]` build) so `discord` imports without libopus/ffmpeg.
- **CI:** `.github/workflows/ci.yml` runs `pytest` on every push/PR.
- Full rationale and coverage map: [docs/testing.md](../../../docs/testing.md).

## Workflow

1. Name the behavior in plain words ("a Gemini timeout shows a friendly retry
   message"). One behavior per test; if the name needs "and", split it.
2. Build the scenario with the existing fixtures, faking only the boundary involved.
3. Drive the public entry point (`vaplsLogic`, `indioLogic`, `generate`,
   `checkKeywords`, …) — not private helpers, unless the helper *is* the unit of
   behavior (e.g. pure functions like `_split_for_discord`, `_clamp_long_term`).
4. Assert on the outcome (what the user saw, the resulting state, the return value).
5. **Refactor-safety check:** could you rename an internal helper or reorder calls
   and keep this test green? If not, loosen the assertion.

## Red flags — stop and reconsider

| Thought / pattern | Reality |
|---|---|
| `assert_called_once_with("<exact string>")` | Couples to copy. Assert the message *contains* the key fact. |
| Asserting an analytics event fired | Analytics is infra. Stub it; don't test it. |
| Mocking one of our own functions | Call it for real. Mock only at the boundary. |
| Fake response with "just the fields I need" | Mirror the real structure completely or it fails silently. |
| `session.destroy()` / method only tests call | Move it to a test helper; keep production clean. |
| Test broke after a no-op refactor | The test was coupled to structure. Fix the test. |

## Verification checklist

- [ ] Each test pins one observable behavior, with a name that says which.
- [ ] Assertions are on outcomes, not exact wording or call counts.
- [ ] Only true boundaries are faked; our own code runs for real.
- [ ] Fakes mirror the real data shape completely.
- [ ] No test-only methods added to production modules.
- [ ] Suite is green and output is clean (no stray warnings / leaked async tasks).

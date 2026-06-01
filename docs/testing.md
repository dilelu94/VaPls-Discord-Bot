# Testing

La suite vive en `tests/` y corre con **pytest** + **pytest-asyncio**
(`asyncio_mode = auto`). La filosofía es testear *comportamiento observable*, no
detalle de implementación: se mockea solo en los bordes reales (Discord, la API
HTTP de Gemini, PostHog, FFmpeg, el filesystem) y se asienta sobre los
resultados, así el código se puede refactorizar sin romper los tests.

> **Antes de tocar tests, leé la skill [`behavioral-testing`](../.agents/skills/behavioral-testing/SKILL.md).**

## Cobertura actual
Suite de comportamiento (Tier 1 lógica pura, Tier 2 lógica con un borde mockeado):

- `test_keywords.py` — detección de wake words (`keywords.checkKeywords`).
- `test_config.py` — parsing/defaults de `config.py` (reload con env monkeypatcheado).
- `test_discord_chunking.py` — `_split_for_discord` (límite, hard-split, tope de chunks).
- `test_error_messages.py` — `_error_message` por `kind` y persona (`indio` vs `vapls`).
- `test_user_header.py` — `_format_user_header`.
- `test_gemini_client.py` — `geminiClient.generate` con la red faked (todos los `GeminiError`).
- `test_vapls_logic.py` / `test_indio_logic.py` — lógica de `/vapls` y `/indio`.
- `test_long_term_memory.py` — memoria de largo plazo del Indio.
- `test_greeting.py` — saludos al unirse a voz (throttle, skips).
- `test_idle_watchdog.py` — auto-desconexión por inactividad.
- `test_recording.py` — captura de voice-reply del userbot.
- `test_soundpad_clip_search.py` — búsqueda de clips.
- `testSoundpad.py` — suite legacy de navegación del Soundpad (`unittest`,
  pytest la sigue descubriendo).

Pendiente para un segundo pase: `playCommand`, `apiServer`, `userbot/bot.py`
(Vosk/DAVE) y migrar las aserciones exactas de `testSoundpad.py` a estilo
behavioral.

## Correr los tests
```bash
pip install -r requirements-dev.txt
pytest -q
```

`make check` es el comando canónico de "done": elige un intérprete que tenga las
deps (venv activo → `.venv` → `python3`) y corre la suite. Ver
[Definition of Done en AGENTS.md](../AGENTS.md) y el hook `.githooks/pre-push`.

Solo se necesitan las **dev deps**: la suite fakea el gateway de Discord, la API
de Gemini, FFmpeg y el filesystem, así que **no** hace falta el build de git de
`py-cord` ni libs de audio del sistema.

## CI
`.github/workflows/ci.yml` corre `pytest` en cada `push` y `pull_request`, sobre
**Python 3.10 únicamente** — la versión exacta que corre el server de producción
(Ubuntu 22.04). Probar solo esa versión mantiene la CI rápida y la mantiene
alineada con prod; ver el constraint en
[Operaciones → CI/CD](operations.md#cicd-pipeline).

> Nota histórica: la CI antes corría una matriz 3.10–3.14 porque `audioop` se
> removió del stdlib en 3.13 (backport `audioop-lts` en `requirements*.txt`). El
> backport sigue declarado, así que el bot también levanta en ≥3.13, pero solo
> gateamos sobre 3.10.

El job `deploy` corre después de que pasa el job de tests.

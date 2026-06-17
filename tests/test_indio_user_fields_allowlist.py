"""Behavior: the indio prompt must only expose fields explicitly listed in
``_INDIO_USER_FIELDS``. Operational fields like ``greeting`` (path a un .mp3
de bienvenida) and ``block_dynamic_substrings`` (filtros de memoria) deben
permanecer fuera del prompt — son datos internos del bot, no del personaje.

Estos tests son guardrails: si alguien suma un campo nuevo al dict de USERS,
o cambia el pick del Indio para incluir más campos sin querer, esto rompe."""

import geminiCommand as gc
from geminiCommand import _format_long_term, _static_user_traits, _INDIO_USER_FIELDS
from users import USERS


def test_dossier_keys_match_allowlist():
    """Cada user en el output tiene EXACTAMENTE las keys de la allowlist —
    nada más, nada menos. Si alguien agrega 'address' al pick, esto rompe."""
    dossiers = _static_user_traits()
    assert dossiers, "expected at least one user from USERS"
    for name, data in dossiers.items():
        assert set(data.keys()) == set(_INDIO_USER_FIELDS), (
            f"user {name!r} expone keys inesperadas: {set(data.keys())}"
        )


def test_greeting_never_leaks_to_indio_prompt():
    """Los paths de greeting (e.g. 'hava-nagila-cut.mp3') están en USERS pero
    nunca deben aparecer en el bloque de long-term que ve Gemini."""
    greetings = [
        info["greeting"]
        for info in USERS.values()
        if isinstance(info, dict) and info.get("greeting")
    ]
    assert greetings, "expected at least one user with greeting in USERS"

    members = [
        info["name"]
        for info in USERS.values()
        if isinstance(info, dict) and info.get("name")
    ]
    rendered = _format_long_term({"users": {}}, current_members=members)

    for path in greetings:
        assert path not in rendered, f"greeting path leaked: {path!r}"
        # También chequeamos el basename (e.g. 'hava-nagila-cut.mp3') por si
        # alguien rendereara solo el filename sin el prefijo de carpeta.
        basename = path.rsplit("/", 1)[-1]
        assert basename not in rendered, f"greeting basename leaked: {basename!r}"


def test_block_substrings_are_not_exposed_as_user_fields():
    """``block_dynamic_substrings`` es un campo operativo (filtro de memoria
    dinámica). Nunca debe aparecer como key en el dossier que va al prompt."""
    dossiers = _static_user_traits()
    for name, data in dossiers.items():
        assert "block_dynamic_substrings" not in data, (
            f"user {name!r} expone block_dynamic_substrings al prompt"
        )


def test_blocked_dynamic_facts_are_scrubbed_from_user_dossier(monkeypatch):
    """Si Gemini distila un fact que matchea un block del usuario, ese fact
    se filtra del dossier de ESE usuario antes de llegar al prompt. Pinea el
    comportamiento real del feature block_dynamic_substrings."""
    ghost = {"name": "Ghost", "block_dynamic_substrings": ["secreta"]}
    monkeypatch.setattr(gc, "_NON_DISCORD_MEMBERS", [ghost])
    monkeypatch.setattr(gc, "_USERS", {})
    lt = {
        "users": {
            "Ghost": {
                "traits": [
                    "tiene un dato secreta",  # debería filtrarse (matchea block)
                    "es un tipazo",  # debería pasar
                ]
            }
        }
    }
    rendered = _format_long_term(lt, current_members=["Ghost"])

    ghost_idx = rendered.find("- Ghost:")
    assert ghost_idx >= 0, "expected Ghost dossier section"
    next_idx = rendered.find("\n- ", ghost_idx + 1)
    chunk = rendered[ghost_idx : next_idx if next_idx > 0 else None]

    assert "es un tipazo" in chunk
    assert "dato secreta" not in chunk


def test_unknown_user_field_is_ignored(monkeypatch):
    """Si alguien agrega un campo nuevo a USERS, no debe filtrarse al prompt
    salvo que se lo agregue explícitamente a la allowlist."""
    fake_user = {
        "name": "TestGhost",
        "traits": ["aparece y desaparece"],
        "secret_data": "PASSWORD_LEAK_CANARY",
        "internal_notes": ["nota interna que no debe leakear"],
    }
    monkeypatch.setattr(gc, "_USERS", {999999999: fake_user})
    monkeypatch.setattr(gc, "_NON_DISCORD_MEMBERS", [])

    dossiers = _static_user_traits()
    assert "TestGhost" in dossiers
    rendered = _format_long_term({"users": {}}, current_members=["TestGhost"])

    # El trait legítimo sí aparece (sanity: el pipeline funciona).
    assert "aparece y desaparece" in rendered
    # Los campos no listados nunca aparecen.
    assert "PASSWORD_LEAK_CANARY" not in rendered
    assert "nota interna" not in rendered
    # Tampoco en el dossier crudo.
    for value in dossiers["TestGhost"].values():
        assert "PASSWORD_LEAK_CANARY" not in value
        assert all("nota interna" not in v for v in value)

"""Behavior: verify that INDIO_SYSTEM instructs the Indio to respond naturally and
without prudish moralizing or scolding when group members talk about women,
physical attraction, or female attributes.
"""
from __future__ import annotations


def _indio_system() -> str:
    from geminiCommand import INDIO_SYSTEM
    return INDIO_SYSTEM.lower()


def test_indio_system_prompt_includes_masculine_stance_on_attraction():
    """INDIO_SYSTEM must instruct Indio to respond as a regular man/veteran of
    the group on female beauty/attraction, without moralizing or calling users
    degenerates."""
    system = _indio_system()
    assert "belleza femenina" in system or "atracción física" in system
    assert "persignado" in system or "moralista" in system
    assert "degenerado" in system

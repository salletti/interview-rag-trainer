"""Réglages S8 de ``KnowledgeBaseSettings`` (variables ``KB_*``).

- ``KB_RECONCILE_INTERVAL_SECONDS`` (``reconcile_interval_seconds``, ``> 0``) :
  période du beat qui planifie un cycle de réconciliation ; rendu en
  ``timedelta`` par ``reconcile_interval``. Valeur Proposed (roadmap S8,
  « réglage des intervalles ») ; seul le caractère positif est figé ici.
- ``KB_RECONCILE_MANIFEST_SAMPLE_SIZE`` (``reconcile_manifest_sample_size``,
  ``>= 0``) : nombre d'indexations ``COMPLETED`` dont le manifest est
  revérifié à chaque cycle (échantillonnage, coût sur un grand corpus).
- ``KB_IDEMPOTENCY_KEY_TTL_SECONDS`` (``idempotency_key_ttl_seconds``, ``> 0``) :
  âge au-delà duquel une clé d'idempotence est purgée (O15) ; défaut **24 h**
  (recommandation de O15) ; rendu en ``timedelta`` par ``idempotency_key_ttl``.
- ``KB_MAX_DOCUMENT_TOKENS`` (``max_document_tokens``, ``> 0``) : borne de
  volume de texte extrait, commune à tous les formats (O16) ; valeur à mesurer.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from knowledge_base import KnowledgeBaseSettings


def settings() -> KnowledgeBaseSettings:
    return KnowledgeBaseSettings(_env_file=None)


def test_the_idempotency_keys_expire_after_24_hours_by_default() -> None:
    assert settings().idempotency_key_ttl == timedelta(hours=24)


@pytest.mark.parametrize(
    ("variable", "attribute", "value", "expected"),
    [
        (
            "KB_RECONCILE_INTERVAL_SECONDS",
            "reconcile_interval",
            "90",
            timedelta(seconds=90),
        ),
        (
            "KB_IDEMPOTENCY_KEY_TTL_SECONDS",
            "idempotency_key_ttl",
            "3600",
            timedelta(hours=1),
        ),
        ("KB_RECONCILE_MANIFEST_SAMPLE_SIZE", "reconcile_manifest_sample_size", "7", 7),
        ("KB_MAX_DOCUMENT_TOKENS", "max_document_tokens", "12345", 12345),
    ],
)
def test_the_s8_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    attribute: str,
    value: str,
    expected: object,
) -> None:
    monkeypatch.setenv(variable, value)

    assert getattr(settings(), attribute) == expected


def test_the_defaults_are_usable_values() -> None:
    current = settings()

    assert current.reconcile_interval > timedelta(0)
    assert current.reconcile_manifest_sample_size >= 0
    assert current.max_document_tokens > 0


@pytest.mark.parametrize(
    "variable",
    [
        "KB_RECONCILE_INTERVAL_SECONDS",
        "KB_IDEMPOTENCY_KEY_TTL_SECONDS",
        "KB_MAX_DOCUMENT_TOKENS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_s8_setting_is_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        settings()


def test_a_negative_sample_size_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_RECONCILE_MANIFEST_SAMPLE_SIZE", "-1")

    with pytest.raises(ValidationError):
        settings()

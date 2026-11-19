"""Réglage S7 de ``KnowledgeBaseSettings`` : délai de grâce des blobs orphelins
(``KB_BLOB_ORPHAN_GRACE_SECONDS``, défaut 24 h, ADR 0012 point 6 Proposed,
strictement positif), rendu en ``timedelta`` par ``blob_orphan_grace``."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from knowledge_base import KnowledgeBaseSettings


def test_the_orphan_grace_defaults_to_24_hours() -> None:
    settings = KnowledgeBaseSettings(_env_file=None)

    assert settings.blob_orphan_grace == timedelta(hours=24)


def test_the_orphan_grace_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_BLOB_ORPHAN_GRACE_SECONDS", "90")

    assert KnowledgeBaseSettings(_env_file=None).blob_orphan_grace == timedelta(
        seconds=90
    )


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_orphan_grace_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KB_BLOB_ORPHAN_GRACE_SECONDS", value)

    with pytest.raises(ValidationError):
        KnowledgeBaseSettings(_env_file=None)

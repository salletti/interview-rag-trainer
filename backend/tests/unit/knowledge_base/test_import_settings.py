"""Réglages S2 de ``KnowledgeBaseSettings`` : limite d'upload et racine des blobs.

API figée : ``max_upload_bytes`` (``KB_MAX_UPLOAD_BYTES``, défaut 20 Mo, ADR 0012,
strictement positif) et ``blob_root`` (``KB_BLOB_ROOT``, ``Path``, défaut
``/var/lib/interview-rag-trainer/blobs``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from knowledge_base import KnowledgeBaseSettings


def test_the_upload_limit_defaults_to_20_mebibytes() -> None:
    assert KnowledgeBaseSettings(_env_file=None).max_upload_bytes == 20 * 1024 * 1024


def test_the_blob_root_defaults_to_the_documented_volume() -> None:
    settings = KnowledgeBaseSettings(_env_file=None)

    assert settings.blob_root == Path("/var/lib/interview-rag-trainer/blobs")


def test_both_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KB_MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))

    settings = KnowledgeBaseSettings(_env_file=None)

    assert settings.max_upload_bytes == 4096
    assert settings.blob_root == tmp_path


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_upload_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KB_MAX_UPLOAD_BYTES", value)

    with pytest.raises(ValidationError):
        KnowledgeBaseSettings(_env_file=None)

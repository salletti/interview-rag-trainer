"""Value Objects de Knowledge Base (S1) : validation et immutabilité.

API figée :

- ``ContentHash(value: str)`` : SHA-256 hexadécimal minuscule de 64 caractères,
  sinon ``ValueError`` ; dataclass figée, égalité par valeur.
- ``DocumentSource(filename, media_type, size_bytes, content_hash, imported_at)``
  figée ; ``filename`` non blanc et ``size_bytes >= 0`` sinon ``ValueError``.
- ``DocumentKind`` : ``COURSE``, ``QA_SHEET``, ``REVISION_SHEET``, ``TECHNICAL``.
- ``FailureReason(code, message, retryable)`` figée ; ``code`` non blanc.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from tests.unit.knowledge_base.domain.builders import source

VALID = "a" * 64


def test_content_hash_accepts_a_sha256_hex_digest() -> None:
    assert ContentHash(VALID).value == VALID


@pytest.mark.parametrize(
    "bad",
    ["", "a" * 63, "a" * 65, "g" * 64, "A" * 64, " " + "a" * 63],
    ids=["empty", "too-short", "too-long", "not-hex", "uppercase", "space"],
)
def test_content_hash_rejects_anything_but_64_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        ContentHash(bad)


def test_content_hash_is_compared_by_value() -> None:
    assert ContentHash(VALID) == ContentHash(VALID)
    assert ContentHash(VALID) != ContentHash("b" * 64)
    assert len({ContentHash(VALID), ContentHash(VALID)}) == 1


def test_content_hash_cannot_be_mutated() -> None:
    with pytest.raises(FrozenInstanceError):
        ContentHash(VALID).value = "b" * 64  # type: ignore[misc]


@pytest.mark.parametrize("field", ["filename", "size_bytes", "content_hash"])
def test_document_source_cannot_be_mutated(field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(source(), field, "x")


def test_document_source_rejects_a_blank_filename() -> None:
    ok = source()
    with pytest.raises(ValueError, match="filename"):
        DocumentSource(
            filename="   ",
            media_type=ok.media_type,
            size_bytes=ok.size_bytes,
            content_hash=ok.content_hash,
            imported_at=ok.imported_at,
        )


def test_document_source_rejects_a_negative_size() -> None:
    ok = source()
    with pytest.raises(ValueError, match="size_bytes"):
        DocumentSource(
            filename=ok.filename,
            media_type=ok.media_type,
            size_bytes=-1,
            content_hash=ok.content_hash,
            imported_at=ok.imported_at,
        )


def test_document_kind_has_the_four_documented_values() -> None:
    assert {kind.name for kind in DocumentKind} == {
        "COURSE",
        "QA_SHEET",
        "REVISION_SHEET",
        "TECHNICAL",
    }


def test_failure_reason_carries_code_message_and_retryable() -> None:
    reason = FailureReason(code="BAD_PDF", message="illisible", retryable=False)
    assert (reason.code, reason.message, reason.retryable) == (
        "BAD_PDF",
        "illisible",
        False,
    )
    with pytest.raises(FrozenInstanceError):
        reason.retryable = True  # type: ignore[misc]


def test_failure_reason_rejects_a_blank_code() -> None:
    with pytest.raises(ValueError, match="code"):
        FailureReason(code="", message="x", retryable=True)

"""Identité de la politique dans les identifiants et le ``chunk_hash`` (revue
S3) : ``ChunkingPolicy.identity`` = ``version+tokenizer``.

Deux politiques qui ne diffèrent que par le tokenizer produisent des lots
distincts. Les valeurs littérales épinglent la formule des UUIDv5 : un
changement de l'espace de noms, du format de la chaîne nommée ou de l'identité
les ferait changer, et avec eux tous les identifiants déjà persistés et les
points Qdrant. Elles ont été calculées une fois, le 2026-11-18, avec
``VERSION_ID = UUID(int=22)`` et l'identité ``chunking-test-1+test-words-v1`` :

- ``unit_id`` : ``uuid5(NS, "unit:00000000-0000-0000-0000-000000000016:``
  ``chunking-test-1+test-words-v1:0")`` ;
- ``chunk_id`` : ``uuid5(NS, "chunk:00000000-0000-0000-0000-000000000016:``
  ``chunking-test-1+test-words-v1:0:0")``,

``NS`` étant l'espace de noms de ``identifiers.py``. Ne jamais les recalculer
pour faire passer le code : un changement voulu est une rupture d'identité à
documenter (base de dev à réinitialiser, réindexation).
"""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from tests.unit.knowledge_base.domain.services.support import (
    VERSION_ID,
    doc,
    h,
    p,
    pipeline,
)

DOCUMENT = doc(h(1, "Fiche"), h(2, "Question : Un ?"), p("a b c"))
QA = DocumentKind.QA_SHEET

PINNED_UNIT_ID = UUID("5f928371-8a11-5df6-b372-271d836f7fc7")
PINNED_CHUNK_ID = UUID("ef2b6107-f92e-5bd6-96f1-5f688eceed0b")


def test_two_policies_differing_only_by_the_tokenizer_give_distinct_batches() -> None:
    (unit_a,), (chunk_a,) = pipeline(DOCUMENT, QA, tokenizer="compteur-a")
    (unit_b,), (chunk_b,) = pipeline(DOCUMENT, QA, tokenizer="compteur-b")

    assert chunk_a.contextual_text == chunk_b.contextual_text
    assert unit_a.unit_id != unit_b.unit_id
    assert chunk_a.chunk_id != chunk_b.chunk_id
    assert chunk_a.chunk_hash != chunk_b.chunk_hash


def test_the_identifiers_of_a_known_batch_are_pinned() -> None:
    (unit,), (chunk,) = pipeline(DOCUMENT, QA, version_id=VERSION_ID)

    assert unit.unit_id == PINNED_UNIT_ID
    assert chunk.chunk_id == PINNED_CHUNK_ID

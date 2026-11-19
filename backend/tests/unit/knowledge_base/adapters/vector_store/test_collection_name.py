"""Nom de collection Qdrant : calculé par l'adapter à partir de ``index_id``,
jamais par le domaine (knowledge-base.md §3.2, §3.6).

API figée (``knowledge_base.adapters.gateway.vector_store.qdrant_vector_index``) :
``QdrantVectorIndex.collection_name(index_id: UUID) -> str`` (méthode statique,
déterministe, une collection par index, caractères ``[A-Za-z0-9_-]`` seulement).
"""

from __future__ import annotations

import re
from uuid import UUID

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)

A, B = UUID(int=0xA), UUID(int=0xB)


def test_the_collection_name_is_deterministic() -> None:
    assert QdrantVectorIndex.collection_name(A) == QdrantVectorIndex.collection_name(A)


def test_each_index_has_its_own_collection() -> None:
    assert QdrantVectorIndex.collection_name(A) != QdrantVectorIndex.collection_name(B)


def test_the_collection_name_is_a_valid_qdrant_name_derived_from_the_index_id() -> None:
    name = QdrantVectorIndex.collection_name(A)

    assert re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name)
    assert A.hex in name.replace("-", "")

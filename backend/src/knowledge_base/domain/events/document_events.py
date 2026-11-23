"""Domain Events locaux de ``KnowledgeDocument`` (knowledge-base.md §5, §8, §10).

Collectés par l'agrégat et relus par le handler via ``pull_events()`` ; ils ne
sortent jamais du contexte (005, couche ``domain``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.content_hash import ContentHash


@dataclass(frozen=True)
class DocumentEvent:
    document_id: UUID


@dataclass(frozen=True)
class DocumentImported(DocumentEvent):
    version_id: UUID
    number: int
    content_hash: ContentHash


@dataclass(frozen=True)
class DocumentVersionExtracted(DocumentEvent):
    version_id: UUID


@dataclass(frozen=True)
class DocumentVersionDiscarded(DocumentEvent):
    """Version abandonnée avant publication par l'import d'une plus récente
    (I6) : ses work items et indexations sont à annuler."""

    version_id: UUID
    number: int
    replaced_by_version_id: UUID


@dataclass(frozen=True)
class DocumentVersionPublished(DocumentEvent):
    version_id: UUID
    number: int
    visibility_revision: int
    superseded_version_id: UUID | None


@dataclass(frozen=True)
class DocumentWithdrawn(DocumentEvent):
    visibility_revision: int

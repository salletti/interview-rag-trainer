"""Port de persistance de l'agrégat ``KnowledgeDocument`` (knowledge-base.md
§3.1, §4.1 I2 et I3)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.value_objects.content_hash import ContentHash


class ConcurrentModificationError(Exception):
    """Verrou optimiste perdu : le document a été modifié par une autre
    transaction depuis sa lecture. Le client peut rejouer sa requête."""

    def __init__(self, document_id: UUID) -> None:
        super().__init__(f"document {document_id} modifié par une autre transaction")
        self.document_id = document_id


class ContentHashTakenError(Exception):
    """Une version du document porte une empreinte déjà enregistrée par une
    transaction concurrente, commitée entre la lecture et l'écriture
    (garde-fou ``UNIQUE(content_hash)``, décision O2)."""

    def __init__(self, document_id: UUID) -> None:
        super().__init__(
            f"document {document_id} : empreinte déjà enregistrée par un import "
            "concurrent"
        )
        self.document_id = document_id


class KnowledgeDocumentRepository(Protocol):
    async def get(self, document_id: UUID) -> KnowledgeDocument | None: ...

    async def find_by_content_hash(
        self, content_hash: ContentHash
    ) -> KnowledgeDocument | None:
        """Document dont **une** version, quel que soit son état, porte cette
        empreinte."""
        ...

    async def add(self, document: KnowledgeDocument) -> None:
        """Lève ``ContentHashTakenError`` si l'empreinte est déjà prise."""
        ...

    async def save(self, document: KnowledgeDocument) -> None:
        """Lève ``ConcurrentModificationError`` si le document a changé depuis
        sa lecture, ``ContentHashTakenError`` si une nouvelle version porte une
        empreinte déjà prise."""
        ...

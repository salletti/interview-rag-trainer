"""Port des dépôts de ``RecordExtraction.apply`` et ``fail``, liés à une
transaction ouverte par la work queue (S0 §5)."""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.providers.extraction_repository import (
    ExtractionRepository,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    KnowledgeDocumentRepository,
)


class ExtractionTransaction(Protocol):
    """Dépôts liés à la transaction de finalisation (ou d'échec) en cours ; la
    transaction appartient à l'appelant."""

    @property
    def documents(self) -> KnowledgeDocumentRepository: ...

    @property
    def extractions(self) -> ExtractionRepository: ...

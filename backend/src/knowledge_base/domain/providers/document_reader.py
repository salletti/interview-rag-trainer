"""Port de lecture de l'agrégat pour ``RecordExtraction.prepare`` : une Unit of
Work en lecture seule, sans ``commit``."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from knowledge_base.domain.providers.knowledge_document_repository import (
    KnowledgeDocumentRepository,
)


class DocumentReader(Protocol):
    """Unit of Work en lecture seule : ``prepare`` n'appelle jamais ``commit``."""

    @property
    def documents(self) -> KnowledgeDocumentRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

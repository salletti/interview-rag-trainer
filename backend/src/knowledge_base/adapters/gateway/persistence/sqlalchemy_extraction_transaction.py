"""Dépôts de l'extraction sur la session d'une transaction ouverte par la work
queue (``ctx.finalize`` ou ``fail``, S0 §5) : c'est l'``ExtractionTransaction``
de ``RecordExtraction``. Ne valide ni n'annule jamais : la transaction et son
fencing appartiennent à S0."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.sqlalchemy_extraction_repository import (  # noqa: E501
    SqlAlchemyExtractionRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_document_repository import (  # noqa: E501
    SqlAlchemyKnowledgeDocumentRepository,
)


class SqlAlchemyExtractionTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._documents = SqlAlchemyKnowledgeDocumentRepository(session)
        self._extractions = SqlAlchemyExtractionRepository(session)

    @property
    def documents(self) -> SqlAlchemyKnowledgeDocumentRepository:
        return self._documents

    @property
    def extractions(self) -> SqlAlchemyExtractionRepository:
        return self._extractions

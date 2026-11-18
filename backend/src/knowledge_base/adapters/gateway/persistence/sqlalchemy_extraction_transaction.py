"""Dépôts de l'extraction sur la session d'une transaction ouverte par la work
queue (``ctx.finalize`` ou ``fail``, S0 §5) : c'est l'``ExtractionTransaction``
de ``RecordExtraction``, soit les dépôts de l'indexation (corpus, indexations,
work queue, depuis S4) plus le lot d'unités et de chunks. Ne valide ni
n'annule jamais : la transaction et son fencing appartiennent à S0."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.sqlalchemy_extraction_repository import (  # noqa: E501
    SqlAlchemyExtractionRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_indexing_transaction import (  # noqa: E501
    SqlAlchemyIndexingTransaction,
)


class SqlAlchemyExtractionTransaction(SqlAlchemyIndexingTransaction):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._extractions = SqlAlchemyExtractionRepository(session)

    @property
    def extractions(self) -> SqlAlchemyExtractionRepository:
        return self._extractions

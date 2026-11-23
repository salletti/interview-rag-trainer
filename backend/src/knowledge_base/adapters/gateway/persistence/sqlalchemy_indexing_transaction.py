"""Dépôts de l'indexation et de la publication sur la session d'une
transaction ouverte par la work queue (``ctx.finalize`` ou ``fail``, S0 §5) :
c'est l'``IndexingTransaction`` de ``CompleteIndexation`` et
``PublishDocumentVersion``. Ne valide ni n'annule jamais : la transaction et
son fencing appartiennent à S0."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.sqlalchemy_indexation_repository import (  # noqa: E501
    SqlAlchemyIndexationRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_corpus_repository import (  # noqa: E501
    SqlAlchemyKnowledgeCorpusRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_document_repository import (  # noqa: E501
    SqlAlchemyKnowledgeDocumentRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)


class SqlAlchemyIndexingTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._documents = SqlAlchemyKnowledgeDocumentRepository(session)
        self._corpus = SqlAlchemyKnowledgeCorpusRepository(session)
        self._indexations = SqlAlchemyIndexationRepository(session)
        self._work_queue = SqlAlchemyWorkQueue(session)

    @property
    def documents(self) -> SqlAlchemyKnowledgeDocumentRepository:
        return self._documents

    @property
    def corpus(self) -> SqlAlchemyKnowledgeCorpusRepository:
        return self._corpus

    @property
    def indexations(self) -> SqlAlchemyIndexationRepository:
        return self._indexations

    @property
    def work_queue(self) -> SqlAlchemyWorkQueue:
        return self._work_queue

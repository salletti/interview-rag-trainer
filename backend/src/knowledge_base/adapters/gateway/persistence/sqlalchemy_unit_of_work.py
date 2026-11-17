"""Unit of Work SQLAlchemy de Knowledge Base : une session, donc une
transaction, partagée par le dépôt des documents, la work queue (S0) et les
clés d'idempotence."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.persistence.sqlalchemy_idempotency_key_repository import (  # noqa: E501
    SqlAlchemyIdempotencyKeyRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_document_repository import (  # noqa: E501
    SqlAlchemyKnowledgeDocumentRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)


class SqlAlchemyUnitOfWork:
    """À usage unique : un ``async with`` par instance. La session ne prend
    une connexion qu'à la première requête."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session = session_factory()
        self._documents = SqlAlchemyKnowledgeDocumentRepository(self._session)
        self._work_queue = SqlAlchemyWorkQueue(self._session)
        self._idempotency_keys = SqlAlchemyIdempotencyKeyRepository(self._session)

    @property
    def documents(self) -> SqlAlchemyKnowledgeDocumentRepository:
        return self._documents

    @property
    def work_queue(self) -> SqlAlchemyWorkQueue:
        return self._work_queue

    @property
    def idempotency_keys(self) -> SqlAlchemyIdempotencyKeyRepository:
        return self._idempotency_keys

    async def commit(self) -> None:
        await self._session.commit()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # close() annule la transaction en cours : sortie sans commit ou sur
        # exception = rollback ; sans effet après commit.
        await self._session.close()

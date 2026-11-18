"""Unit of Work SQLAlchemy de Knowledge Base : une session, donc une
transaction, partagée par le dépôt des documents, la work queue (S0), les
clés d'idempotence (S2), le corpus, les indexations et la lecture des chunks
(S4)."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.persistence.sqlalchemy_chunk_reader import (
    SqlAlchemyChunkReader,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_idempotency_key_repository import (  # noqa: E501
    SqlAlchemyIdempotencyKeyRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_indexing_transaction import (  # noqa: E501
    SqlAlchemyIndexingTransaction,
)


class SqlAlchemyUnitOfWork(SqlAlchemyIndexingTransaction):
    """Les dépôts de ``SqlAlchemyIndexingTransaction`` (documents, work queue,
    corpus, indexations) sur une session qu'elle ouvre et valide elle-même,
    plus les clés d'idempotence et la lecture des chunks.

    À usage unique : un ``async with`` par instance. La session ne prend
    une connexion qu'à la première requête."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session = session_factory()
        super().__init__(self._session)
        self._idempotency_keys = SqlAlchemyIdempotencyKeyRepository(self._session)
        self._chunks = SqlAlchemyChunkReader(self._session)

    @property
    def idempotency_keys(self) -> SqlAlchemyIdempotencyKeyRepository:
        return self._idempotency_keys

    @property
    def chunks(self) -> SqlAlchemyChunkReader:
        return self._chunks

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

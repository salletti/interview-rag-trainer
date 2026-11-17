"""Port de l'Unit of Work de Knowledge Base : une transaction partagée par le
dépôt des documents, la work queue et les clés d'idempotence."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyRepository,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    KnowledgeDocumentRepository,
)
from knowledge_base.domain.providers.work_queue import WorkQueue


class UnitOfWork(Protocol):
    """``async with`` ouvre la transaction ; seul ``commit()`` la valide.
    Sortir du bloc sans commit, ou sur exception, annule tout."""

    @property
    def documents(self) -> KnowledgeDocumentRepository: ...

    @property
    def work_queue(self) -> WorkQueue: ...

    @property
    def idempotency_keys(self) -> IdempotencyKeyRepository: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

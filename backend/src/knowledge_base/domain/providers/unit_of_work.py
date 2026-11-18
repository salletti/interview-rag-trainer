"""Port de l'Unit of Work de Knowledge Base : une transaction partagée par le
dépôt des documents, la work queue et les clés d'idempotence, ainsi que le
corpus et les indexations, dont l'import d'une version et le retrait annulent
le travail en cours (S7, I6 ; knowledge-base.md §8 point 1, §10)."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyRepository,
)
from knowledge_base.domain.providers.indexation_repository import (
    IndexationRepository,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    KnowledgeCorpusRepository,
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

    @property
    def corpus(self) -> KnowledgeCorpusRepository: ...

    @property
    def indexations(self) -> IndexationRepository: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

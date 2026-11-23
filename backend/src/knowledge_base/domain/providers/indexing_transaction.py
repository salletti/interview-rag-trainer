"""Ports des transactions de l'indexation et de la publication
(knowledge-base.md §6, §8, §13).

- ``IndexingTransaction`` : dépôts liés à une transaction ouverte par
  l'appelant (finalisation d'un work item ou Unit of Work d'un handler) ;
  ce que reçoivent ``CompleteIndexation.apply``, ``fail`` et
  ``PublishDocumentVersion.apply``.
- ``IndexingUnitOfWork`` : la même transaction, ouverte et validée par le
  handler lui-même (provisionnement, publication directe), avec la lecture des
  chunks.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from knowledge_base.domain.providers.chunk_reader import ChunkReader
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


class IndexingTransaction(Protocol):
    @property
    def documents(self) -> KnowledgeDocumentRepository: ...

    @property
    def corpus(self) -> KnowledgeCorpusRepository: ...

    @property
    def indexations(self) -> IndexationRepository: ...

    @property
    def work_queue(self) -> WorkQueue: ...


class IndexingUnitOfWork(IndexingTransaction, Protocol):
    """``async with`` ouvre la transaction ; seul ``commit()`` la valide."""

    @property
    def chunks(self) -> ChunkReader: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

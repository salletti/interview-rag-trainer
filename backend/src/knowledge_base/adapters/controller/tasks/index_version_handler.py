"""Handler du ``kind`` ``index_version`` : pont entre la work queue (S0 §5) et
``CompleteIndexation`` (knowledge-base.md §6, §9).

1. ``use_case.prepare`` hors transaction : embeddings (dans un thread, par
   l'adapter), upserts ``visible=false`` et vérification du manifest ; la
   boucle prolonge le lease toutes les ``heartbeat_interval``.
2. UN ``ctx.finalize`` dont le callback appelle ``use_case.apply`` (ou, s'il
   n'y a rien à indexer, annule l'indexation devenue sans objet) : l'item doit
   se terminer.

Erreurs :

- ``IndexManifestMismatchError`` (point perdu ou modifié) : transitoire, la
  tentative suivante réécrit les points ;
- ``CollectionMismatchError`` : terminale ``COLLECTION_MISMATCH``,
  l'indexation passe ``FAILED`` ;
- payload illisible : terminale ``INVALID_PAYLOAD`` (aucune indexation
  désignée) ;
- toute autre exception (Qdrant, modèle) : transitoire pour S0 ; à la
  dernière tentative, l'indexation passe ``FAILED``.

L'échec d'une indexation est ``retryable`` : la version reste ``EXTRACTED``
et ``RetryIngestion`` (S8) rouvrira l'indexation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    INGESTION_QUEUE,
    INVALID_PAYLOAD,
)
from knowledge_base.adapters.controller.tasks.lease import keeping_lease
from knowledge_base.adapters.controller.tasks.retry_policy import (
    TerminalWorkError,
    TransientWorkError,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_indexing_transaction import (  # noqa: E501
    SqlAlchemyIndexingTransaction,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.indexing_transaction import IndexingTransaction
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.complete_indexation.complete_indexation_command import (
    CompleteIndexation,
)
from knowledge_base.use_cases.complete_indexation.complete_indexation_handler import (
    CompleteIndexationHandler,
    PreparedIndexation,
)
from knowledge_base.use_cases.complete_indexation.errors import (
    COLLECTION_MISMATCH,
    CollectionMismatchError,
    IndexManifestMismatchError,
)
from knowledge_base.use_cases.import_steps import utc_now
from knowledge_base.use_cases.indexing_steps import INDEX_VERSION
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE: Final = 32

TransactionFactory = Callable[[AsyncSession], IndexingTransaction]


class IndexVersionWorkHandler:
    kind: ClassVar[str] = INDEX_VERSION
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(
        self,
        use_case: CompleteIndexationHandler,
        transaction: TransactionFactory,
        *,
        heartbeat_interval: timedelta | None = None,
    ) -> None:
        """``heartbeat_interval`` : ``None`` désactive les heartbeats (doubles
        de ``WorkContext`` des tests unitaires qui n'en ont pas)."""
        self._use_case = use_case
        self._transaction = transaction
        self._heartbeat_interval = heartbeat_interval

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        command = _command(item.payload)
        try:
            prepared = await keeping_lease(
                ctx, self._use_case.prepare(command), self._heartbeat_interval
            )
        except IndexManifestMismatchError as error:
            raise TransientWorkError(str(error)) from error
        except CollectionMismatchError as error:
            raise TerminalWorkError(COLLECTION_MISMATCH, str(error)) from error

        async def finalize(session: AsyncSession) -> None:
            await self._finalize(session, command, prepared)

        await ctx.finalize(finalize)

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        try:
            command = _command(item.payload)
        except TerminalWorkError:
            logger.warning(
                "work item %s : payload illisible, aucun échec d'indexation à "
                "enregistrer (%s)",
                item.id,
                error.code,
            )
            return
        reason = FailureReason(code=error.code, message=error.message, retryable=True)
        await self._use_case.fail(self._transaction(session), command, reason)

    async def _finalize(
        self,
        session: AsyncSession,
        command: CompleteIndexation,
        prepared: PreparedIndexation | None,
    ) -> None:
        transaction = self._transaction(session)
        if prepared is None:
            await self._use_case.cancel_if_obsolete(transaction, command)
        else:
            await self._use_case.apply(transaction, prepared)


def _command(payload: Mapping[str, str]) -> CompleteIndexation:
    try:
        return CompleteIndexation(
            document_id=UUID(payload["document_id"]),
            version_id=UUID(payload["version_id"]),
            index_id=UUID(payload["index_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD, f"payload index_version illisible : {dict(payload)!r}"
        ) from error


def build_index_version_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    embedder: Embedder,
    vector_index: VectorIndex,
    clock: Callable[[], datetime] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IndexVersionWorkHandler:
    """Composition de production ; ne se connecte à rien et ne charge pas le
    modèle (l'``Embedder`` du worker est paresseux)."""
    clock = clock or utc_now

    def unit_of_work() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    use_case = CompleteIndexationHandler(
        unit_of_work=unit_of_work,
        embedder=embedder,
        vector_index=vector_index,
        publisher=PublishDocumentVersionHandler(unit_of_work=unit_of_work, clock=clock),
        clock=clock,
        batch_size=batch_size,
    )
    return IndexVersionWorkHandler(
        use_case=use_case,
        transaction=SqlAlchemyIndexingTransaction,
        heartbeat_interval=settings.heartbeat_interval,
    )

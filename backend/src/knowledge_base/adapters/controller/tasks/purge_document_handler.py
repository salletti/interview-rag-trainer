"""Handler du ``kind`` ``purge_document`` : pont entre la work queue (S0 §5)
et ``PurgeDocument`` (knowledge-base.md §6, §10 ; ADR 0012 point 6).

Ordre du point 6 de l'ADR 0012 (points, chunks, blobs) :

1. ``use_case.prepare`` hors transaction : points Qdrant du document dans
   chaque index vivant ; un heartbeat refusé l'interrompt (``keeping_lease``) ;
2. UN ``ctx.finalize`` dont le callback appelle ``use_case.apply`` (chunks,
   unités, ``purged_at``) ; s'il n'y a rien à purger (document inconnu ou
   actif), la finalisation n'écrit rien, mais l'item se termine ;
3. une fois la finalisation commitée, et seulement alors,
   ``use_case.erase_unheld_blobs`` : blobs qu'aucun document non purgé ne
   porte. Une tentative périmée (``StaleAttempt`` levée par ``finalize``) n'y
   arrive jamais. Étape au mieux, qui ne lève pas : l'item est déjà
   ``SUCCEEDED``, une exception ici serait traitée comme l'échec d'un item
   terminé ; un fichier restant est repris par ``CleanBlobOrphans``.

Pourquoi une étape après le commit plutôt qu'un item ``delete_blob`` : la
suppression d'un fichier qu'aucun document ne porte n'a pas besoin d'une
garantie durable propre, ``CleanBlobOrphans`` en est déjà le filet (même
règle, ``holders`` vide) ; un nouveau ``kind`` ajouterait clé, handler et
file pour la même garantie.

Erreurs : payload illisible → terminale ``INVALID_PAYLOAD`` ; toute autre
erreur des étapes 1 et 2 (Qdrant, PostgreSQL) est transitoire (retry avec
backoff), sans finalisation. Aucun état métier n'enregistre l'échec : l'item
``FAILED`` reste visible en base (réouverture et réconciliation : S8).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    INGESTION_QUEUE,
    INVALID_PAYLOAD,
)
from knowledge_base.adapters.controller.tasks.lease import keeping_lease
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_blob_references import (
    SqlAlchemyBlobReferences,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_document_purger import (
    SqlAlchemyPurgeTransaction,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.providers.document_purger import PurgeTransaction
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.import_steps import utc_now
from knowledge_base.use_cases.indexing_steps import PURGE_DOCUMENT
from knowledge_base.use_cases.purge_document.purge_document_command import (
    PurgeDocument,
)
from knowledge_base.use_cases.purge_document.purge_document_handler import (
    PurgeDocumentHandler,
)

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )

TransactionFactory = Callable[[AsyncSession], PurgeTransaction]


class PurgeDocumentWorkHandler:
    kind: ClassVar[str] = PURGE_DOCUMENT
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(
        self,
        use_case: PurgeDocumentHandler,
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
        prepared = await keeping_lease(
            ctx, self._use_case.prepare(command), self._heartbeat_interval
        )

        async def finalize(session: AsyncSession) -> None:
            if prepared is not None:
                await self._use_case.apply(self._transaction(session), prepared)

        await ctx.finalize(finalize)
        if prepared is not None:
            await self._use_case.erase_unheld_blobs(prepared)


def _command(payload: Mapping[str, str]) -> PurgeDocument:
    try:
        return PurgeDocument(document_id=UUID(payload["document_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD, f"payload purge_document illisible : {dict(payload)!r}"
        ) from error


def build_purge_document_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    vector_index: VectorIndex,
    clock: Callable[[], datetime] | None = None,
) -> PurgeDocumentWorkHandler:
    """Composition de production (volume ``settings.blob_root``) ; ne se
    connecte à rien."""
    return PurgeDocumentWorkHandler(
        use_case=PurgeDocumentHandler(
            unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
            vector_index=vector_index,
            blob_eraser=FileSystemBlobStore(settings.blob_root),
            blob_references=SqlAlchemyBlobReferences(session_factory),
            clock=clock or utc_now,
        ),
        transaction=SqlAlchemyPurgeTransaction,
        heartbeat_interval=settings.heartbeat_interval,
    )

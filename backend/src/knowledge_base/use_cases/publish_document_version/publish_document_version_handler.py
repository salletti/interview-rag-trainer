"""``PublishDocumentVersion`` : bascule SQL qui rend une version visible
(knowledge-base.md §8, §10, §13 ; I4, I5, I9).

Gardes, dans la transaction de publication :

1. l'index actif est lu sous ``FOR SHARE`` de la ligne du corpus : une
   activation concurrente (``FOR UPDATE``) attend la fin de la publication, ou
   la publication attend l'activation et voit le nouvel index ;
2. I9 : l'``Indexation`` de la version dans cet index est ``COMPLETED`` ;
3. l'agrégat : version ``EXTRACTED``, document ``ACTIVE``, I5.

La publication planifie ``sync_visibility`` dans la même transaction : les
points restent ``visible=false`` jusqu'à la synchronisation (§11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.providers.indexing_transaction import (
    IndexingTransaction,
    IndexingUnitOfWork,
)
from knowledge_base.use_cases.errors import DocumentNotFoundError
from knowledge_base.use_cases.indexing_steps import schedule_visibility_sync
from knowledge_base.use_cases.publish_document_version.errors import (
    IndexationNotCompletedError,
    NoActiveIndexError,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_command import (  # noqa: E501
    PublishDocumentVersion,
)

logger = logging.getLogger(__name__)


class PublishDocumentVersionHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], IndexingUnitOfWork],
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        # Horodatage du journal de publication : l'agrégat ne date pas ses
        # transitions (S1), la base n'a pas de colonne de date de publication.
        self._clock = clock

    async def handle(self, command: PublishDocumentVersion) -> None:
        """Publication dans sa propre transaction ; rien n'est validé si elle
        est refusée."""
        async with self._unit_of_work() as uow:
            await self.apply(uow, command)
            await uow.commit()

    async def apply(
        self, transaction: IndexingTransaction, command: PublishDocumentVersion
    ) -> None:
        """Publie dans la transaction fournie, sans jamais la valider. Lève
        ``PublicationRefusedError`` ou ``KnowledgeDocumentError`` avant toute
        écriture."""
        corpus = await transaction.corpus.get_shared()
        active = None if corpus is None else corpus.active_index()
        if active is None:
            raise NoActiveIndexError(command.version_id)
        indexation = await transaction.indexations.get(
            command.version_id, active.index_id
        )
        if indexation is None or indexation.status is not IndexationStatus.COMPLETED:
            raise IndexationNotCompletedError(command.version_id, active.index_id)
        document = await transaction.documents.get(command.document_id)
        if document is None:
            raise DocumentNotFoundError(command.document_id)
        document.publish(command.version_id)
        await transaction.documents.save(document)
        await schedule_visibility_sync(
            transaction.work_queue, document.document_id, document.visibility_revision
        )
        logger.info(
            "version %s du document %s publiée à %s (révision de visibilité %d, "
            "index %s)",
            command.version_id,
            command.document_id,
            self._clock().isoformat(),
            document.visibility_revision,
            active.index_id,
        )

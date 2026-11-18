"""``WithdrawDocument`` : retrait logique immédiat d'un document
(knowledge-base.md §5.1, §10 ligne « Retrait » ; I12 ; ADR 0010 point 8).

Une seule transaction :

1. ``KnowledgeDocument.withdraw()`` : ``WITHDRAWN``, ``visibility_revision``
   + 1 ; les versions gardent leur état (§5.2). Dès le commit, le contrôle SQL
   de chaque recherche écarte le document (I12) : aucune synchronisation
   Qdrant n'est planifiée, la purge supprime les points ;
2. travail en cours annulé : item ``extract_version`` de chaque version non
   terminale ; dans chaque index vivant, item ``index_version`` de chaque
   version et indexation ``PENDING`` ou ``FAILED`` -> ``CANCELLED`` ;
3. item ``purge_document`` (suppression physique asynchrone, ADR 0012).

Un document déjà retiré est refusé (``DocumentWithdrawnError``) : la révision
n'avance pas et aucune seconde purge n'est planifiée. Un verrou optimiste
perdu (import de version concurrent) est rejoué une fois sur l'état à jour,
pour que la version créée entre-temps n'échappe pas à l'annulation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.use_cases.errors import DocumentNotFoundError
from knowledge_base.use_cases.import_steps import extract_version_key
from knowledge_base.use_cases.indexing_steps import cancel_indexing, schedule_purge
from knowledge_base.use_cases.withdraw_document.withdraw_document_command import (
    WithdrawDocument,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_result import (
    WithdrawDocumentResult,
)

logger = logging.getLogger(__name__)

# Versions dont l'extraction peut encore être en cours ou à venir.
_EXTRACTION_IN_PLAY: Final = frozenset(
    {VersionStatus.RECEIVED, VersionStatus.EXTRACTED, VersionStatus.FAILED}
)


class WithdrawDocumentHandler:
    def __init__(self, *, unit_of_work: Callable[[], UnitOfWork]) -> None:
        self._unit_of_work = unit_of_work

    async def handle(self, command: WithdrawDocument) -> WithdrawDocumentResult:
        try:
            return await self._withdraw(command)
        except ConcurrentModificationError as race:
            logger.info("retrait concurrent d'une écriture, relecture : %s", race)
            return await self._withdraw(command)

    async def _withdraw(self, command: WithdrawDocument) -> WithdrawDocumentResult:
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(command.document_id)
            if document is None:
                raise DocumentNotFoundError(command.document_id)
            document.withdraw()
            await uow.documents.save(document)
            for version in document.versions:
                if version.status in _EXTRACTION_IN_PLAY:
                    await uow.work_queue.cancel(extract_version_key(version.version_id))
            await cancel_indexing(uow, [v.version_id for v in document.versions])
            await schedule_purge(uow.work_queue, document.document_id)
            await uow.commit()
        logger.info(
            "document %s retiré (révision %d), purge planifiée",
            document.document_id,
            document.visibility_revision,
        )
        return WithdrawDocumentResult(
            document_id=document.document_id,
            visibility_revision=document.visibility_revision,
        )

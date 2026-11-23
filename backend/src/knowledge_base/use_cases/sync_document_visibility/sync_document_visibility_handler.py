"""``SyncDocumentVisibility`` : convergence du drapeau ``visible`` de Qdrant
vers l'état SQL courant d'un document (knowledge-base.md §6, §10, §11 ;
ADR 0010 §4, §5, §7).

Dans chaque index non ``RETIRED`` :

- document ``WITHDRAWN`` : tous ses points sont supprimés ;
- sinon : les points de la version publiée passent ``visible=true`` et ceux
  des versions ``SUPERSEDED`` ou ``DISCARDED`` sont supprimés. Une version
  ``EXTRACTED`` n'est **pas** touchée : son indexation peut être en cours, ses
  points sont encore ``visible=false`` (précision S4 de §11).

Idempotente par construction : rejouée, ou exécutée avec une révision
périmée, elle produit le même état. Aucune écriture PostgreSQL ; la lecture
est fermée avant le premier appel Qdrant. PostgreSQL reste l'arbitre de la
visibilité : ce drapeau ne sert qu'à filtrer avant le top-k (§12).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.providers.indexing_transaction import IndexingUnitOfWork
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.value_objects.indexed_point import PointSelector
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_command import (  # noqa: E501
    SyncDocumentVisibility,
)

logger = logging.getLogger(__name__)

# Versions dont les points n'ont plus de raison d'être : elles ne seront plus
# jamais publiées (états terminaux, §5.2).
_OBSOLETE_VERSIONS: Final = frozenset(
    {VersionStatus.SUPERSEDED, VersionStatus.DISCARDED}
)


class SyncDocumentVisibilityHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], IndexingUnitOfWork],
        vector_index: VectorIndex,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._vector_index = vector_index

    async def handle(self, command: SyncDocumentVisibility) -> None:
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(command.document_id)
            corpus = await uow.corpus.get_shared()
        if document is None:
            logger.info(
                "synchronisation du document %s ignorée : document inconnu",
                command.document_id,
            )
            return
        indexes = () if corpus is None else corpus.live_indexes()
        for index in indexes:
            await self._converge(index.index_id, document)
        logger.info(
            "visibilité du document %s synchronisée (révision courante %d, "
            "demandée %d) dans %d index",
            document.document_id,
            document.visibility_revision,
            command.visibility_revision,
            len(indexes),
        )

    async def _converge(self, index_id: UUID, document: KnowledgeDocument) -> None:
        document_id = document.document_id
        if document.status is DocumentStatus.WITHDRAWN:
            await self._vector_index.delete_points(index_id, PointSelector(document_id))
            return
        published = document.published_version_id
        if published is not None:
            await self._vector_index.set_visibility(
                index_id, document_id=document_id, version_id=published, visible=True
            )
        for version in document.versions:
            if version.status in _OBSOLETE_VERSIONS:
                await self._vector_index.delete_points(
                    index_id, PointSelector(document_id, version.version_id)
                )

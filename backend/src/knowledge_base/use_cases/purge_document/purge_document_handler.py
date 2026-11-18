"""``PurgeDocument`` : suppression physique des données d'un document retiré
(knowledge-base.md §6 ligne ``purge_document``, §7.3, §10 ; ADR 0012 point 6).

Coupé en deux pour la work queue (S0 §5), comme ``CompleteIndexation`` :

- ``prepare``, hors transaction (lecture brève, fermée avant tout effet
  externe) : rien si le document est inconnu ou n'est pas ``WITHDRAWN`` (une
  purge ne détruit jamais un document actif) ; sinon, dans cet ordre,
  1. les points du document dans chaque index vivant (``ACTIVE``,
     ``BUILDING``) ; ceux d'un index ``RETIRED`` partent avec sa collection
     (S9) ;
  2. le blob de chaque version, sauf s'il est encore porté par un autre
     document non purgé : les versions du document purgé (tombstones) ne
     retiennent jamais leur propre blob.
  Une panne interrompt les étapes suivantes et remonte : l'item est rejoué,
  chaque étape étant répétable (suppression par filtre, fichier absent = rien
  à faire). Les points d'abord : tant qu'ils existent, rien d'autre ne
  disparaît.
- ``apply``, dans la transaction de finalisation (sous fencing) : chunks et
  unités supprimés, date de purge enregistrée. Les lignes document et
  versions restent (tombstones : citations figées des consommateurs).

Écart d'ordre avec §6 et §10 (points, chunks, puis blobs) : les chunks ne
sont supprimés qu'à la finalisation, après les blobs, pour que leur
suppression et ``purged_at`` soient protégés par le fencing (précision S7 de
§10).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
)
from knowledge_base.domain.providers.blob_references import BlobReferences
from knowledge_base.domain.providers.document_blob_eraser import DocumentBlobEraser
from knowledge_base.domain.providers.document_purger import PurgeTransaction
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.indexed_point import PointSelector
from knowledge_base.use_cases.purge_document.purge_document_command import (
    PurgeDocument,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedPurge:
    """Effets externes faits : il reste la finalisation SQL."""

    document_id: UUID


class PurgeDocumentHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        vector_index: VectorIndex,
        blob_eraser: DocumentBlobEraser,
        blob_references: BlobReferences,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._vector_index = vector_index
        self._blob_eraser = blob_eraser
        self._blob_references = blob_references
        self._clock = clock

    # --- hors transaction -------------------------------------------------------

    async def prepare(self, command: PurgeDocument) -> PreparedPurge | None:
        """``None``, sans aucun effet, s'il n'y a rien à purger."""
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(command.document_id)
            corpus = await uow.corpus.get_shared()
        if document is None or document.status is not DocumentStatus.WITHDRAWN:
            logger.info(
                "purge du document %s ignorée : document inconnu ou non retiré",
                command.document_id,
            )
            return None
        for index in () if corpus is None else corpus.live_indexes():
            await self._vector_index.delete_points(
                index.index_id, PointSelector(document.document_id)
            )
        deleted = 0
        for content_hash in _content_hashes(document):
            if await self._erase_unless_held_elsewhere(document, content_hash):
                deleted += 1
        logger.info(
            "purge du document %s : points supprimés, %d original(aux) supprimé(s)",
            document.document_id,
            deleted,
        )
        return PreparedPurge(document_id=document.document_id)

    async def _erase_unless_held_elsewhere(
        self, document: KnowledgeDocument, content_hash: ContentHash
    ) -> bool:
        """Vrai si un fichier a été supprimé."""
        others = await self._blob_references.holders(content_hash) - {
            document.document_id
        }
        if others:
            logger.info(
                "original %s conservé : encore porté par %d autre(s) document(s)",
                content_hash.value,
                len(others),
            )
            return False
        return await self._blob_eraser.delete(content_hash)

    # --- transaction de finalisation ----------------------------------------------

    async def apply(
        self, transaction: PurgeTransaction, prepared: PreparedPurge
    ) -> None:
        """Ne valide jamais la transaction."""
        await transaction.purger.delete_derived_data(
            prepared.document_id, purged_at=self._clock()
        )


def _content_hashes(document: KnowledgeDocument) -> tuple[ContentHash, ...]:
    return tuple(dict.fromkeys(v.source.content_hash for v in document.versions))

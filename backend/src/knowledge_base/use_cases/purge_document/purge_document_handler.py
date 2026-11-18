"""``PurgeDocument`` : suppression physique des données d'un document retiré
(knowledge-base.md §6 ligne ``purge_document``, §7.3, §10 ; ADR 0012 point 6).

Ordre du point 6 de l'ADR 0012 : points, puis chunks, puis blobs. Trois
étapes, autour de la transaction de finalisation du work item (S0 §5) :

- ``prepare``, hors transaction (lecture brève, fermée avant tout effet
  externe) : rien si le document est inconnu ou n'est pas ``WITHDRAWN`` (une
  purge ne détruit jamais un document actif) ; sinon les points du document
  dans chaque index vivant (``ACTIVE``, ``BUILDING``) ; ceux d'un index
  ``RETIRED`` partent avec sa collection (S9). Une panne remonte : l'item est
  rejoué (suppression par filtre, répétable) et rien d'autre ne disparaît.
- ``apply``, dans la transaction de finalisation (sous fencing) : chunks et
  unités supprimés, date de purge enregistrée. Les lignes document et
  versions restent (tombstones : citations figées des consommateurs).
- ``erase_unheld_blobs``, après le commit de cette transaction : le blob de
  chaque version, sauf s'il est encore porté par un document non purgé. Au
  mieux : un échec est journalisé, jamais remonté (l'item est déjà
  ``SUCCEEDED``) ; ``purged_at`` étant posée, ``BlobReferences.holders`` ne
  compte plus ce document et ``CleanBlobOrphans`` reprend le fichier après le
  délai de grâce.
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
    """Points supprimés : il reste la finalisation SQL, puis les blobs."""

    document_id: UUID
    content_hashes: tuple[ContentHash, ...]


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
        logger.info("purge du document %s : points supprimés", document.document_id)
        return PreparedPurge(
            document_id=document.document_id,
            content_hashes=_content_hashes(document),
        )

    # --- transaction de finalisation ----------------------------------------------

    async def apply(
        self, transaction: PurgeTransaction, prepared: PreparedPurge
    ) -> None:
        """Ne valide jamais la transaction."""
        await transaction.purger.delete_derived_data(
            prepared.document_id, purged_at=self._clock()
        )

    # --- après le commit de la finalisation -----------------------------------------

    async def erase_unheld_blobs(self, prepared: PreparedPurge) -> None:
        """À n'appeler qu'une fois ``apply`` commité. Ne lève jamais."""
        deleted = 0
        for content_hash in prepared.content_hashes:
            try:
                if await self._erase_unless_held(content_hash):
                    deleted += 1
            except Exception:
                # Large à dessein : volume ou PostgreSQL, l'item est déjà
                # SUCCEEDED et ne serait pas rejoué ; le fichier, que plus
                # aucun document non purgé ne porte, est un orphelin
                # ordinaire pour CleanBlobOrphans.
                logger.warning(
                    "purge du document %s : original %s non supprimé, laissé "
                    "au nettoyage des orphelins",
                    prepared.document_id,
                    content_hash.value,
                    exc_info=True,
                )
        logger.info(
            "purge du document %s : %d original(aux) supprimé(s)",
            prepared.document_id,
            deleted,
        )

    async def _erase_unless_held(self, content_hash: ContentHash) -> bool:
        """Vrai si un fichier a été supprimé.

        Défense en profondeur : inatteignable tant que ``UNIQUE(content_hash)``
        est global (O2), aucun autre document ne pouvant porter l'empreinte ;
        le contrôle tient si la contrainte était levée. Après le commit,
        ``purged_at`` est posée : le document purgé ne se retient pas
        lui-même. Pour la même raison (O2), la suppression est sans
        condition : un import du même contenu est refusé (le tombstone garde
        l'empreinte) et son dépôt n'est référencé par aucune version."""
        holders = await self._blob_references.holders(content_hash)
        if holders:
            logger.info(
                "original %s conservé : encore porté par %d document(s)",
                content_hash.value,
                len(holders),
            )
            return False
        return await self._blob_eraser.delete(content_hash)


def _content_hashes(document: KnowledgeDocument) -> tuple[ContentHash, ...]:
    return tuple(dict.fromkeys(v.source.content_hash for v in document.versions))

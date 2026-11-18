"""Items de la work queue de l'indexation, de la visibilité et de la purge
(knowledge-base.md §6, §10, §11) : ``kind``, clé de dédoublonnage et payload,
au même endroit pour le producteur (use case) et le consommateur (handler) ;
leur replanification sous une génération suivante (S8, ``reschedule_*``) ; et
l'annulation du travail d'indexation d'une version abandonnée ou d'un
document retiré (S7 ; I6, §5.3, §8 point 1), générations comprises.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.providers.indexing_transaction import IndexingTransaction
from knowledge_base.domain.providers.work_queue import WorkQueue

INDEX_VERSION: Final = "index_version"
SYNC_VISIBILITY: Final = "sync_visibility"
PURGE_DOCUMENT: Final = "purge_document"

# Une indexation COMPLETED reste un fait : aucune transition n'en part (§5.3).
_CANCELLABLE_INDEXATIONS: Final = frozenset(
    {IndexationStatus.PENDING, IndexationStatus.FAILED}
)


def index_version_key(version_id: UUID, index_id: UUID) -> str:
    return f"{INDEX_VERSION}:{version_id}:{index_id}"


def sync_visibility_key(document_id: UUID, visibility_revision: int) -> str:
    return f"{SYNC_VISIBILITY}:{document_id}:{visibility_revision}"


def _indexation_payload(
    document_id: UUID, version_id: UUID, index_id: UUID
) -> dict[str, str]:
    return {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "index_id": str(index_id),
    }


def _sync_payload(document_id: UUID, visibility_revision: int) -> dict[str, str]:
    return {
        "document_id": str(document_id),
        "visibility_revision": str(visibility_revision),
    }


async def schedule_indexation(
    work_queue: WorkQueue, document_id: UUID, version_id: UUID, index_id: UUID
) -> None:
    await work_queue.enqueue(
        INDEX_VERSION,
        index_version_key(version_id, index_id),
        _indexation_payload(document_id, version_id, index_id),
    )


async def reschedule_indexation(
    work_queue: WorkQueue, document_id: UUID, version_id: UUID, index_id: UUID
) -> None:
    """Indexation à refaire (S8 : manifest qui ne se vérifie plus, indexation
    manquante) : génération suivante de la clé, sauf item déjà en attente."""
    await work_queue.enqueue_again(
        INDEX_VERSION,
        index_version_key(version_id, index_id),
        _indexation_payload(document_id, version_id, index_id),
    )


async def schedule_visibility_sync(
    work_queue: WorkQueue, document_id: UUID, visibility_revision: int
) -> None:
    await work_queue.enqueue(
        SYNC_VISIBILITY,
        sync_visibility_key(document_id, visibility_revision),
        _sync_payload(document_id, visibility_revision),
    )


async def reschedule_visibility_sync(
    work_queue: WorkQueue, document_id: UUID, visibility_revision: int
) -> None:
    """Synchronisation à refaire sans nouvelle révision (S8 : points d'une
    version publiée réécrits invisibles par une réindexation ou une tentative
    zombie) : génération suivante de la clé de la révision courante."""
    await work_queue.enqueue_again(
        SYNC_VISIBILITY,
        sync_visibility_key(document_id, visibility_revision),
        _sync_payload(document_id, visibility_revision),
    )


def purge_document_key(document_id: UUID) -> str:
    return f"{PURGE_DOCUMENT}:{document_id}"


async def schedule_purge(work_queue: WorkQueue, document_id: UUID) -> None:
    await work_queue.enqueue(
        PURGE_DOCUMENT,
        purge_document_key(document_id),
        {"document_id": str(document_id)},
    )


async def reschedule_purge(work_queue: WorkQueue, document_id: UUID) -> None:
    """Purge à refaire après une purge réussie (S8 : points écrits par une
    tentative zombie après la purge) : ``purge_document:<id>:<n>``, la clé de
    base restant celle de la purge d'origine, terminée."""
    await work_queue.enqueue_again(
        PURGE_DOCUMENT,
        purge_document_key(document_id),
        {"document_id": str(document_id)},
    )


async def cancel_indexing(
    transaction: IndexingTransaction, version_ids: Collection[UUID]
) -> None:
    """Dans chaque index vivant (lu sous ``FOR SHARE`` du corpus) : l'item
    ``index_version`` de chaque version est annulé (fencing : une tentative en
    cours ne pourra plus finaliser) et son indexation ``PENDING`` ou
    ``FAILED`` passe ``CANCELLED``. Dans la transaction de l'appelant."""
    if not version_ids:
        return
    corpus = await transaction.corpus.get_shared()
    for index in () if corpus is None else corpus.live_indexes():
        for version_id in version_ids:
            await transaction.work_queue.cancel(
                index_version_key(version_id, index.index_id)
            )
            indexation = await transaction.indexations.get(version_id, index.index_id)
            if indexation is not None and (
                indexation.status in _CANCELLABLE_INDEXATIONS
            ):
                indexation.cancel()
                await transaction.indexations.save(indexation)

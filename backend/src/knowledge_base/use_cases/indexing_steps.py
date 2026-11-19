"""Items de la work queue de l'indexation, de la visibilité, de la purge et de
la migration d'index (knowledge-base.md §6, §10, §11, §13) : ``kind``, clé de
dédoublonnage et payload, au même endroit pour le producteur (use case) et le
consommateur (handler) ;
leur replanification sous une génération suivante (S8, ``reschedule_*``) ; et
l'annulation du travail d'indexation d'une version abandonnée ou d'un
document retiré (S7 ; I6, §5.3, §8 point 1), ou d'un index dont la migration
est abandonnée (revue S9), générations comprises.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta
from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.providers.indexing_transaction import IndexingTransaction
from knowledge_base.domain.providers.work_queue import WorkQueue

INDEX_VERSION: Final = "index_version"
SYNC_VISIBILITY: Final = "sync_visibility"
PURGE_DOCUMENT: Final = "purge_document"
# S9 : lot de chunks d'une version pour un index d'une autre politique, et
# suppression de la collection d'un index retiré.
RECHUNK_VERSION: Final = "rechunk_version"
DROP_INDEX_COLLECTION: Final = "drop_index_collection"

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


def rechunk_version_key(version_id: UUID, index_id: UUID) -> str:
    return f"{RECHUNK_VERSION}:{version_id}:{index_id}"


async def schedule_rechunk(
    work_queue: WorkQueue, document_id: UUID, version_id: UUID, index_id: UUID
) -> None:
    """Le lot de la politique de l'index manque : il est produit d'abord
    (re-parsing de l'original, O4), puis ``rechunk_version`` planifie
    ``index_version``."""
    await work_queue.enqueue(
        RECHUNK_VERSION,
        rechunk_version_key(version_id, index_id),
        _indexation_payload(document_id, version_id, index_id),
    )


async def reschedule_rechunk(
    work_queue: WorkQueue, document_id: UUID, version_id: UUID, index_id: UUID
) -> None:
    """Rechunking à refaire (réconciliation S9 : indexation perdue dans un
    index ``BUILDING``) : génération suivante, sauf item déjà en attente."""
    await work_queue.enqueue_again(
        RECHUNK_VERSION,
        rechunk_version_key(version_id, index_id),
        _indexation_payload(document_id, version_id, index_id),
    )


def drop_index_collection_key(index_id: UUID) -> str:
    return f"{DROP_INDEX_COLLECTION}:{index_id}"


async def schedule_collection_drop(
    work_queue: WorkQueue, index_id: UUID, *, delay: timedelta
) -> None:
    """Suppression de la collection d'un index retiré, disponible après
    ``delay`` : une recherche commencée avant la bascule peut encore
    interroger l'ancienne collection."""
    await work_queue.enqueue(
        DROP_INDEX_COLLECTION,
        drop_index_collection_key(index_id),
        {"index_id": str(index_id)},
        delay=delay,
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
    zombie) : génération suivante de la clé de la révision courante.

    Un item ``PENDING`` est réutilisé (il lira l'état après les points déjà
    écrits), jamais un item ``RUNNING`` (revue S9) : il a pu converger
    l'index avant l'écriture des points et les laisser invisibles, sans que
    l'activation d'un index ``BUILDING`` puisse le voir. Deux
    synchronisations du même document peuvent alors se chevaucher : elles
    sont idempotentes et convergent vers le même état."""
    await work_queue.enqueue_again(
        SYNC_VISIBILITY,
        sync_visibility_key(document_id, visibility_revision),
        _sync_payload(document_id, visibility_revision),
        reuse_running=False,
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


async def cancel_index_work(
    work_queue: WorkQueue, version_id: UUID, index_id: UUID
) -> None:
    """Annule (fencing) les items ``index_version`` et ``rechunk_version``
    d'une version dans un index, générations comprises (abandon d'une
    migration, revue S9)."""
    await work_queue.cancel(index_version_key(version_id, index_id))
    await work_queue.cancel(rechunk_version_key(version_id, index_id))


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

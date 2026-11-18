"""Items de la work queue de l'indexation et de la visibilité
(knowledge-base.md §6, §11) : ``kind``, clé de dédoublonnage et payload, au
même endroit pour le producteur (use case) et le consommateur (handler).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from knowledge_base.domain.providers.work_queue import WorkQueue

INDEX_VERSION: Final = "index_version"
SYNC_VISIBILITY: Final = "sync_visibility"


def index_version_key(version_id: UUID, index_id: UUID) -> str:
    return f"{INDEX_VERSION}:{version_id}:{index_id}"


def sync_visibility_key(document_id: UUID, visibility_revision: int) -> str:
    return f"{SYNC_VISIBILITY}:{document_id}:{visibility_revision}"


async def schedule_indexation(
    work_queue: WorkQueue, document_id: UUID, version_id: UUID, index_id: UUID
) -> None:
    await work_queue.enqueue(
        INDEX_VERSION,
        index_version_key(version_id, index_id),
        {
            "document_id": str(document_id),
            "version_id": str(version_id),
            "index_id": str(index_id),
        },
    )


async def schedule_visibility_sync(
    work_queue: WorkQueue, document_id: UUID, visibility_revision: int
) -> None:
    await work_queue.enqueue(
        SYNC_VISIBILITY,
        sync_visibility_key(document_id, visibility_revision),
        {
            "document_id": str(document_id),
            "visibility_revision": str(visibility_revision),
        },
    )

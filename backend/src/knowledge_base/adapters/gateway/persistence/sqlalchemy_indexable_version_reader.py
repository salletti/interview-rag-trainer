"""Versions à couvrir par un index (S9 ; port ``IndexableVersionReader``),
lues dans la session de la transaction qui tient le corpus ``FOR UPDATE``.

Deux requêtes, quel que soit le nombre de versions : les versions
``PUBLISHED``/``EXTRACTED`` des documents ``ACTIVE`` avec les politiques de
leurs lots (``knowledge_units``, index unique ``(version_id,
chunking_policy_version, ordinal)``) ; les documents qui ont une
synchronisation de visibilité vivante (index ``(kind, status)`` de
``work_items``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    PENDING,
    RUNNING,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.providers.index_migration import IndexableVersion
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_versions,
    documents,
    knowledge_units,
    work_items,
)
from knowledge_base.use_cases.indexing_steps import SYNC_VISIBILITY

_v = document_versions.c
_d = documents.c
_u = knowledge_units.c
_w = work_items.c

_INDEXABLE: Final = (VersionStatus.PUBLISHED.value, VersionStatus.EXTRACTED.value)


class SqlAlchemyIndexableVersionReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_indexable_versions(self) -> Sequence[IndexableVersion]:
        policies = func.array_remove(
            func.array_agg(_u.chunking_policy_version.distinct()), None
        )
        rows = await self._session.execute(
            select(_v.id, _v.document_id, _v.status, policies.label("policies"))
            .join(documents, _d.id == _v.document_id)
            .outerjoin(knowledge_units, _u.version_id == _v.id)
            .where(_d.status == DocumentStatus.ACTIVE.value, _v.status.in_(_INDEXABLE))
            .group_by(_v.id, _v.document_id, _v.status)
            .order_by(_v.id)
        )
        return [
            IndexableVersion(
                document_id=row.document_id,
                version_id=row.id,
                version_status=VersionStatus(row.status),
                policy_versions=frozenset(row.policies),
            )
            for row in rows
        ]

    async def documents_awaiting_visibility(self) -> frozenset[UUID]:
        values = await self._session.scalars(
            select(_w.payload["document_id"].astext)
            .where(_w.kind == SYNC_VISIBILITY, _w.status.in_((PENDING, RUNNING)))
            .distinct()
        )
        found: set[UUID] = set()
        for value in values:
            try:
                found.add(UUID(value))
            except (TypeError, ValueError):
                continue  # payload illisible : il ne désigne aucun document
        return frozenset(found)

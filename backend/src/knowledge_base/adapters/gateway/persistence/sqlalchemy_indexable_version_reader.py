"""Versions à couvrir par un index (S9 ; port ``IndexableVersionReader``),
lues dans la session de la transaction qui tient le corpus ``FOR UPDATE``.

Une requête par méthode, quel que soit le nombre de versions : les versions
indexables (prédicat partagé, ``indexable_versions``) avec les politiques de
leurs lots (``knowledge_units``, index unique ``(version_id,
chunking_policy_version, ordinal)``) ; les documents qui ont une
synchronisation de visibilité vivante, ou dont la dernière est ``FAILED``
(index ``(kind, status)`` de ``work_items``) ; les statuts des indexations
d'un index (clé primaire ``(version_id, index_id)``, lue par ``index_id`` :
une ligne par version et par index, quelques milliers en V1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.indexable_versions import (
    VERSION_DOCUMENT_JOIN,
    is_indexable_version,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    FAILED,
    PENDING,
    RUNNING,
)
from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.providers.index_migration import IndexableVersion
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_versions,
    documents,
    indexations,
    knowledge_units,
    work_items,
)
from knowledge_base.use_cases.indexing_steps import SYNC_VISIBILITY

_v = document_versions.c
_u = knowledge_units.c
_w = work_items.c
_i = indexations.c

_SYNC_DOCUMENT: Final = _w.payload["document_id"].astext


class SqlAlchemyIndexableVersionReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_indexable_versions(self) -> Sequence[IndexableVersion]:
        policies = func.array_remove(
            func.array_agg(_u.chunking_policy_version.distinct()), None
        )
        rows = await self._session.execute(
            select(_v.id, _v.document_id, _v.status, policies.label("policies"))
            .join(documents, VERSION_DOCUMENT_JOIN)
            .outerjoin(knowledge_units, _u.version_id == _v.id)
            .where(is_indexable_version())
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
            select(_SYNC_DOCUMENT)
            .where(_w.kind == SYNC_VISIBILITY, _w.status.in_((PENDING, RUNNING)))
            .distinct()
        )
        return _document_ids(values)

    async def documents_with_failed_visibility(self) -> frozenset[UUID]:
        # Dernier item de chaque document ; à date de création égale (même
        # transaction), un item non FAILED l'emporte.
        latest = (
            select(_SYNC_DOCUMENT.label("document_id"), _w.status)
            .where(_w.kind == SYNC_VISIBILITY)
            .order_by(_SYNC_DOCUMENT, _w.created_at.desc(), (_w.status == FAILED))
            .ext(distinct_on(_SYNC_DOCUMENT))
            .subquery()
        )
        values = await self._session.scalars(
            select(latest.c.document_id).where(latest.c.status == FAILED)
        )
        return _document_ids(values)

    async def indexation_statuses(
        self, index_id: UUID
    ) -> Mapping[UUID, IndexationStatus]:
        rows = await self._session.execute(
            select(_i.version_id, _i.status).where(_i.index_id == index_id)
        )
        return {row.version_id: IndexationStatus(row.status) for row in rows}


def _document_ids(values: Iterable[str | None]) -> frozenset[UUID]:
    found: set[UUID] = set()
    for value in values:
        if value is None:
            continue  # payload sans document
        try:
            found.add(UUID(value))
        except ValueError:
            continue  # payload illisible : il ne désigne aucun document
    return frozenset(found)

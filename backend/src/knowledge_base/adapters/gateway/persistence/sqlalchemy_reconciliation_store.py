"""Constats SQL de la réconciliation (S8 ; port ``ReconciliationStore``).

Une session courte par lecture, hors de toute transaction métier. Les
identifiants du payload des items sont du texte : les jointures se font sur
``id::text`` (un payload illisible ne fait jamais échouer la requête, il ne
joint simplement rien), sur des ensembles petits (items ``FAILED``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Text, cast, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from knowledge_base.adapters.gateway.persistence.indexable_versions import (
    VERSION_DOCUMENT_JOIN,
    is_indexable_version,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_failed_work_repository import (  # noqa: E501
    record_of,
    work_item_columns,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    FAILED,
    PENDING,
    RUNNING,
)
from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.providers.reconciliation_store import (
    CompletedIndexation,
    ExtractedVersion,
    FailedItemFacts,
    IndexableVersionFacts,
    PublicationCandidate,
)
from knowledge_base.domain.services.reconciliation import DocumentFacts
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_versions,
    documents,
    idempotency_keys,
    indexations,
    knowledge_units,
    search_indexes,
    work_items,
)

_d = documents.c
_v = document_versions.c
_i = indexations.c
_s = search_indexes.c
_w = work_items.c

_LIVE_INDEXES: Final = (IndexStatus.ACTIVE.value, IndexStatus.BUILDING.value)


class SqlAlchemyReconciliationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def documents(
        self, document_ids: Collection[UUID]
    ) -> Mapping[UUID, DocumentFacts]:
        if not document_ids:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        _d.id,
                        _d.status,
                        _d.purged_at,
                        _v.id.label("version_id"),
                        _v.status.label("version_status"),
                    )
                    .join(document_versions, _v.document_id == _d.id)
                    .where(_d.id.in_(list(document_ids)))
                )
            ).all()
        versions: defaultdict[UUID, dict[UUID, VersionStatus]] = defaultdict(dict)
        heads: dict[UUID, Any] = {}
        for row in rows:
            heads[row.id] = row
            versions[row.id][row.version_id] = VersionStatus(row.version_status)
        return {
            document_id: DocumentFacts(
                status=DocumentStatus(head.status),
                purged=head.purged_at is not None,
                versions=versions[document_id],
            )
            for document_id, head in heads.items()
        }

    async def documents_with_live_work(self, kind: str) -> frozenset[UUID]:
        async with self._session_factory() as session:
            values = (
                await session.scalars(
                    select(_w.payload["document_id"].astext)
                    .where(_w.kind == kind, _w.status.in_((PENDING, RUNNING)))
                    .distinct()
                )
            ).all()
        return frozenset(found for value in values if (found := _uuid(value)))

    async def publication_candidates(self) -> Sequence[PublicationCandidate]:
        published = aliased(document_versions)
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    _v.document_id,
                    _i.version_id,
                    _i.index_id,
                    _d.status.label("document_status"),
                    _v.status.label("version_status"),
                    _v.number,
                    published.c.number.label("published_number"),
                    _i.status.label("indexation_status"),
                    _s.status.label("index_status"),
                )
                .join(document_versions, _v.id == _i.version_id)
                .join(documents, _d.id == _v.document_id)
                .join(search_indexes, _s.index_id == _i.index_id)
                .outerjoin(published, published.c.id == _d.published_version_id)
                .where(
                    _i.status == IndexationStatus.COMPLETED.value,
                    _v.status == VersionStatus.EXTRACTED.value,
                )
                .order_by(_i.version_id, _i.index_id)
            )
            return [
                PublicationCandidate(
                    document_id=row.document_id,
                    version_id=row.version_id,
                    index_id=row.index_id,
                    document_status=DocumentStatus(row.document_status),
                    version_status=VersionStatus(row.version_status),
                    version_number=row.number,
                    published_number=row.published_number,
                    indexation_status=IndexationStatus(row.indexation_status),
                    index_status=IndexStatus(row.index_status),
                )
                for row in rows
            ]

    async def extracted_versions(self) -> Sequence[ExtractedVersion]:
        async with self._session_factory() as session:
            versions = (
                await session.execute(
                    select(
                        _v.id,
                        _v.document_id,
                        _v.status,
                        _d.status.label("document_status"),
                    )
                    .join(documents, _d.id == _v.document_id)
                    .where(_v.status == VersionStatus.EXTRACTED.value)
                    .order_by(_v.id)
                )
            ).all()
            ids = [row.id for row in versions]
            policies = await _grouped(
                session,
                select(
                    knowledge_units.c.version_id,
                    knowledge_units.c.chunking_policy_version,
                )
                .where(knowledge_units.c.version_id.in_(ids))
                .distinct(),
            )
            indexed = await _grouped(
                session,
                select(_i.version_id, _i.index_id).where(_i.version_id.in_(ids)),
            )
        return [
            ExtractedVersion(
                document_id=row.document_id,
                version_id=row.id,
                document_status=DocumentStatus(row.document_status),
                version_status=VersionStatus(row.status),
                policy_versions=frozenset(policies.get(row.id, ())),
                indexed_index_ids=frozenset(indexed.get(row.id, ())),
            )
            for row in versions
        ]

    async def indexable_versions(self) -> Sequence[IndexableVersionFacts]:
        async with self._session_factory() as session:
            versions = (
                await session.execute(
                    select(_v.id, _v.document_id, _v.status)
                    .join(documents, VERSION_DOCUMENT_JOIN)
                    .where(is_indexable_version())
                    .order_by(_v.id)
                )
            ).all()
            ids = [row.id for row in versions]
            policies = await _grouped(
                session,
                select(
                    knowledge_units.c.version_id,
                    knowledge_units.c.chunking_policy_version,
                )
                .where(knowledge_units.c.version_id.in_(ids))
                .distinct(),
            )
            indexed = await _grouped(
                session,
                select(_i.version_id, _i.index_id).where(_i.version_id.in_(ids)),
            )
        return [
            IndexableVersionFacts(
                document_id=row.document_id,
                version_id=row.id,
                version_status=VersionStatus(row.status),
                policy_versions=frozenset(policies.get(row.id, ())),
                indexed_index_ids=frozenset(indexed.get(row.id, ())),
            )
            for row in versions
        ]

    async def completed_indexations(self) -> Sequence[CompletedIndexation]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    _v.document_id,
                    _i.version_id,
                    _i.index_id,
                    _i.chunking_policy_version,
                    _i.manifest_digest,
                )
                .join(document_versions, _v.id == _i.version_id)
                .join(documents, VERSION_DOCUMENT_JOIN)
                .join(search_indexes, _s.index_id == _i.index_id)
                .where(
                    _i.status == IndexationStatus.COMPLETED.value,
                    is_indexable_version(),
                    _s.status.in_(_LIVE_INDEXES),
                )
                .order_by(_i.version_id, _i.index_id)
            )
            return [
                CompletedIndexation(
                    document_id=row.document_id,
                    version_id=row.version_id,
                    index_id=row.index_id,
                    chunking_policy_version=row.chunking_policy_version,
                    manifest_digest=row.manifest_digest,
                )
                for row in rows
            ]

    async def failed_items(self, kinds: Collection[str]) -> Sequence[FailedItemFacts]:
        other = aliased(work_items)
        payload = _w.payload
        live_work = exists().where(
            other.c.kind == _w.kind,
            other.c.status.in_((PENDING, RUNNING)),
            other.c.payload == payload,
        )
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    *work_item_columns(),
                    _d.status.label("document_status"),
                    (_d.purged_at.is_not(None)).label("purged"),
                    _v.status.label("version_status"),
                    _i.status.label("indexation_status"),
                    live_work.label("live_work"),
                )
                .outerjoin(
                    documents, cast(_d.id, Text) == payload["document_id"].astext
                )
                .outerjoin(
                    document_versions, cast(_v.id, Text) == payload["version_id"].astext
                )
                .outerjoin(
                    indexations,
                    (cast(_i.version_id, Text) == payload["version_id"].astext)
                    & (cast(_i.index_id, Text) == payload["index_id"].astext),
                )
                .where(_w.status == FAILED, _w.kind.in_(list(kinds)))
                .order_by(_w.completed_at, _w.id)
            )
            return [
                FailedItemFacts(
                    item=record_of(row),
                    document_status=_optional(DocumentStatus, row.document_status),
                    purged=row.purged if row.document_status is not None else None,
                    version_status=_optional(VersionStatus, row.version_status),
                    indexation_status=_optional(
                        IndexationStatus, row.indexation_status
                    ),
                    live_work=bool(row.live_work),
                )
                for row in rows
            ]

    async def delete_idempotency_keys_older_than(self, age: timedelta) -> int:
        async with self._session_factory() as session, session.begin():
            deleted = await session.scalars(
                delete(idempotency_keys)
                .where(idempotency_keys.c.created_at < func.now() - age)
                .returning(idempotency_keys.c.key)
            )
            return len(deleted.all())


async def _grouped(session: AsyncSession, query: Any) -> dict[UUID, list[Any]]:
    grouped: defaultdict[UUID, list[Any]] = defaultdict(list)
    for key, value in await session.execute(query):
        grouped[key].append(value)
    return grouped


def _optional[E: StrEnum](enum: type[E], value: str | None) -> E | None:
    return None if value is None else enum(value)


def _uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None

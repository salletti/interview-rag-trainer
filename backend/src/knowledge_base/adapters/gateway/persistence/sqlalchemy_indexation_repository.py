"""Dépôt SQLAlchemy de l'agrégat ``Indexation`` (knowledge-base.md §3.3),
ligne ``indexations`` identifiée par ``(version_id, index_id)``.

Pas de verrou optimiste : une indexation n'est modifiée que dans la
transaction de finalisation (ou d'échec) de son work item, protégée par le
fencing token (I14), ou à la création des indexations d'une version.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.frameworks.sqlalchemy.tables import indexations

_x = indexations.c


class SqlAlchemyIndexationRepository:
    """Travaille dans la session de l'Unit of Work ; ne valide ni n'annule
    jamais la transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, version_id: UUID, index_id: UUID) -> Indexation | None:
        row = (
            await self._session.execute(
                select(indexations).where(
                    _x.version_id == version_id, _x.index_id == index_id
                )
            )
        ).one_or_none()
        return None if row is None else _to_indexation(row)

    async def add(self, indexation: Indexation) -> None:
        await self._session.execute(
            insert(indexations).values(
                version_id=indexation.version_id,
                index_id=indexation.index_id,
                chunking_policy_version=indexation.chunking_policy_version,
                **_mutable_values(indexation),
            )
        )

    async def save(self, indexation: Indexation) -> None:
        await self._session.execute(
            update(indexations)
            .where(
                _x.version_id == indexation.version_id,
                _x.index_id == indexation.index_id,
            )
            .values(**_mutable_values(indexation))
        )


def _mutable_values(indexation: Indexation) -> dict[str, Any]:
    failure = indexation.failure
    return {
        "status": indexation.status.value,
        "manifest_digest": indexation.manifest_digest,
        "point_count": indexation.point_count,
        "completed_at": indexation.completed_at,
        "failure_code": None if failure is None else failure.code,
        "failure_message": None if failure is None else failure.message,
        "failure_retryable": None if failure is None else failure.retryable,
    }


def _to_indexation(row: Row[Any]) -> Indexation:
    failure = None
    if row.failure_code is not None:
        failure = FailureReason(
            code=row.failure_code,
            message=row.failure_message,
            retryable=row.failure_retryable,
        )
    return Indexation.restore(
        version_id=row.version_id,
        index_id=row.index_id,
        status=IndexationStatus(row.status),
        chunking_policy_version=row.chunking_policy_version,
        manifest_digest=row.manifest_digest,
        point_count=row.point_count,
        completed_at=row.completed_at,
        failure=failure,
    )

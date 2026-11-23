"""Items ``FAILED`` de ``knowledge_base.work_items`` : lecture, verrou et
réouverture (S8 ; S0 §3, transition ``reopen``), dans la session de
l'appelant. Ne valide ni n'annule jamais la transaction."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    FAILED,
    PENDING,
)
from knowledge_base.domain.providers.failed_work import (
    WorkItemRecord,
    WorkItemStatus,
)
from knowledge_base.frameworks.sqlalchemy.tables import work_items

_c = work_items.c


class SqlAlchemyFailedWorkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self, item_id: UUID) -> WorkItemRecord | None:
        row = (
            await self._session.execute(
                _records().where(_c.id == item_id).with_for_update()
            )
        ).one_or_none()
        return None if row is None else record_of(row)

    async def reopen(self, item_id: UUID) -> bool:
        reopened = await self._session.scalar(
            update(work_items)
            .where(_c.id == item_id, _c.status == FAILED)
            .values(
                status=PENDING,
                attempts=0,
                available_at=func.now(),
                dispatched_at=None,
                completed_at=None,
                reopen_count=_c.reopen_count + 1,
                updated_at=func.now(),
            )
            .returning(_c.id)
        )
        return reopened is not None

    async def list_failed(
        self, limit: int, *, excluding_kinds: Collection[str] = ()
    ) -> Sequence[WorkItemRecord]:
        rows = await self._session.execute(
            _records()
            .where(_c.status == FAILED, _c.kind.not_in(excluding_kinds))
            .order_by(_c.completed_at.desc().nulls_last(), _c.id)
            .limit(limit)
        )
        return [record_of(row) for row in rows]


def work_item_columns() -> tuple[ColumnElement[Any], ...]:
    """Colonnes d'un ``WorkItemRecord`` (lu par ``record_of``)."""
    return (
        _c.id,
        _c.kind,
        _c.status,
        _c.payload,
        _c.attempts,
        _c.max_attempts,
        _c.last_error_code,
        _c.last_error_message,
        _c.completed_at,
        _c.reopen_count,
    )


def _records() -> Select[Any]:
    return select(*work_item_columns())


def record_of(row: Any) -> WorkItemRecord:
    return WorkItemRecord(
        item_id=row.id,
        kind=row.kind,
        status=WorkItemStatus(row.status),
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        failed_at=row.completed_at,
        reopen_count=row.reopen_count,
    )

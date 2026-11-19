"""Cycles de réconciliation ``FAILED`` : ni listés, ni rejoués (revue S8, I2 ;
ADR 0004 point 9).

Un cycle n'a pas d'objet propre : le créneau suivant en planifie un neuf, qui
refait tout le travail. Le lister inviterait à le rejouer pour rien ;
``RetryIngestion`` le refuse en ``NOT_REPLAYABLE`` (``SupersededCycleError``)
quel que soit son code, même un code rejouable (``UNEXPECTED_ERROR``).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.providers.failed_work import (
    FailedWorkUnitOfWork,
    WorkItemRecord,
    WorkItemStatus,
)
from knowledge_base.use_cases.list_failures.list_failures_command import ListFailures
from knowledge_base.use_cases.list_failures.list_failures_handler import (
    ListFailuresHandler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    RECONCILE_KNOWLEDGE_BASE,
)
from knowledge_base.use_cases.retry_ingestion.errors import (
    NotReplayableError,
    SupersededCycleError,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_command import (
    RetryIngestion,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_handler import (
    RetryIngestionHandler,
)

FAILED_AT = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)


def failed(kind: str, code: str = "UNEXPECTED_ERROR") -> WorkItemRecord:
    return WorkItemRecord(
        item_id=uuid4(),
        kind=kind,
        status=WorkItemStatus.FAILED,
        payload={},
        attempts=3,
        max_attempts=3,
        last_error_code=code,
        last_error_message="Qdrant injoignable",
        failed_at=FAILED_AT,
        reopen_count=0,
    )


@dataclass
class FakeFailedWork:
    """Items en mémoire ; ``list_failed`` filtre AVANT la limite, comme le SQL."""

    items: dict[UUID, WorkItemRecord] = field(default_factory=dict)
    reopened: list[UUID] = field(default_factory=list)

    async def get_for_update(self, item_id: UUID) -> WorkItemRecord | None:
        return self.items.get(item_id)

    async def reopen(self, item_id: UUID) -> bool:
        self.reopened.append(item_id)
        return True

    async def list_failed(
        self, limit: int, *, excluding_kinds: Collection[str] = ()
    ) -> Sequence[WorkItemRecord]:
        kept = [r for r in self.items.values() if r.kind not in excluding_kinds]
        return kept[:limit]


class FakeUnitOfWork:
    def __init__(self, failed_work: FakeFailedWork) -> None:
        self.failed_work = failed_work
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def factory(uow: FakeUnitOfWork) -> FailedWorkUnitOfWork:
    return cast(FailedWorkUnitOfWork, uow)


@pytest.mark.parametrize("code", ["UNEXPECTED_ERROR", "LEASE_EXPIRED_MAX_ATTEMPTS"])
async def test_a_failed_reconciliation_cycle_is_never_replayed(code: str) -> None:
    cycle = failed(RECONCILE_KNOWLEDGE_BASE, code)
    uow = FakeUnitOfWork(FakeFailedWork({cycle.item_id: cycle}))
    handler = RetryIngestionHandler(unit_of_work=lambda: factory(uow))

    with pytest.raises(SupersededCycleError) as refused:
        await handler.handle(RetryIngestion(item_id=cycle.item_id))

    assert isinstance(refused.value, NotReplayableError)  # 409 NOT_REPLAYABLE
    assert refused.value.code == code
    assert uow.failed_work.reopened == []
    assert uow.commits == 0


async def test_failed_reconciliation_cycles_are_not_listed() -> None:
    cycles = [failed(RECONCILE_KNOWLEDGE_BASE) for _ in range(3)]
    purge = failed("purge_document", "TRANSIENT")
    store = FakeFailedWork({r.item_id: r for r in [*cycles, purge]})
    handler = ListFailuresHandler(unit_of_work=lambda: factory(FakeUnitOfWork(store)))

    views = await handler.handle(ListFailures(limit=1))

    assert [(v.item_id, v.kind) for v in views] == [(purge.item_id, "purge_document")]

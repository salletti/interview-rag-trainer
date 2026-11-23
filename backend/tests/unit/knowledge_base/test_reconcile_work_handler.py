"""Handler de work item ``reconcile_knowledge_base`` : pont entre la work queue
(S0 §5) et le use case ``ReconcileKnowledgeBase`` (S8).

API figée (``knowledge_base.adapters.controller.tasks.reconcile_handler``),
même forme que ``purge_document_handler`` :

- ``ReconcileWorkHandler(use_case: ReconcileKnowledgeBaseHandler, *,
  heartbeat_interval: timedelta | None = None)`` ; ``kind =
  "reconcile_knowledge_base"``, ``queue = "knowledge_base.ingestion"`` (voir
  ``test_register_worker_reconcile`` : sérialisé avec les autres traitements),
  ``max_attempts >= 1``.
- ``execute(item, ctx)`` : ``use_case.handle(ReconcileKnowledgeBase())`` hors
  transaction, sous ``keeping_lease`` (un cycle sur un grand corpus dépasse le
  lease : un heartbeat refusé l'interrompt) ; puis UN SEUL ``ctx.finalize``
  dont le callback n'écrit rien (les corrections ont leurs propres
  transactions) : l'item doit se terminer ``SUCCEEDED``.
- Toute erreur du cycle (Qdrant, volume, PostgreSQL) remonte telle quelle :
  transitoire pour S0, jamais de finalisation. Un cycle raté n'est jamais
  « réussi » : l'item est rejoué avec backoff, puis ``FAILED`` consultable.
- Pas de hook ``on_terminal_failure`` : aucun agrégat métier à marquer.
- ``ReconcileKnowledgeBase`` : commande sans champ
  (``reconcile_knowledge_base_command``). ``ReconciliationReport`` :
  ``reconciliation_report`` ; voir ``test_reconciliation_report``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.reconcile_handler import (
    ReconcileWorkHandler,
)
from knowledge_base.adapters.controller.tasks.registry import (
    HandlerRegistry,
    HandlesTerminalFailure,
)
from knowledge_base.adapters.controller.tasks.retry_policy import StaleAttempt
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_handler import (  # noqa: E501
    ReconcileKnowledgeBaseHandler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconciliation_report import (
    ReconciliationReport,
)
from tests.unit.knowledge_base.s8_support import INGESTION_QUEUE, RECONCILE_KIND

SESSION = cast(AsyncSession, object())
HEARTBEAT = timedelta(seconds=0.02)


class FakeUseCase:
    def __init__(
        self, *, error: Exception | None = None, forever: bool = False
    ) -> None:
        self.commands: list[ReconcileKnowledgeBase] = []
        self.cancelled = False
        self._error = error
        self._forever = forever

    async def handle(self, command: ReconcileKnowledgeBase) -> ReconciliationReport:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        if self._forever:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return ReconciliationReport(counts={})


class Context:
    def __init__(self, *, stale: bool = False) -> None:
        self._stale = stale
        self.heartbeats = 0
        self.finalize_calls = 0
        self.callback_session: object = None

    async def heartbeat(self) -> None:
        self.heartbeats += 1
        if self._stale:
            raise StaleAttempt("lease repris par une autre tentative")

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1
        self.callback_session = SESSION
        await callback(SESSION)


def handler(
    use_case: FakeUseCase, *, heartbeat: timedelta | None = None
) -> ReconcileWorkHandler:
    return ReconcileWorkHandler(
        cast(ReconcileKnowledgeBaseHandler, use_case), heartbeat_interval=heartbeat
    )


def claimed() -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind=RECONCILE_KIND,
        payload={},
        attempts=1,
        max_attempts=3,
        fencing_token=1,
        correlation_id=None,
    )


def test_the_handler_serves_reconcile_on_the_ingestion_queue() -> None:
    work_handler = handler(FakeUseCase())

    registry = HandlerRegistry([work_handler])

    assert work_handler.kind == RECONCILE_KIND
    assert registry.kinds_by_queue() == {INGESTION_QUEUE: frozenset({RECONCILE_KIND})}
    assert registry.max_attempts_by_kind()[RECONCILE_KIND] >= 1
    assert not isinstance(work_handler, HandlesTerminalFailure)


async def test_execute_runs_one_cycle_then_finalizes_exactly_once() -> None:
    use_case, ctx = FakeUseCase(), Context()

    await handler(use_case).execute(claimed(), cast(WorkContext, ctx))

    assert use_case.commands == [ReconcileKnowledgeBase()]
    assert ctx.finalize_calls == 1


async def test_a_failing_cycle_never_finalizes_and_the_error_surfaces() -> None:
    boom = ConnectionError("Qdrant injoignable")
    use_case, ctx = FakeUseCase(error=boom), Context()

    with pytest.raises(ConnectionError):
        await handler(use_case).execute(claimed(), cast(WorkContext, ctx))

    assert ctx.finalize_calls == 0


async def test_the_lease_is_extended_while_a_long_cycle_runs() -> None:
    class Slow(FakeUseCase):
        async def handle(self, command: ReconcileKnowledgeBase) -> ReconciliationReport:
            await asyncio.sleep(0.1)
            return await super().handle(command)

    ctx = Context()

    await handler(Slow(), heartbeat=HEARTBEAT).execute(
        claimed(), cast(WorkContext, ctx)
    )

    assert ctx.heartbeats >= 2
    assert ctx.finalize_calls == 1


async def test_a_refused_heartbeat_interrupts_the_cycle_without_finalizing() -> None:
    use_case, ctx = FakeUseCase(forever=True), Context(stale=True)

    with pytest.raises(StaleAttempt):
        async with asyncio.timeout(5):
            await handler(use_case, heartbeat=HEARTBEAT).execute(
                claimed(), cast(WorkContext, ctx)
            )

    assert use_case.cancelled
    assert ctx.finalize_calls == 0

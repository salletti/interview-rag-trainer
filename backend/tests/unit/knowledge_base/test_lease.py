"""``keeping_lease`` : heartbeats pendant un travail long, interruption au
premier refus, aucune tâche qui survive à l'appel (S0 §7 ;
knowledge-base.md §7.3 condition 6).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast

import pytest

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.lease import keeping_lease
from knowledge_base.adapters.controller.tasks.retry_policy import StaleAttempt

INTERVAL = timedelta(seconds=0.01)
STEP_SECONDS = 0.01
STEPS = 30


class Context:
    """Double de ``WorkContext`` : ``refusal`` levée dès le heartbeat n°
    ``refuse_at`` (à partir de 1) ; ``None`` : tous acceptés."""

    def __init__(
        self, *, refuse_at: int | None = None, refusal: Exception | None = None
    ) -> None:
        self._refuse_at = refuse_at
        self._refusal = refusal or StaleAttempt("lease repris")
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1
        if self._refuse_at is not None and self.heartbeats >= self._refuse_at:
            raise self._refusal


class Work:
    """Travail découpé en étapes asynchrones, comme des upserts par lots."""

    def __init__(self) -> None:
        self.steps = 0
        self.cancelled = False

    async def run(self) -> str:
        try:
            for _ in range(STEPS):
                await asyncio.sleep(STEP_SECONDS)
                self.steps += 1
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "fait"


def as_context(ctx: Context) -> WorkContext:
    return cast(WorkContext, ctx)


def other_tasks() -> set[asyncio.Task[object]]:
    return asyncio.all_tasks() - {asyncio.current_task()}


async def test_the_work_result_is_returned_while_heartbeats_are_accepted() -> None:
    ctx = Context()
    work = Work()

    result = await keeping_lease(as_context(ctx), work.run(), INTERVAL)

    assert result == "fait"
    assert work.steps == STEPS
    assert ctx.heartbeats >= 3
    assert other_tasks() == set()


async def test_a_refused_heartbeat_cancels_the_work_at_once() -> None:
    ctx = Context(refuse_at=2)
    work = Work()

    with pytest.raises(StaleAttempt):
        await keeping_lease(as_context(ctx), work.run(), INTERVAL)
    steps_at_refusal = work.steps
    await asyncio.sleep(STEP_SECONDS * 5)

    assert work.cancelled
    assert steps_at_refusal < STEPS
    assert work.steps == steps_at_refusal  # plus aucune étape après le refus
    assert ctx.heartbeats == 2
    assert other_tasks() == set()


async def test_any_heartbeat_error_interrupts_the_work_and_is_raised() -> None:
    ctx = Context(refuse_at=1, refusal=ConnectionError("PostgreSQL indisponible"))
    work = Work()

    with pytest.raises(ConnectionError, match="PostgreSQL"):
        await keeping_lease(as_context(ctx), work.run(), INTERVAL)

    assert work.cancelled
    assert other_tasks() == set()


async def test_a_work_error_is_raised_and_stops_the_heartbeats() -> None:
    ctx = Context()

    async def broken() -> None:
        await asyncio.sleep(STEP_SECONDS * 3)
        raise RuntimeError("Qdrant indisponible")

    with pytest.raises(RuntimeError, match="Qdrant"):
        await keeping_lease(as_context(ctx), broken(), INTERVAL)
    beats = ctx.heartbeats
    await asyncio.sleep(STEP_SECONDS * 5)

    assert ctx.heartbeats == beats
    assert other_tasks() == set()


async def test_cancelling_the_caller_cancels_the_work_and_the_heartbeats() -> None:
    ctx = Context()
    work = Work()
    caller = asyncio.create_task(keeping_lease(as_context(ctx), work.run(), INTERVAL))
    await asyncio.sleep(STEP_SECONDS * 3)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    beats = ctx.heartbeats
    await asyncio.sleep(STEP_SECONDS * 5)

    assert work.cancelled
    assert ctx.heartbeats == beats
    assert other_tasks() == set()


async def test_without_interval_the_work_runs_without_heartbeat() -> None:
    ctx = Context(refuse_at=1)
    work = Work()

    assert await keeping_lease(as_context(ctx), work.run(), None) == "fait"
    assert ctx.heartbeats == 0

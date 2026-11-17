"""Exécution d'un travail (§5) : claim, handler hors transaction, finalisation
fencée, classification des erreurs. Scénarios 6, 6b, 8, 9 et 10 du §12.2.

API figée (``adapters/controller/tasks/execution.py``) :

- ``execute_work_item(item_id, resources, settings, *, owner=None)`` ->
  ``"skipped" | "succeeded" | "retry_scheduled" | "failed" | "stale"``.
  ``skipped`` : claim à 0 ligne ; ``stale`` : ``StaleAttempt`` (aucune
  transition par la tentative périmée).
- ``WorkContext.heartbeat()`` (lève ``StaleAttempt`` si 0 ligne) et
  ``WorkContext.finalize(callback)`` ; ``callback(session)`` reçoit l'
  ``AsyncSession`` de la transaction de finalisation.
- ``handler.on_terminal_failure(session, item, error: FailureInfo)`` est
  facultatif et s'exécute dans la transaction de ``fail``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base import (
    KnowledgeBaseSettings,
    KnowledgeBaseWorkerResources,
    WorkHandler,
)
from knowledge_base.adapters.controller.tasks.dispatch import recover_expired_leases
from knowledge_base.adapters.controller.tasks.execution import (
    WorkContext,
    execute_work_item,
)
from knowledge_base.adapters.controller.tasks.retry_policy import (
    StaleAttempt,
    TerminalWorkError,
    TransientWorkError,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
    SqlAlchemyWorkLifecycle,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from runtime.database import SessionFactory
from tests.integration.knowledge_base.support import (
    NewHandler,
    cancel_committed,
    effect_notes,
    effects_of,
    enqueue_committed,
    fetch_item,
    fetch_scalar,
    finalize_with_effect,
    kb_settings,
    seconds_from_now,
    set_item,
    write_effect,
)

pytestmark = pytest.mark.integration

FAST_RETRY = {"work_backoff_base_seconds": 0.01}


def resources_for(
    factory: SessionFactory, *handlers: WorkHandler
) -> KnowledgeBaseWorkerResources:
    return KnowledgeBaseWorkerResources(
        session_factory=factory,
        loop=asyncio.get_running_loop(),
        handlers=tuple(handlers),
    )


async def run(
    factory: SessionFactory,
    handler: WorkHandler,
    item_id: UUID,
    settings: KnowledgeBaseSettings | None = None,
    owner: str = "worker-A",
) -> str:
    return await execute_work_item(
        item_id,
        resources_for(factory, handler),
        settings or kb_settings(),
        owner=owner,
    )


# --- chemin nominal -----------------------------------------------------------------


async def test_a_handler_that_finalizes_ends_the_item_succeeded_with_one_effect(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(session_factory, handler, item_id)

    row = await fetch_item(session_factory, item_id)
    assert outcome == "succeeded"
    assert row["status"] == "SUCCEEDED"
    assert row["attempts"] == 1
    assert row["completed_at"] is not None
    assert row["lease_expires_at"] is None
    assert await effects_of(session_factory, item_id) == [1]


async def test_executing_a_finished_item_again_is_skipped_without_a_second_effect(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, handler.kind)
    await run(session_factory, handler, item_id)

    outcome = await run(session_factory, handler, item_id)

    assert outcome == "skipped"
    assert await effects_of(session_factory, item_id) == [1]
    assert (await fetch_item(session_factory, item_id))["attempts"] == 1


async def test_executing_an_unknown_item_id_is_skipped(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)

    assert await run(session_factory, handler, uuid.uuid4()) == "skipped"


async def test_the_handler_runs_while_the_item_is_running_under_the_owner_lease(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    seen: dict[str, object] = {}

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        row = await fetch_item(session_factory, item.id)
        seen.update(
            status=row["status"],
            owner=row["lease_owner"],
            token=item.fencing_token,
            attempts=item.attempts,
            payload=dict(item.payload),
        )
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(
        session_factory, handler.kind, payload={"version_id": "v-7"}
    )

    await run(session_factory, handler, item_id, owner="worker-X")

    assert seen == {
        "status": "RUNNING",
        "owner": "worker-X",
        "token": 1,
        "attempts": 1,
        "payload": {"version_id": "v-7"},
    }


async def test_no_transaction_is_open_while_the_handler_runs(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    idle_in_transaction: list[int] = []

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        count = await fetch_scalar(
            session_factory,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()",
        )
        idle_in_transaction.append(int(count))
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind)

    await run(session_factory, handler, item_id)

    assert idle_in_transaction == [0]


async def test_finalization_is_atomic_a_failing_callback_discards_its_writes(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        async def callback(session: AsyncSession) -> None:
            await write_effect(session, item)
            raise TransientWorkError("écriture métier impossible")

        await ctx.finalize(callback)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(session_factory, handler, item_id, kb_settings(**FAST_RETRY))

    row = await fetch_item(session_factory, item_id)
    assert outcome == "retry_scheduled"
    assert row["status"] == "PENDING"
    assert await effects_of(session_factory, item_id) == []


# --- scénario 6 : tentative zombie --------------------------------------------------


async def test_a_superseded_attempt_cannot_finalize_after_its_lease_was_reassigned(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        if item.attempts == 1:  # tentative A : lente, sans heartbeat
            started.set()
            await release.wait()
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    settings = kb_settings(work_lease_ttl_seconds=0.3, **FAST_RETRY)
    resources = resources_for(session_factory, handler)
    item_id = await enqueue_committed(session_factory, handler.kind)

    attempt_a = asyncio.create_task(
        execute_work_item(item_id, resources, settings, owner="A")
    )
    await started.wait()
    await asyncio.sleep(0.5)  # le lease de A expire
    assert await recover_expired_leases(resources, settings) == 1
    await asyncio.sleep(0.1)  # backoff de reprise (<= 10 ms)
    outcome_b = await execute_work_item(item_id, resources, settings, owner="B")
    release.set()
    outcome_a = await attempt_a

    row = await fetch_item(session_factory, item_id)
    assert outcome_b == "succeeded"
    assert outcome_a == "stale"
    assert row["status"] == "SUCCEEDED"
    assert (row["attempts"], row["fencing_token"]) == (2, 2)
    # Un seul effet métier, celui de B ; celui de A a été annulé (ROLLBACK de T2).
    assert await effects_of(session_factory, item_id) == [2]


async def test_a_worker_killed_after_the_claim_is_recovered_and_reclaimed(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    # Scénario 6b : claim, puis plus rien (ni heartbeat ni finalisation).
    handler = new_handler(finalize_with_effect)
    settings = kb_settings(**FAST_RETRY)
    resources = resources_for(session_factory, handler)
    lifecycle = SqlAlchemyWorkLifecycle(session_factory)
    item_id = await enqueue_committed(session_factory, handler.kind)
    dead = await lifecycle.claim(item_id, "dead-worker", timedelta(seconds=0.3))
    assert dead is not None
    await asyncio.sleep(0.5)

    assert await recover_expired_leases(resources, settings) == 1
    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["last_error_code"]) == ("PENDING", "LEASE_EXPIRED")
    await asyncio.sleep(0.1)
    outcome = await execute_work_item(item_id, resources, settings, owner="B")

    row = await fetch_item(session_factory, item_id)
    assert outcome == "succeeded"
    assert (row["attempts"], row["fencing_token"]) == (2, dead.fencing_token + 1)
    assert await effects_of(session_factory, item_id) == [2]


async def test_a_worker_killed_on_the_last_attempt_leaves_the_item_failed(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    resources = resources_for(session_factory, handler)
    lifecycle = SqlAlchemyWorkLifecycle(session_factory)
    item_id = await enqueue_committed(session_factory, handler.kind, max_attempts=1)
    await lifecycle.claim(item_id, "dead-worker", timedelta(seconds=0.2))
    await asyncio.sleep(0.4)

    await recover_expired_leases(resources, kb_settings())

    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["last_error_code"]) == (
        "FAILED",
        "LEASE_EXPIRED_MAX_ATTEMPTS",
    )


# --- scénario 8 : erreurs transitoires ----------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "fragment"),
    [
        (TransientWorkError("qdrant indisponible"), "TRANSIENT", "qdrant"),
        (RuntimeError("boom inattendu"), "UNEXPECTED_ERROR", "boom inattendu"),
    ],
    ids=["transient", "unknown-exception"],
)
async def test_a_transient_or_unknown_error_is_retried_then_fails_at_max_attempts(
    session_factory: SessionFactory,
    new_handler: NewHandler,
    error: Exception,
    code: str,
    fragment: str,
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        raise error

    handler = new_handler(behaviour, max_attempts=2)
    settings = kb_settings(work_backoff_base_seconds=30)
    item_id = await enqueue_committed(session_factory, handler.kind)

    first = await run(session_factory, handler, item_id, settings)

    row = await fetch_item(session_factory, item_id)
    assert first == "retry_scheduled"
    assert (row["status"], row["attempts"]) == ("PENDING", 1)
    assert row["last_error_code"] == code
    assert fragment in row["last_error_message"]
    assert row["dispatched_at"] is None
    # attempts = 1 : d = 30 s, equal jitter dans [15, 30].
    assert 10 < await seconds_from_now(session_factory, item_id, "available_at") <= 30

    # Tant que available_at n'est pas atteint, le claim est refusé.
    assert await run(session_factory, handler, item_id, settings) == "skipped"
    assert (await fetch_item(session_factory, item_id))["attempts"] == 1

    await set_item(session_factory, item_id, available_at="now()")
    second = await run(session_factory, handler, item_id, settings)

    row = await fetch_item(session_factory, item_id)
    assert second == "failed"
    assert (row["status"], row["attempts"]) == ("FAILED", 2)
    assert row["last_error_code"] == code
    assert fragment in row["last_error_message"]
    assert row["completed_at"] is not None
    assert await effects_of(session_factory, item_id) == []


async def test_the_handler_max_attempts_is_applied_at_claim_over_the_enqueue_default(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        raise TransientWorkError("toujours indisponible")

    handler = new_handler(behaviour, max_attempts=2)
    item_id = await enqueue_committed(session_factory, handler.kind)
    assert (await fetch_item(session_factory, item_id))["max_attempts"] == 5

    first = await run(session_factory, handler, item_id, kb_settings(**FAST_RETRY))
    assert (await fetch_item(session_factory, item_id))["max_attempts"] == 2
    await set_item(session_factory, item_id, available_at="now()")
    second = await run(session_factory, handler, item_id, kb_settings(**FAST_RETRY))

    row = await fetch_item(session_factory, item_id)
    assert (first, second) == ("retry_scheduled", "failed")
    assert (row["status"], row["attempts"], row["last_error_code"]) == (
        "FAILED",
        2,
        "TRANSIENT",
    )


async def test_a_retried_item_succeeds_when_the_second_attempt_works(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        if item.attempts == 1:
            raise TransientWorkError("pas cette fois")
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    settings = kb_settings(**FAST_RETRY)
    item_id = await enqueue_committed(session_factory, handler.kind)
    await run(session_factory, handler, item_id, settings)
    await asyncio.sleep(0.05)

    outcome = await run(session_factory, handler, item_id, settings)

    assert outcome == "succeeded"
    assert await effects_of(session_factory, item_id) == [2]


# --- scénario 9 : erreur terminale et hook métier -----------------------------------


async def test_a_terminal_error_fails_the_item_and_runs_the_hook_atomically(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        raise TerminalWorkError("EMPTY_DOCUMENT", "aucun texte extractible")

    async def hook(
        session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await write_effect(session, item, note=f"hook:{error.code}")

    handler = new_handler(behaviour, hook)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(session_factory, handler, item_id)

    row = await fetch_item(session_factory, item_id)
    assert outcome == "failed"
    assert row["status"] == "FAILED"
    assert row["attempts"] == 1
    assert row["last_error_code"] == "EMPTY_DOCUMENT"
    assert "aucun texte extractible" in row["last_error_message"]
    assert await effect_notes(session_factory, item_id) == ["hook:EMPTY_DOCUMENT"]


async def test_the_failure_hook_is_rolled_back_when_the_item_was_cancelled_meanwhile(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def hook(
        session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await write_effect(session, item, note="hook")

    dedup_key = "test.cancelled-while-failing"

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await cancel_committed(session_factory, dedup_key)
        raise TerminalWorkError("BAD_INPUT")

    handler = new_handler(behaviour, hook)
    item_id = await enqueue_committed(session_factory, handler.kind, dedup_key)

    outcome = await run(session_factory, handler, item_id)

    assert outcome == "stale"
    assert (await fetch_item(session_factory, item_id))["status"] == "CANCELLED"
    assert await effect_notes(session_factory, item_id) == []


async def test_a_handler_returning_without_finalize_fails_with_not_finalized(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        return None  # oublie ctx.finalize

    async def hook(
        session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await write_effect(session, item, note=f"hook:{error.code}")

    handler = new_handler(behaviour, hook)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(session_factory, handler, item_id)

    row = await fetch_item(session_factory, item_id)
    assert outcome == "failed"
    assert (row["status"], row["last_error_code"]) == ("FAILED", "NOT_FINALIZED")
    # Aucun effet métier de finalisation ; seul le hook d'échec a écrit.
    assert await effect_notes(session_factory, item_id) == ["hook:NOT_FINALIZED"]


async def test_a_failing_terminal_hook_is_rolled_back_and_retried_then_failed(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        raise TerminalWorkError("BAD_INPUT", "illisible")

    async def hook(
        session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await write_effect(session, item, note="hook")
        raise RuntimeError("table métier verrouillée")

    handler = new_handler(behaviour, hook, max_attempts=2)
    settings = kb_settings(**FAST_RETRY)
    item_id = await enqueue_committed(session_factory, handler.kind)

    first = await run(session_factory, handler, item_id, settings)

    row = await fetch_item(session_factory, item_id)
    assert first == "retry_scheduled"
    assert (row["status"], row["attempts"], row["last_error_code"]) == (
        "PENDING",
        1,
        "TERMINAL_HOOK_FAILED",
    )
    assert "BAD_INPUT" in row["last_error_message"]
    assert "table métier verrouillée" in row["last_error_message"]
    assert await effect_notes(session_factory, item_id) == []  # hook annulé

    await set_item(session_factory, item_id, available_at="now()")
    second = await run(session_factory, handler, item_id, settings)

    row = await fetch_item(session_factory, item_id)
    assert second == "failed"
    assert (row["status"], row["attempts"], row["last_error_code"]) == (
        "FAILED",
        2,
        "TERMINAL_HOOK_FAILED",
    )
    assert row["completed_at"] is not None
    assert await effect_notes(session_factory, item_id) == []


async def test_a_terminal_error_without_a_hook_still_fails_the_item(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        raise TerminalWorkError("BAD_INPUT", "x")

    handler = new_handler(behaviour, hookless=True)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(session_factory, handler, item_id)

    row = await fetch_item(session_factory, item_id)
    assert (outcome, row["status"], row["last_error_code"]) == (
        "failed",
        "FAILED",
        "BAD_INPUT",
    )


async def test_an_item_of_an_unregistered_kind_reaching_execute_fails_with_unknown_kind(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, "extract_version")

    outcome = await run(session_factory, handler, item_id)

    row = await fetch_item(session_factory, item_id)
    assert outcome == "failed"
    assert (row["status"], row["last_error_code"]) == ("FAILED", "UNKNOWN_KIND")


# --- scénario 10 : annulation pendant l'exécution -----------------------------------


async def test_cancelling_during_execution_rejects_the_finalization(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        started.set()
        await release.wait()
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind, "k:cancel-me")
    attempt = asyncio.create_task(run(session_factory, handler, item_id))
    await started.wait()

    assert await cancel_committed(session_factory, "k:cancel-me") is True
    release.set()
    outcome = await attempt

    row = await fetch_item(session_factory, item_id)
    assert outcome == "stale"
    assert row["status"] == "CANCELLED"
    assert row["fencing_token"] == 2  # claim (1) puis annulation (2)
    assert await effects_of(session_factory, item_id) == []


async def test_a_heartbeat_after_cancellation_stops_the_attempt_with_stale_attempt(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    raised: list[type[BaseException]] = []

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        started.set()
        await release.wait()
        try:
            await ctx.heartbeat()
        except StaleAttempt as error:
            raised.append(type(error))
            raise

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind, "k:cancel-hb")
    attempt = asyncio.create_task(run(session_factory, handler, item_id))
    await started.wait()
    await cancel_committed(session_factory, "k:cancel-hb")
    release.set()

    outcome = await attempt

    row = await fetch_item(session_factory, item_id)
    assert raised == [StaleAttempt]
    assert outcome == "stale"
    assert (row["status"], row["fencing_token"]) == ("CANCELLED", 2)


async def test_a_heartbeat_extends_the_lease_of_the_running_attempt(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    measured: dict[str, datetime] = {}

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        before = await fetch_item(session_factory, item.id)
        measured["before"] = before["lease_expires_at"]
        await asyncio.sleep(1.5)
        await ctx.heartbeat()
        after = await fetch_item(session_factory, item.id)
        measured["after"] = after["lease_expires_at"]
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind)

    outcome = await run(
        session_factory, handler, item_id, kb_settings(work_lease_ttl_seconds=30)
    )

    assert outcome == "succeeded"
    # Échéances absolues : le heartbeat, ~1,5 s après le claim, repousse la fin
    # du lease d'autant. (Un écart « lease - now() » vaudrait ~30 s dans les
    # deux mesures, avec ou sans heartbeat.)
    assert measured["after"] - measured["before"] > timedelta(seconds=1)


async def test_the_work_is_not_executed_when_the_claim_is_lost_to_another_worker(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    calls: list[UUID] = []

    async def behaviour(item: ClaimedWorkItem, ctx: WorkContext) -> None:
        calls.append(item.id)
        await finalize_with_effect(item, ctx)

    handler = new_handler(behaviour)
    item_id = await enqueue_committed(session_factory, handler.kind)
    other = await SqlAlchemyWorkLifecycle(session_factory).claim(
        item_id, "other", timedelta(seconds=30)
    )
    assert other is not None

    outcome = await run(session_factory, handler, item_id)

    assert outcome == "skipped"
    assert calls == []
    assert (await fetch_item(session_factory, item_id))["lease_owner"] == "other"

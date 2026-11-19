"""Cycle complet à travers un vrai RabbitMQ et un worker Celery in-process
(pool solo) : scénarios 4, 5 et 7b du §12.2, plus la tâche ``recover``.

Le broker et la base sont réels ; le handler est un artefact de test injecté
dans le registre. Les files sont propres à chaque test.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkLifecycle,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.support import (
    NewHandler,
    consuming,
    effects_of,
    enqueue_committed,
    fetch_item,
    finalize_with_effect,
    kb_settings,
    pop_message_with_ack,
    queue_depth,
    run_dispatch_task,
    run_recover_task,
    set_item,
    wait_for,
    wait_for_status,
    worker_rig,
)

pytestmark = pytest.mark.integration

EXECUTE = "knowledge_base.work.execute"


async def test_full_cycle_enqueue_dispatch_execute_finalize(
    integration_settings: Settings,
    session_factory: SessionFactory,
    new_handler: NewHandler,
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(
        session_factory, handler.kind, payload={"version_id": "v-1"}
    )
    with worker_rig(integration_settings, [handler], kb_settings()) as rig:
        dispatched = await asyncio.to_thread(run_dispatch_task, rig)

        await wait_for_status(session_factory, item_id, "SUCCEEDED")

    row = await fetch_item(session_factory, item_id)
    assert dispatched == 1
    assert row["dispatched_at"] is not None
    assert (row["attempts"], row["fencing_token"]) == (1, 1)
    assert await effects_of(session_factory, item_id) == [1]


async def test_the_same_message_delivered_twice_produces_a_single_effect(
    integration_settings: Settings,
    session_factory: SessionFactory,
    new_handler: NewHandler,
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, handler.kind)
    sentinel = await enqueue_committed(session_factory, handler.kind)
    with worker_rig(integration_settings, [handler], kb_settings()) as rig:

        def send(item: UUID) -> None:
            rig.app.send_task(EXECUTE, args=[str(item)], queue=handler.queue)

        # File FIFO : le doublon est consommé avant la sentinelle.
        send(item_id)
        send(item_id)
        send(sentinel)
        await wait_for_status(session_factory, sentinel, "SUCCEEDED")
        await wait_for(lambda: _drained(integration_settings, handler.queue))

    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["attempts"], row["fencing_token"]) == (
        "SUCCEEDED",
        1,
        1,
    )
    assert await effects_of(session_factory, item_id) == [1]


async def _drained(settings: Settings, queue: str) -> bool:
    return queue_depth(settings, queue) == 0


async def test_a_message_acknowledged_then_lost_before_the_claim_is_redispatched(
    integration_settings: Settings,
    session_factory: SessionFactory,
    new_handler: NewHandler,
) -> None:
    # Scénario 7b : le message est consommé et acquitté, puis le worker
    # disparaît avant le claim. RabbitMQ a oublié le message : seule la table
    # permet la reprise.
    handler = new_handler(finalize_with_effect)
    settings = kb_settings(work_redispatch_after_seconds=0.5)
    item_id = await enqueue_committed(session_factory, handler.kind)
    with worker_rig(integration_settings, [handler], settings, consume=False) as rig:
        assert await asyncio.to_thread(run_dispatch_task, rig) == 1
        assert pop_message_with_ack(integration_settings, handler.queue) is True
        assert queue_depth(integration_settings, handler.queue) == 0

        row = await fetch_item(session_factory, item_id)
        assert (row["status"], row["attempts"]) == ("PENDING", 0)
        assert row["dispatched_at"] is not None
        assert await asyncio.to_thread(run_dispatch_task, rig) == 0

        await asyncio.sleep(0.7)

        assert await asyncio.to_thread(run_dispatch_task, rig) == 1
        assert queue_depth(integration_settings, handler.queue) == 1
        with consuming(rig):
            await wait_for_status(session_factory, item_id, "SUCCEEDED")

    assert await effects_of(session_factory, item_id) == [1]


async def test_the_recover_task_requeues_an_expired_lease_and_the_cycle_completes(
    integration_settings: Settings,
    session_factory: SessionFactory,
    new_handler: NewHandler,
) -> None:
    handler = new_handler(finalize_with_effect)
    settings = kb_settings(work_backoff_base_seconds=0.01)
    item_id = await enqueue_committed(session_factory, handler.kind)
    claimed = await SqlAlchemyWorkLifecycle(session_factory).claim(
        item_id, "dead-worker", timedelta(seconds=30)
    )
    assert claimed is not None
    await set_item(
        session_factory, item_id, lease_expires_at="now() - interval '1 second'"
    )
    with worker_rig(integration_settings, [handler], settings) as rig:
        recovered = await asyncio.to_thread(run_recover_task, rig)
        assert (await fetch_item(session_factory, item_id))["status"] == "PENDING"
        await asyncio.sleep(0.1)  # backoff de reprise (<= 10 ms)

        await asyncio.to_thread(run_dispatch_task, rig)
        await wait_for_status(session_factory, item_id, "SUCCEEDED")

    assert recovered == 1
    assert (await fetch_item(session_factory, item_id))["fencing_token"] == 2
    assert await effects_of(session_factory, item_id) == [2]

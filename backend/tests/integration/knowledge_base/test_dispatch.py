"""Dispatch (§8.2) : sélection, plafond en vol, publication après commit, et
scénarios 7, 11 et 11b du §12.2.

API figée :

- ``SqlAlchemyWorkLifecycle.select_dispatchable(queues, batch,
  redispatch_after)`` pose ``dispatched_at`` et commit avant de rendre la liste
  ``[(id, kind)]``. Chaque ``QueueCapacity(queue, kinds, max_in_flight)``
  porte le plafond **de sa file** (S0 §8.2 : « plafond en vol par file »),
  partagé par tous les ``kind`` servis par cette file.
- ``dispatch_pending(resources, settings, *, publish)`` ;
  ``publish(item_id, queue)`` est synchrone et appelé **après** le commit ;
  retourne le nombre d'items notifiés.
- ``recover_expired_leases(resources, settings)`` retourne le nombre d'items
  repris ; le délai est ``compute_backoff`` configuré par les settings.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from knowledge_base import KnowledgeBaseWorkerResources
from knowledge_base.adapters.controller.tasks.dispatch import (
    dispatch_pending,
    recover_expired_leases,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    QueueCapacity,
    SqlAlchemyWorkLifecycle,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.support import (
    NewHandler,
    enqueue_committed,
    fetch_item,
    finalize_with_effect,
    kb_settings,
    queue_depth,
    run_dispatch_task,
    seconds_from_now,
    set_item,
    worker_rig,
)

pytestmark = pytest.mark.integration

KIND = "test.kind"
QUEUE = "test.queue"
OLD = {"dispatched_at": "now() - interval '11 minutes'"}


@pytest.fixture
def lifecycle(session_factory: SessionFactory) -> SqlAlchemyWorkLifecycle:
    return SqlAlchemyWorkLifecycle(session_factory)


async def select(
    lifecycle: SqlAlchemyWorkLifecycle,
    *,
    kinds: tuple[str, ...] = (KIND,),
    batch: int = 10,
    redispatch_after: timedelta = timedelta(seconds=600),
    max_in_flight: int = 100,
) -> list[tuple[UUID, str]]:
    """Une seule file, qui sert ``kinds``."""
    return await lifecycle.select_dispatchable(
        [QueueCapacity(QUEUE, frozenset(kinds), max_in_flight)],
        batch,
        redispatch_after,
    )


# --- sélection ----------------------------------------------------------------------


async def test_select_returns_pending_items_and_marks_them_dispatched(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, KIND)

    assert await select(lifecycle) == [(item_id, KIND)]

    row = await fetch_item(session_factory, item_id)
    assert row["dispatched_at"] is not None
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0


async def test_select_does_not_notify_twice_within_redispatch_after(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    await enqueue_committed(session_factory, KIND)
    await select(lifecycle)

    assert await select(lifecycle) == []


async def test_select_notifies_again_once_redispatch_after_has_elapsed(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, KIND)
    await set_item(session_factory, item_id, **OLD)

    assert await select(lifecycle) == [(item_id, KIND)]
    assert await seconds_from_now(session_factory, item_id, "dispatched_at") > -5


async def test_select_redispatches_with_a_short_redispatch_after_parameter(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    # Horloge accélérée par paramètre, pas par sleep long (scénario 7).
    item_id = await enqueue_committed(session_factory, KIND)
    short = timedelta(milliseconds=300)
    assert await select(lifecycle, redispatch_after=short) == [(item_id, KIND)]
    assert await select(lifecycle, redispatch_after=short) == []

    await asyncio.sleep(0.5)

    assert await select(lifecycle, redispatch_after=short) == [(item_id, KIND)]


async def test_select_only_returns_the_requested_kinds(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    known = await enqueue_committed(session_factory, KIND)
    unknown = await enqueue_committed(session_factory, "extract_version")

    assert await select(lifecycle) == [(known, KIND)]

    row = await fetch_item(session_factory, unknown)
    assert (row["status"], row["attempts"], row["dispatched_at"]) == (
        "PENDING",
        0,
        None,
    )


async def test_select_skips_items_whose_available_at_is_in_the_future(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, KIND)
    await set_item(session_factory, item_id, available_at="now() + interval '1 hour'")

    assert await select(lifecycle) == []


@pytest.mark.parametrize("status", ["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"])
async def test_select_skips_items_that_are_not_pending(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle, status: str
) -> None:
    item_id = await enqueue_committed(session_factory, KIND)
    lease = ", lease_expires_at = now() + interval '1 hour'"
    await set_item(
        session_factory,
        item_id,
        status=f"'{status}'" + (lease if status == "RUNNING" else ""),
    )

    assert await select(lifecycle) == []


async def test_select_respects_the_batch_size_and_oldest_first(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    newest = await enqueue_committed(session_factory, KIND)
    oldest = await enqueue_committed(session_factory, KIND)
    middle = await enqueue_committed(session_factory, KIND)
    await set_item(session_factory, newest, available_at="now() - interval '1 minute'")
    await set_item(session_factory, middle, available_at="now() - interval '2 minutes'")
    await set_item(session_factory, oldest, available_at="now() - interval '3 minutes'")

    assert await select(lifecycle, batch=2) == [(oldest, KIND), (middle, KIND)]


# --- scénario 11 : plafond en vol ---------------------------------------------------


async def test_max_in_flight_bounds_the_number_of_dispatched_items(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    for _ in range(5):
        await enqueue_committed(session_factory, KIND)
    cap = 2

    first = await select(lifecycle, max_in_flight=cap)
    second = await select(lifecycle, max_in_flight=cap)

    assert len(first) == 2
    assert second == []


async def test_max_in_flight_counts_running_items(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    running = await enqueue_committed(session_factory, KIND)
    await enqueue_committed(session_factory, KIND)
    await lifecycle.claim(running, "host:1", timedelta(seconds=30))

    assert await select(lifecycle, max_in_flight=1) == []


async def test_max_in_flight_frees_a_slot_when_an_item_finishes(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    for _ in range(4):
        await enqueue_committed(session_factory, KIND)
    cap = 2
    first = await select(lifecycle, max_in_flight=cap)
    assert await select(lifecycle, max_in_flight=cap) == []

    await set_item(session_factory, first[0][0], status="'CANCELLED'")

    assert len(await select(lifecycle, max_in_flight=cap)) == 1


async def test_max_in_flight_is_shared_by_all_the_kinds_of_a_queue(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    running = await enqueue_committed(session_factory, "test.extract")
    await enqueue_committed(session_factory, "test.extract")
    await enqueue_committed(session_factory, "test.index")
    await lifecycle.claim(running, "host:1", timedelta(seconds=30))

    # Un item d'un kind occupe le créneau : l'autre kind de la même file attend.
    selected = await select(
        lifecycle, kinds=("test.extract", "test.index"), max_in_flight=2
    )
    assert len(selected) == 1
    assert (
        await select(lifecycle, kinds=("test.extract", "test.index"), max_in_flight=2)
        == []
    )


async def test_max_in_flight_is_counted_separately_for_each_queue(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    busy = await enqueue_committed(session_factory, "test.extract")
    await enqueue_committed(session_factory, "test.extract")
    other = await enqueue_committed(session_factory, "test.notify")
    await lifecycle.claim(busy, "host:1", timedelta(seconds=30))
    queues = [
        QueueCapacity("test.ingestion", frozenset({"test.extract"}), 1),
        QueueCapacity("test.light", frozenset({"test.notify"}), 1),
    ]

    # La file d'ingestion est pleine ; l'autre file garde son propre créneau.
    selected = await lifecycle.select_dispatchable(queues, 10, timedelta(seconds=600))

    assert selected == [(other, "test.notify")]


async def test_max_in_flight_ignores_stale_notifications(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    stale = await enqueue_committed(session_factory, KIND)
    fresh = await enqueue_committed(session_factory, KIND)
    await set_item(session_factory, stale, available_at="now() - interval '2 minutes'")
    await set_item(session_factory, stale, **OLD)

    # « en vol » = RUNNING + PENDING notifiés depuis moins de redispatch_after :
    # la notification ancienne ne compte plus, un seul créneau est libre.
    selected = await select(lifecycle, max_in_flight=1)

    assert selected == [(stale, KIND)]
    assert fresh not in [item_id for item_id, _ in selected]


# --- dispatch_pending ---------------------------------------------------------------


async def test_dispatch_publishes_one_message_per_item_to_the_handler_queue(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    first = await enqueue_committed(session_factory, handler.kind)
    second = await enqueue_committed(session_factory, handler.kind)
    published: list[tuple[UUID, str]] = []
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(handler,),
    )

    count = await dispatch_pending(
        resources, kb_settings(), publish=lambda i, q: published.append((i, q))
    )

    assert count == 2
    assert sorted(published) == sorted(
        [(first, handler.queue), (second, handler.queue)]
    )


async def test_dispatch_commits_dispatched_at_before_publishing(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    # Crash entre la transaction courte et la publication : la marque est déjà
    # durable ; le passage suivant (après redispatch_after) rattrape l'item.
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, handler.kind)
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(handler,),
    )

    def crash(_item: UUID, _queue: str) -> None:
        raise RuntimeError("broker injoignable")

    with pytest.raises(RuntimeError, match="broker injoignable"):
        await dispatch_pending(resources, kb_settings(), publish=crash)

    row = await fetch_item(session_factory, item_id)
    assert row["dispatched_at"] is not None
    assert row["status"] == "PENDING"


async def test_dispatch_never_publishes_an_unregistered_kind(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    # Scénario 11b.
    handler = new_handler(finalize_with_effect)
    orphan = await enqueue_committed(session_factory, "extract_version")
    published: list[UUID] = []
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(handler,),
    )

    count = await dispatch_pending(
        resources, kb_settings(), publish=lambda i, _q: published.append(i)
    )

    assert count == 0
    assert published == []
    row = await fetch_item(session_factory, orphan)
    assert (row["status"], row["attempts"], row["dispatched_at"]) == (
        "PENDING",
        0,
        None,
    )


async def test_dispatch_with_an_empty_registry_publishes_nothing(
    session_factory: SessionFactory,
) -> None:
    await enqueue_committed(session_factory, KIND)
    published: list[UUID] = []
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory, loop=asyncio.get_running_loop()
    )

    count = await dispatch_pending(
        resources, kb_settings(), publish=lambda i, _q: published.append(i)
    )

    assert (count, published) == (0, [])


async def test_dispatch_applies_batch_size_and_max_in_flight_from_the_settings(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    for _ in range(6):
        await enqueue_committed(session_factory, handler.kind)
    published: list[UUID] = []
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(handler,),
    )

    def publish(item_id: UUID, _queue: str) -> None:
        published.append(item_id)

    batch = kb_settings(work_dispatch_batch=1, work_max_in_flight=3)
    assert await dispatch_pending(resources, batch, publish=publish) == 1
    assert await dispatch_pending(resources, batch, publish=publish) == 1
    assert await dispatch_pending(resources, batch, publish=publish) == 1
    # Trois notifications en vol : le plafond de 3 est atteint.
    assert await dispatch_pending(resources, batch, publish=publish) == 0
    assert len(set(published)) == 3


async def test_recover_expired_leases_applies_the_backoff_from_the_settings(
    session_factory: SessionFactory, new_handler: NewHandler
) -> None:
    handler = new_handler(finalize_with_effect)
    item_id = await enqueue_committed(session_factory, handler.kind)
    lifecycle = SqlAlchemyWorkLifecycle(session_factory)
    await lifecycle.claim(item_id, "host:1", timedelta(seconds=30))
    await set_item(
        session_factory, item_id, lease_expires_at="now() - interval '1 second'"
    )
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(handler,),
    )

    recovered = await recover_expired_leases(
        resources, kb_settings(work_backoff_base_seconds=40)
    )

    assert recovered == 1
    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["last_error_code"]) == ("PENDING", "LEASE_EXPIRED")
    # attempts = 1 : d = 40 s, equal jitter dans [20, 40].
    assert 15 < await seconds_from_now(session_factory, item_id, "available_at") <= 40


# --- scénario 7 : message jamais consommé, avec le vrai RabbitMQ --------------------


async def test_a_notification_that_nobody_consumes_is_published_again(
    integration_settings: Settings,
    session_factory: SessionFactory,
    new_handler: NewHandler,
) -> None:
    handler = new_handler(finalize_with_effect)
    settings = kb_settings(work_redispatch_after_seconds=0.4)
    item_id = await enqueue_committed(session_factory, handler.kind)
    with worker_rig(integration_settings, [handler], settings, consume=False) as rig:
        assert await asyncio.to_thread(run_dispatch_task, rig) == 1
        assert queue_depth(integration_settings, handler.queue) == 1

        # Tant que redispatch_after n'est pas écoulé : pas de doublon de réveil.
        assert await asyncio.to_thread(run_dispatch_task, rig) == 0
        assert queue_depth(integration_settings, handler.queue) == 1

        await asyncio.sleep(0.6)

        assert await asyncio.to_thread(run_dispatch_task, rig) == 1
        assert queue_depth(integration_settings, handler.queue) == 2
    assert (await fetch_item(session_factory, item_id))["status"] == "PENDING"

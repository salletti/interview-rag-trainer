"""Scénarios 1 à 3 du §12.2 : ``enqueue`` et ``cancel`` dans la transaction
métier (port ``WorkQueue``, adapter ``SqlAlchemyWorkQueue``).

API figée : ``SqlAlchemyWorkQueue(session)`` implémente
``enqueue(kind, dedup_key, payload=None, correlation_id=None, max_attempts=None)``
et ``cancel(dedup_key)``. Il n'ouvre ni ne valide jamais de transaction.
"""

from __future__ import annotations

import uuid

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)
from runtime.database import SessionFactory
from tests.integration.knowledge_base.support import (
    cancel_committed,
    count_items,
    enqueue_committed,
    fetch_item,
    fetch_scalar,
    set_item,
)

pytestmark = pytest.mark.integration


async def test_enqueue_then_rollback_leaves_no_row(
    session_factory: SessionFactory,
) -> None:
    session = session_factory()
    try:
        await SqlAlchemyWorkQueue(session).enqueue("test.kind", "test.kind:1")
        await session.rollback()
    finally:
        await session.close()

    assert await count_items(session_factory) == 0


async def test_enqueue_then_commit_creates_one_pending_row(
    session_factory: SessionFactory,
) -> None:
    correlation_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        item_id = await SqlAlchemyWorkQueue(session).enqueue(
            "test.kind",
            "test.kind:1",
            {"version_id": "v-1"},
            correlation_id,
        )

    row = await fetch_item(session_factory, item_id)
    assert await count_items(session_factory) == 1
    assert row["status"] == "PENDING"
    assert row["kind"] == "test.kind"
    assert row["dedup_key"] == "test.kind:1"
    assert row["payload"] == {"version_id": "v-1"}
    assert row["correlation_id"] == correlation_id
    assert row["attempts"] == 0
    assert row["max_attempts"] == 5
    assert row["fencing_token"] == 0
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["dispatched_at"] is None
    assert row["completed_at"] is None
    assert await fetch_scalar(
        session_factory,
        "SELECT available_at <= now() FROM knowledge_base.work_items WHERE id = :id",
        id=item_id,
    )


async def test_enqueue_is_invisible_until_the_business_transaction_commits(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        await SqlAlchemyWorkQueue(session).enqueue("test.kind", "test.kind:1")

        # Même transaction que l'état métier : une autre session ne voit rien.
        assert await count_items(session_factory) == 0

    assert await count_items(session_factory) == 1


async def test_enqueue_honours_an_explicit_max_attempts(
    session_factory: SessionFactory,
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", max_attempts=2)

    assert (await fetch_item(session_factory, item_id))["max_attempts"] == 2


async def test_enqueue_defaults_to_an_empty_payload(
    session_factory: SessionFactory,
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")

    assert (await fetch_item(session_factory, item_id))["payload"] == {}


async def test_two_enqueues_with_the_same_dedup_key_share_one_row(
    session_factory: SessionFactory,
) -> None:
    first = await enqueue_committed(
        session_factory, "test.kind", "test.kind:1", payload={"n": "first"}
    )
    second = await enqueue_committed(
        session_factory, "test.kind", "test.kind:1", payload={"n": "second"}
    )

    assert second == first
    assert await count_items(session_factory) == 1
    assert (await fetch_item(session_factory, first))["payload"] == {"n": "first"}


async def test_dedup_key_stays_unique_for_finished_items(
    session_factory: SessionFactory,
) -> None:
    first = await enqueue_committed(session_factory, "test.kind", "test.kind:1")
    await set_item(session_factory, first, status="'SUCCEEDED'", completed_at="now()")

    again = await enqueue_committed(session_factory, "test.kind", "test.kind:1")

    assert again == first
    assert (await fetch_item(session_factory, first))["status"] == "SUCCEEDED"
    assert await count_items(session_factory) == 1


async def test_different_dedup_keys_create_distinct_items(
    session_factory: SessionFactory,
) -> None:
    first = await enqueue_committed(session_factory, "test.kind", "test.kind:1")
    second = await enqueue_committed(session_factory, "test.kind", "test.kind:2")

    assert first != second
    assert await count_items(session_factory) == 2


async def test_cancel_a_pending_item_cancels_it_and_bumps_the_fencing_token(
    session_factory: SessionFactory,
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "test.kind:1")

    assert await cancel_committed(session_factory, "test.kind:1") is True

    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "CANCELLED"
    assert row["fencing_token"] == 1
    assert row["completed_at"] is not None


async def test_cancel_an_unknown_dedup_key_returns_false(
    session_factory: SessionFactory,
) -> None:
    assert await cancel_committed(session_factory, "test.kind:unknown") is False


async def test_cancel_twice_is_a_no_op_the_second_time(
    session_factory: SessionFactory,
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "test.kind:1")
    await cancel_committed(session_factory, "test.kind:1")

    assert await cancel_committed(session_factory, "test.kind:1") is False
    assert (await fetch_item(session_factory, item_id))["fencing_token"] == 1


async def test_cancel_does_not_touch_a_succeeded_item(
    session_factory: SessionFactory,
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "test.kind:1")
    await set_item(session_factory, item_id, status="'SUCCEEDED'", completed_at="now()")

    assert await cancel_committed(session_factory, "test.kind:1") is False
    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "SUCCEEDED"
    assert row["fencing_token"] == 0

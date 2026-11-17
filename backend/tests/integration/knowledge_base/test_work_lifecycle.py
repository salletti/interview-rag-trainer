"""Transitions SQL du §3 : claim, heartbeat, complete, retry_later, fail,
recover. Un retour ``False``/``None`` signifie « 0 ligne modifiée », jamais une
erreur technique.

API figée : ``SqlAlchemyWorkLifecycle(session_factory)`` ; ``complete`` et
``fail`` reçoivent la session de la transaction de finalisation (le test
commit) ; ``recover_expired(batch, *, backoff)`` où ``backoff(attempts)`` donne
le délai de ``available_at`` (calcul Python, appliqué par ligne).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from uuid import UUID

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkLifecycle,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from runtime.database import SessionFactory
from tests.integration.knowledge_base.support import (
    cancel_committed,
    enqueue_committed,
    fetch_item,
    seconds_from_now,
    set_item,
)

pytestmark = pytest.mark.integration

TTL = timedelta(seconds=30)
EXPIRED = {"lease_expires_at": "now() - interval '1 second'"}


@pytest.fixture
def lifecycle(session_factory: SessionFactory) -> SqlAlchemyWorkLifecycle:
    return SqlAlchemyWorkLifecycle(session_factory)


async def complete(
    factory: SessionFactory,
    lifecycle: SqlAlchemyWorkLifecycle,
    item_id: UUID,
    token: int,
) -> bool:
    async with factory() as session, session.begin():
        return await lifecycle.complete(session, item_id, token)


async def fail(
    factory: SessionFactory,
    lifecycle: SqlAlchemyWorkLifecycle,
    item_id: UUID,
    token: int,
    error: FailureInfo,
) -> bool:
    async with factory() as session, session.begin():
        return await lifecycle.fail(session, item_id, token, error)


# --- claim --------------------------------------------------------------------------


async def test_claim_moves_a_pending_item_to_running_with_a_lease_and_a_token(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(
        session_factory, "test.kind", payload={"k": "v"}, max_attempts=3
    )

    claimed = await lifecycle.claim(item_id, "host:1", TTL)

    assert claimed is not None
    assert claimed.id == item_id
    assert claimed.kind == "test.kind"
    assert claimed.payload == {"k": "v"}
    assert claimed.attempts == 1
    assert claimed.max_attempts == 3
    assert claimed.fencing_token == 1
    assert claimed.correlation_id is None
    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "RUNNING"
    assert row["attempts"] == 1
    assert row["fencing_token"] == 1
    assert row["lease_owner"] == "host:1"
    remaining = await seconds_from_now(session_factory, item_id, "lease_expires_at")
    assert 25 < remaining <= 30


async def test_claim_a_second_time_returns_none_and_changes_nothing(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    assert await lifecycle.claim(item_id, "host:2", TTL) is None

    row = await fetch_item(session_factory, item_id)
    assert (row["attempts"], row["fencing_token"], row["lease_owner"]) == (
        1,
        1,
        "host:1",
    )


async def test_claim_ignores_an_item_not_yet_available(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await set_item(session_factory, item_id, available_at="now() + interval '1 hour'")

    assert await lifecycle.claim(item_id, "host:1", TTL) is None
    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["attempts"]) == ("PENDING", 0)


async def test_claim_ignores_a_cancelled_item_and_an_unknown_id(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "k:1")
    await cancel_committed(session_factory, "k:1")

    assert await lifecycle.claim(item_id, "host:1", TTL) is None
    assert await lifecycle.claim(uuid.uuid4(), "host:1", TTL) is None


# --- heartbeat ----------------------------------------------------------------------


async def test_heartbeat_with_the_current_token_extends_the_lease(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    claimed = await lifecycle.claim(item_id, "host:1", timedelta(seconds=5))
    assert claimed is not None

    assert await lifecycle.heartbeat(item_id, claimed.fencing_token, TTL) is True

    assert await seconds_from_now(session_factory, item_id, "lease_expires_at") > 20


async def test_heartbeat_with_a_stale_token_returns_false(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    claimed = await lifecycle.claim(item_id, "host:1", timedelta(seconds=5))
    assert claimed is not None

    assert await lifecycle.heartbeat(item_id, claimed.fencing_token + 1, TTL) is False
    assert await seconds_from_now(session_factory, item_id, "lease_expires_at") <= 5


async def test_heartbeat_after_cancellation_returns_false(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "k:1")
    claimed = await lifecycle.claim(item_id, "host:1", TTL)
    assert claimed is not None
    await cancel_committed(session_factory, "k:1")

    assert await lifecycle.heartbeat(item_id, claimed.fencing_token, TTL) is False
    assert await lifecycle.heartbeat(item_id, claimed.fencing_token + 1, TTL) is False


# --- complete -----------------------------------------------------------------------


async def test_complete_with_the_current_token_succeeds_and_clears_the_lease(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    claimed = await lifecycle.claim(item_id, "host:1", TTL)
    assert claimed is not None

    assert await complete(session_factory, lifecycle, item_id, 1) is True

    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "SUCCEEDED"
    assert row["completed_at"] is not None
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None


async def test_complete_with_a_wrong_token_changes_nothing(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    assert await complete(session_factory, lifecycle, item_id, 99) is False
    assert (await fetch_item(session_factory, item_id))["status"] == "RUNNING"


async def test_complete_a_pending_item_returns_false(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")

    assert await complete(session_factory, lifecycle, item_id, 0) is False
    assert (await fetch_item(session_factory, item_id))["status"] == "PENDING"


async def test_complete_by_a_superseded_attempt_is_refused_after_a_reclaim(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "A", TTL)
    await set_item(session_factory, item_id, **EXPIRED)
    assert await lifecycle.recover_expired(10, backoff=lambda _n: timedelta(0)) == 1
    reclaimed = await lifecycle.claim(item_id, "B", TTL)
    assert reclaimed is not None
    assert reclaimed.fencing_token == 2

    assert await complete(session_factory, lifecycle, item_id, 1) is False
    assert (await fetch_item(session_factory, item_id))["status"] == "RUNNING"
    assert await complete(session_factory, lifecycle, item_id, 2) is True


async def test_complete_after_cancellation_is_refused(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", "k:1")
    await lifecycle.claim(item_id, "host:1", TTL)
    await cancel_committed(session_factory, "k:1")

    assert await complete(session_factory, lifecycle, item_id, 1) is False
    assert await complete(session_factory, lifecycle, item_id, 2) is False
    assert (await fetch_item(session_factory, item_id))["status"] == "CANCELLED"


# --- retry_later --------------------------------------------------------------------


async def test_retry_later_puts_the_item_back_to_pending_with_a_delay(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)
    await set_item(session_factory, item_id, dispatched_at="now()")

    done = await lifecycle.retry_later(
        item_id,
        1,
        FailureInfo("TRANSIENT", "réseau coupé"),
        timedelta(seconds=120),
    )

    assert done is True
    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "PENDING"
    assert row["attempts"] == 1
    assert row["dispatched_at"] is None
    assert row["last_error_code"] == "TRANSIENT"
    assert row["last_error_message"] == "réseau coupé"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert 110 < await seconds_from_now(session_factory, item_id, "available_at") <= 120


async def test_retry_later_with_a_wrong_token_changes_nothing(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    done = await lifecycle.retry_later(
        item_id, 2, FailureInfo("TRANSIENT", "x"), timedelta(seconds=1)
    )

    assert done is False
    assert (await fetch_item(session_factory, item_id))["status"] == "RUNNING"


async def test_retry_later_is_refused_when_no_attempt_is_left(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", max_attempts=1)
    await lifecycle.claim(item_id, "host:1", TTL)

    done = await lifecycle.retry_later(
        item_id, 1, FailureInfo("TRANSIENT", "x"), timedelta(seconds=1)
    )

    assert done is False
    assert (await fetch_item(session_factory, item_id))["status"] == "RUNNING"


# --- fail ---------------------------------------------------------------------------


async def test_fail_records_the_error_and_the_completion_date(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    done = await fail(
        session_factory, lifecycle, item_id, 1, FailureInfo("BAD_INPUT", "illisible")
    )

    assert done is True
    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "FAILED"
    assert row["last_error_code"] == "BAD_INPUT"
    assert row["last_error_message"] == "illisible"
    assert row["completed_at"] is not None


async def test_fail_truncates_the_message_to_2000_characters(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    await fail(session_factory, lifecycle, item_id, 1, FailureInfo("BOOM", "x" * 5000))

    message = (await fetch_item(session_factory, item_id))["last_error_message"]
    assert message == "x" * 2000


async def test_fail_with_a_wrong_token_changes_nothing(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    done = await fail(
        session_factory, lifecycle, item_id, 7, FailureInfo("BAD_INPUT", "x")
    )

    assert done is False
    row = await fetch_item(session_factory, item_id)
    assert (row["status"], row["last_error_code"]) == ("RUNNING", None)


# --- recover ------------------------------------------------------------------------


async def test_recover_puts_an_expired_item_back_to_pending_with_backoff(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", max_attempts=5)
    await lifecycle.claim(item_id, "host:1", TTL)
    await set_item(session_factory, item_id, dispatched_at="now()", **EXPIRED)
    seen: list[int] = []

    def backoff(attempts: int) -> timedelta:
        seen.append(attempts)
        return timedelta(seconds=60)

    assert await lifecycle.recover_expired(10, backoff=backoff) == 1

    row = await fetch_item(session_factory, item_id)
    assert seen == [1]
    assert row["status"] == "PENDING"
    assert row["last_error_code"] == "LEASE_EXPIRED"
    assert row["attempts"] == 1
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["dispatched_at"] is None
    assert 50 < await seconds_from_now(session_factory, item_id, "available_at") <= 60


async def test_recover_fails_an_expired_item_without_attempts_left(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind", max_attempts=1)
    await lifecycle.claim(item_id, "host:1", TTL)
    await set_item(session_factory, item_id, **EXPIRED)

    assert await lifecycle.recover_expired(10, backoff=lambda _n: timedelta(0)) == 1

    row = await fetch_item(session_factory, item_id)
    assert row["status"] == "FAILED"
    assert row["last_error_code"] == "LEASE_EXPIRED_MAX_ATTEMPTS"
    assert row["completed_at"] is not None


async def test_recover_leaves_a_live_lease_alone(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)

    assert await lifecycle.recover_expired(10, backoff=lambda _n: timedelta(0)) == 0

    assert (await fetch_item(session_factory, item_id))["status"] == "RUNNING"


async def test_recover_ignores_items_that_are_not_running(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    pending = await enqueue_committed(session_factory, "test.kind")

    assert await lifecycle.recover_expired(10, backoff=lambda _n: timedelta(0)) == 0

    assert (await fetch_item(session_factory, pending))["status"] == "PENDING"


async def test_recover_processes_at_most_one_batch(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    ids = [await enqueue_committed(session_factory, "test.kind") for _ in range(3)]
    for item_id in ids:
        await lifecycle.claim(item_id, "host:1", TTL)
        await set_item(session_factory, item_id, **EXPIRED)

    assert await lifecycle.recover_expired(2, backoff=lambda _n: timedelta(0)) == 2
    assert await lifecycle.recover_expired(2, backoff=lambda _n: timedelta(0)) == 1

    statuses = [(await fetch_item(session_factory, i))["status"] for i in ids]
    assert statuses == ["PENDING"] * 3


async def test_a_recovered_item_can_be_claimed_again_with_a_new_token(
    session_factory: SessionFactory, lifecycle: SqlAlchemyWorkLifecycle
) -> None:
    item_id = await enqueue_committed(session_factory, "test.kind")
    await lifecycle.claim(item_id, "host:1", TTL)
    await set_item(session_factory, item_id, **EXPIRED)
    await lifecycle.recover_expired(10, backoff=lambda _n: timedelta(0))

    claimed = await lifecycle.claim(item_id, "host:2", TTL)

    assert claimed is not None
    assert (claimed.attempts, claimed.fencing_token) == (2, 2)

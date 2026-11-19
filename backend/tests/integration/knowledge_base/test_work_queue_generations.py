"""Générations d'un travail (S8 ; port ``WorkQueue``, adapter
``SqlAlchemyWorkQueue`` ; knowledge-base.md §6, précisions S8).

``dedup_key`` est unique pour toujours (S0 §2) : un travail à refaire après un
item terminé prend la génération suivante, ``<clé>:<n>``
(``enqueue_again``) ; la clé et ses générations forment une famille, que
``cancel`` annule en entier.

Revue S9 (I4) : ``reuse_running=False`` ne réutilise qu'un item ``PENDING`` ;
un item ``RUNNING`` a pu lire l'état avant l'écriture qui rend le travail
nécessaire, la génération suivante est alors créée (synchronisation de
visibilité après la réécriture des points).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)
from runtime.database import SessionFactory

pytestmark = pytest.mark.integration
KIND = "test.kind"


async def again(
    factory: SessionFactory, key: str, *, reuse_running: bool = True
) -> None:
    async with factory() as session, session.begin():
        await SqlAlchemyWorkQueue(session).enqueue_again(
            KIND, key, {"x": "1"}, reuse_running=reuse_running
        )


async def start_running(factory: SessionFactory) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET status = 'RUNNING', "
                "lease_expires_at = now() + interval '1 minute' "
                "WHERE status = 'PENDING'"
            )
        )


async def cancel(factory: SessionFactory, key: str) -> bool:
    async with factory() as session, session.begin():
        return await SqlAlchemyWorkQueue(session).cancel(key)


async def finish(factory: SessionFactory, status: str = "SUCCEEDED") -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET status = :s, "
                "completed_at = now() WHERE status = 'PENDING'"
            ),
            {"s": status},
        )


async def keys(factory: SessionFactory) -> dict[str, str]:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT dedup_key, status FROM knowledge_base.work_items")
        )
        return {str(key): str(status) for key, status in rows}


async def test_an_empty_family_gets_its_base_key(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")

    assert await keys(session_factory) == {"w:1": "PENDING"}


async def test_a_pending_member_is_returned_and_nothing_is_added(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")
    await again(session_factory, "w:1")

    assert await keys(session_factory) == {"w:1": "PENDING"}


async def test_a_running_member_is_returned_by_default(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")
    await start_running(session_factory)

    await again(session_factory, "w:1")

    assert await keys(session_factory) == {"w:1": "RUNNING"}


async def test_without_reusing_running_members_a_running_one_gets_a_successor(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")
    await start_running(session_factory)

    await again(session_factory, "w:1", reuse_running=False)
    await again(session_factory, "w:1", reuse_running=False)

    # le successeur PENDING est réutilisé : un seul de plus
    assert await keys(session_factory) == {"w:1": "RUNNING", "w:1:2": "PENDING"}


async def test_after_terminated_members_comes_the_next_generation(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")
    await finish(session_factory)
    await again(session_factory, "w:1")
    await finish(session_factory, "FAILED")
    await again(session_factory, "w:1")

    assert await keys(session_factory) == {
        "w:1": "SUCCEEDED",
        "w:1:2": "FAILED",
        "w:1:3": "PENDING",
    }


async def test_cancel_reaches_every_live_generation_of_the_family(
    session_factory: SessionFactory,
) -> None:
    await again(session_factory, "w:1")
    await finish(session_factory)
    await again(session_factory, "w:1")

    assert await cancel(session_factory, "w:1") is True

    assert await keys(session_factory) == {"w:1": "SUCCEEDED", "w:1:2": "CANCELLED"}
    assert await cancel(session_factory, "w:1") is False


async def test_a_key_is_never_a_pattern_and_a_longer_key_is_another_family(
    session_factory: SessionFactory,
) -> None:
    for key in ("a_b", "axb:2", "a%b", "a_b1"):
        await again(session_factory, key)

    await cancel(session_factory, "a_b")
    await again(session_factory, "a%b")

    assert await keys(session_factory) == {
        "a_b": "CANCELLED",
        "axb:2": "PENDING",
        "a%b": "PENDING",
        "a_b1": "PENDING",
    }

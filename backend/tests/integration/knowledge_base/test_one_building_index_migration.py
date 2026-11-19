"""Migration de la revue S9 : au plus un index ``BUILDING`` par corpus, index
unique partiel ``ix_search_indexes_one_building`` qui double l'invariant de
``KnowledgeCorpus.provision_index`` (comme ``ix_search_indexes_one_active``
double I10).

Même convention que les migrations de S2 à S8 : aller-retour ancré sur les
révisions.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]

ONE_BUILDING_REVISION = "40038a21e22f"
BEFORE_ONE_BUILDING_REVISION = "5d2f8c1a7b30"  # work_items.reopen_count (S8)

INSERT_INDEX = text(
    "INSERT INTO knowledge_base.search_indexes (index_id, corpus_id, status, "
    "embedding_profile, chunking_policy, created_at) VALUES "
    "(:i, :c, :s, '{}'::jsonb, '{}'::jsonb, now())"
)


def alembic_ok(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)}\n{result.stderr}"
    return result.stdout + result.stderr


@pytest.fixture
def restore_head() -> Iterator[None]:
    yield
    alembic_ok("upgrade", "head")


@pytest.fixture
async def corpus(session_factory: SessionFactory) -> AsyncIterator[UUID]:
    """Un corpus de test, sans index ; tables vidées après le test."""
    corpus_id = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.knowledge_corpus "
                "(corpus_id, active_index_id, version) VALUES (:c, NULL, 0)"
            ),
            {"c": corpus_id},
        )
    try:
        yield corpus_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(text("DELETE FROM knowledge_base.search_indexes"))
            await session.execute(text("DELETE FROM knowledge_base.knowledge_corpus"))


async def insert_index(factory: SessionFactory, corpus_id: UUID, status: str) -> None:
    async with factory() as session, session.begin():
        await session.execute(INSERT_INDEX, {"i": uuid4(), "c": corpus_id, "s": status})


async def test_a_second_building_index_of_the_same_corpus_is_refused(
    session_factory: SessionFactory, corpus: UUID
) -> None:
    await insert_index(session_factory, corpus, "BUILDING")

    with pytest.raises(IntegrityError, match="ix_search_indexes_one_building"):
        await insert_index(session_factory, corpus, "BUILDING")


async def test_a_building_index_lives_beside_the_active_and_retired_ones(
    session_factory: SessionFactory, corpus: UUID
) -> None:
    for status in ("ACTIVE", "RETIRED", "RETIRED", "BUILDING"):
        await insert_index(session_factory, corpus, status)

    async with session_factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM knowledge_base.search_indexes "
                "WHERE corpus_id = :c"
            ),
            {"c": corpus},
        )
    assert count == 4


async def test_downgrade_drops_the_index_and_upgrade_restores_it(
    session_factory: SessionFactory, restore_head: None
) -> None:
    query = text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'knowledge_base' "
        "AND indexname = 'ix_search_indexes_one_building'"
    )
    alembic_ok("upgrade", "head")

    alembic_ok("downgrade", BEFORE_ONE_BUILDING_REVISION)

    assert BEFORE_ONE_BUILDING_REVISION in alembic_ok("current")
    async with session_factory() as session:
        assert (await session.execute(query)).first() is None

    alembic_ok("upgrade", ONE_BUILDING_REVISION)

    assert ONE_BUILDING_REVISION in alembic_ok("current")
    async with session_factory() as session:
        definition = await session.scalar(query)
    assert definition is not None
    assert "UNIQUE" in definition
    assert "(corpus_id)" in definition
    assert "'BUILDING'::text" in definition

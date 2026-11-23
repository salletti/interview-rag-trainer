"""Migration de revue S4 : ``knowledge_corpus.active_index_id`` désigne un index
de CE corpus (clé étrangère composite ``DEFERRABLE INITIALLY DEFERRED`` vers
``search_indexes(corpus_id, index_id)``, et sa contrainte UNIQUE cible).

L'aller-retour est ancré sur les révisions (``FK_REVISION`` et la précédente),
pas sur ``downgrade -1`` (même convention que S2 à S4). L'absence de dérive
entre ``tables.py`` et les migrations est vérifiée par ``alembic check``
(``test_indexing_migration``).
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

FK_REVISION = "c43c7e850745"
BEFORE_FK_REVISION = "448949d0e134"  # tables d'indexation (S4)

ACTIVE_INDEX_FK = (
    "FOREIGN KEY (corpus_id, active_index_id) REFERENCES "
    "knowledge_base.search_indexes(corpus_id, index_id) "
    "DEFERRABLE INITIALLY DEFERRED"
)
INSERT_CORPUS = text(
    "INSERT INTO knowledge_base.knowledge_corpus (corpus_id, active_index_id, "
    "version) VALUES (:c, :a, 0)"
)
INSERT_INDEX = text(
    "INSERT INTO knowledge_base.search_indexes (index_id, corpus_id, status, "
    "embedding_profile, chunking_policy, created_at) VALUES "
    "(:i, :c, :s, '{}'::jsonb, '{}'::jsonb, now())"
)


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def alembic_ok(*args: str) -> str:
    result = alembic(*args)
    assert result.returncode == 0, f"alembic {' '.join(args)}\n{result.stderr}"
    return result.stdout + result.stderr


async def constraints(factory: SessionFactory, table: str, kind: str) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'knowledge_base' AND t.relname = :t "
                "AND CAST(c.contype AS text) = :k"
            ),
            {"t": table, "k": kind},
        )
        return [str(row[0]) for row in rows]


@pytest.fixture
def restore_head() -> Iterator[None]:
    yield
    alembic("upgrade", "head")


@pytest.fixture(autouse=True)
async def clean_corpus(session_factory: SessionFactory) -> AsyncIterator[None]:
    yield
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM knowledge_base.indexations"))
        await session.execute(text("DELETE FROM knowledge_base.search_indexes"))
        await session.execute(text("DELETE FROM knowledge_base.knowledge_corpus"))


async def add_corpus_with_index(
    session_factory: SessionFactory, *, status: str = "ACTIVE"
) -> tuple[UUID, UUID]:
    """Corpus écrit AVANT son index actif, dans une seule transaction : l'ordre
    du provisionnement, accepté parce que le contrôle est différé au commit."""
    corpus_id, index_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(INSERT_CORPUS, {"c": corpus_id, "a": index_id})
        await session.execute(
            INSERT_INDEX, {"i": index_id, "c": corpus_id, "s": status}
        )
    return corpus_id, index_id


async def test_the_active_index_is_a_deferred_composite_foreign_key(
    session_factory: SessionFactory,
) -> None:
    assert ACTIVE_INDEX_FK in await constraints(
        session_factory, "knowledge_corpus", "f"
    )
    assert "UNIQUE (corpus_id, index_id)" in await constraints(
        session_factory, "search_indexes", "u"
    )


async def test_a_corpus_written_before_its_active_index_commits(
    session_factory: SessionFactory,
) -> None:
    corpus_id, index_id = await add_corpus_with_index(session_factory)

    async with session_factory() as session:
        active = await session.scalar(
            text(
                "SELECT active_index_id FROM knowledge_base.knowledge_corpus "
                "WHERE corpus_id = :c"
            ),
            {"c": corpus_id},
        )
    assert UUID(str(active)) == index_id


async def test_an_unknown_active_index_is_refused_at_commit(
    session_factory: SessionFactory,
) -> None:
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(INSERT_CORPUS, {"c": uuid4(), "a": uuid4()})


async def test_the_index_of_another_corpus_cannot_be_active(
    session_factory: SessionFactory,
) -> None:
    _, foreign_index = await add_corpus_with_index(session_factory)

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(INSERT_CORPUS, {"c": uuid4(), "a": foreign_index})


async def test_the_active_index_cannot_be_deleted_alone(
    session_factory: SessionFactory,
) -> None:
    _, index_id = await add_corpus_with_index(session_factory)

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM knowledge_base.search_indexes WHERE index_id = :i"),
                {"i": index_id},
            )


async def test_a_corpus_without_active_index_is_allowed(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(INSERT_CORPUS, {"c": uuid4(), "a": None})


async def test_downgrade_removes_the_constraints_and_upgrade_restores_them(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")

    alembic_ok("downgrade", BEFORE_FK_REVISION)

    assert BEFORE_FK_REVISION in alembic_ok("current")
    assert ACTIVE_INDEX_FK not in await constraints(
        session_factory, "knowledge_corpus", "f"
    )
    assert "UNIQUE (corpus_id, index_id)" not in await constraints(
        session_factory, "search_indexes", "u"
    )

    alembic_ok("upgrade", FK_REVISION)

    assert FK_REVISION in alembic_ok("current")
    assert ACTIVE_INDEX_FK in await constraints(
        session_factory, "knowledge_corpus", "f"
    )

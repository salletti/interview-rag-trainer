"""Migration S4 : ``knowledge_corpus``, ``search_indexes`` et ``indexations``
(schéma ``knowledge_base``). Le schéma est figé ici (noms, types, nullabilité,
contraintes) ; Alembic reste l'unique créateur (aucun ``create_all``). La
migration S4 est UNE révision au-dessus de celle de S3.

Choix : le corpus est un singleton ; l'invariant I10 est porté par l'agrégat,
doublé d'un index unique partiel (au plus un index ``ACTIVE`` par corpus) ; les
colonnes de cohérence suivent la convention de ``document_versions`` (une
``failure_code`` si et seulement si ``FAILED``).

L'aller-retour est ancré sur les révisions (redescente jusqu'à la révision
précédente, ``BEFORE_S4_REVISION``), pas sur ``downgrade -1`` : une migration
postérieure à S4 ne doit pas fausser ce test (même convention que S2 et S3).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]
S4_TABLES = ("knowledge_corpus", "search_indexes", "indexations")
S4_REVISION = "448949d0e134"
BEFORE_S4_REVISION = "756db53032ab"  # trigger I1 de document_versions (revue S2)

# colonne -> (type PostgreSQL, nullable)
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "knowledge_corpus": {
        "corpus_id": ("uuid", False),
        "active_index_id": ("uuid", True),
        "version": ("integer", False),
    },
    "search_indexes": {
        "index_id": ("uuid", False),
        "corpus_id": ("uuid", False),
        "status": ("text", False),
        "embedding_profile": ("jsonb", False),
        "chunking_policy": ("jsonb", False),
        "created_at": ("timestamp with time zone", False),
        "activated_at": ("timestamp with time zone", True),
        "retired_at": ("timestamp with time zone", True),
    },
    "indexations": {
        "version_id": ("uuid", False),
        "index_id": ("uuid", False),
        "status": ("text", False),
        "chunking_policy_version": ("text", False),
        "manifest_digest": ("text", True),
        "point_count": ("integer", True),
        "completed_at": ("timestamp with time zone", True),
        "failure_code": ("text", True),
        "failure_message": ("text", True),
        "failure_retryable": ("boolean", True),
    },
}


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


@pytest.fixture
def restore_head() -> Iterator[None]:
    yield
    alembic("upgrade", "head")


async def existing(factory: SessionFactory, table: str) -> bool:
    async with factory() as session:
        found = await session.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"knowledge_base.{table}"},
        )
    return bool(found)


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


@pytest.mark.parametrize("table", S4_TABLES)
async def test_columns_match_the_design(
    session_factory: SessionFactory, table: str
) -> None:
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'knowledge_base' AND table_name = :t"
            ),
            {"t": table},
        )
        actual = {name: (kind, nullable == "YES") for name, kind, nullable in rows}

    assert actual == EXPECTED_COLUMNS[table]


async def test_downgrade_removes_the_s4_tables_and_keeps_the_s3_ones(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")
    for table in (*S4_TABLES, "document_chunks", "document_versions"):
        assert await existing(session_factory, table)

    alembic_ok("downgrade", BEFORE_S4_REVISION)

    assert BEFORE_S4_REVISION in alembic_ok("current")
    for table in S4_TABLES:
        assert not await existing(session_factory, table)
    assert await existing(session_factory, "document_chunks")
    assert await existing(session_factory, "document_versions")

    alembic_ok("upgrade", S4_REVISION)
    assert S4_REVISION in alembic_ok("current")
    for table in S4_TABLES:
        assert await existing(session_factory, table)

    alembic_ok("upgrade", "head")
    for table in S4_TABLES:
        assert await existing(session_factory, table)


async def test_the_models_and_the_migration_do_not_drift() -> None:
    alembic_ok("check")


async def test_primary_keys_are_the_logical_identities(
    session_factory: SessionFactory,
) -> None:
    assert await constraints(session_factory, "knowledge_corpus", "p") == [
        "PRIMARY KEY (corpus_id)"
    ]
    assert await constraints(session_factory, "search_indexes", "p") == [
        "PRIMARY KEY (index_id)"
    ]
    assert await constraints(session_factory, "indexations", "p") == [
        "PRIMARY KEY (version_id, index_id)"
    ]


async def test_an_indexation_references_its_version_and_its_index(
    session_factory: SessionFactory,
) -> None:
    foreign = await constraints(session_factory, "indexations", "f")

    assert any(
        "FOREIGN KEY (version_id) REFERENCES knowledge_base.document_versions(id)" in d
        for d in foreign
    )
    assert any(
        "FOREIGN KEY (index_id) REFERENCES knowledge_base.search_indexes(index_id)" in d
        for d in foreign
    )


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("search_indexes", ("BUILDING", "ACTIVE", "RETIRED")),
        ("indexations", ("PENDING", "COMPLETED", "FAILED", "CANCELLED")),
    ],
)
async def test_statuses_are_limited_to_the_state_diagram(
    session_factory: SessionFactory, table: str, values: tuple[str, ...]
) -> None:
    checks = " ".join(await constraints(session_factory, table, "c"))

    for value in values:
        assert value in checks


async def test_an_indexation_is_completed_if_and_only_if_it_has_a_manifest_digest(
    session_factory: SessionFactory,
) -> None:
    checks = " ".join(await constraints(session_factory, "indexations", "c"))

    assert "manifest_digest IS NOT NULL" in checks
    assert "failure_code IS NOT NULL" in checks


async def test_at_most_one_active_index_per_corpus(
    session_factory: SessionFactory,
) -> None:
    corpus_id, first, second = uuid4(), uuid4(), uuid4()
    insert_index = text(
        "INSERT INTO knowledge_base.search_indexes (index_id, corpus_id, status, "
        "embedding_profile, chunking_policy, created_at) VALUES "
        "(:i, :c, 'ACTIVE', '{}'::jsonb, '{}'::jsonb, now())"
    )
    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_base.knowledge_corpus "
                    "(corpus_id, active_index_id, version) VALUES (:c, NULL, 0)"
                ),
                {"c": corpus_id},
            )
            await session.execute(insert_index, {"i": first, "c": corpus_id})
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                await session.execute(insert_index, {"i": second, "c": corpus_id})
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(text("DELETE FROM knowledge_base.search_indexes"))
            await session.execute(text("DELETE FROM knowledge_base.knowledge_corpus"))


async def test_a_chunk_references_a_unit_of_its_own_batch(
    session_factory: SessionFactory,
) -> None:
    """Correction reportée de la revue S3 : la version et la politique
    dénormalisées d'un chunk sont celles de son unité."""
    foreign = await constraints(session_factory, "document_chunks", "f")

    assert any(
        "FOREIGN KEY (unit_id, version_id, chunking_policy_version) REFERENCES "
        "knowledge_base.knowledge_units(id, version_id, chunking_policy_version)" in d
        for d in foreign
    )

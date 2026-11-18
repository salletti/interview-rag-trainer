"""Migration S3 : ``knowledge_units`` et ``document_chunks`` (schéma
``knowledge_base``). Le schéma est figé ici (noms, types, nullabilité,
contraintes) ; Alembic reste l'unique créateur (aucun ``create_all``).

Identité d'un lot : ``(version_id, chunking_policy_version)`` (§3.4) ; les
unités et chunks sont immuables et dérivés : pas de ``updated_at``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]
S3_TABLES = ("knowledge_units", "document_chunks")

# colonne -> (type PostgreSQL, nullable)
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "knowledge_units": {
        "id": ("uuid", False),
        "version_id": ("uuid", False),
        "chunking_policy_version": ("text", False),
        "ordinal": ("integer", False),
        "unit_type": ("text", False),
        "question_text": ("text", True),
        "section_path": ("ARRAY", False),
        "page_start": ("integer", True),
        "page_end": ("integer", True),
        "token_count": ("integer", False),
        "text": ("text", False),
        "created_at": ("timestamp with time zone", False),
    },
    "document_chunks": {
        "id": ("uuid", False),
        "unit_id": ("uuid", False),
        "version_id": ("uuid", False),
        "chunking_policy_version": ("text", False),
        "ordinal_in_unit": ("integer", False),
        "contextual_text": ("text", False),
        "chunk_hash": ("text", False),
        "token_count": ("integer", False),
        "citation": ("jsonb", False),
        "created_at": ("timestamp with time zone", False),
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


@pytest.mark.parametrize("table", S3_TABLES)
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


async def test_downgrade_removes_the_s3_tables_and_keeps_the_s2_ones(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")
    for table in (*S3_TABLES, "document_versions", "work_items"):
        assert await existing(session_factory, table)

    alembic_ok("downgrade", "-1")

    for table in S3_TABLES:
        assert not await existing(session_factory, table)
    assert await existing(session_factory, "document_versions")
    assert await existing(session_factory, "work_items")

    alembic_ok("upgrade", "head")
    for table in S3_TABLES:
        assert await existing(session_factory, table)


async def test_the_models_and_the_migration_do_not_drift() -> None:
    alembic_ok("check")


async def test_primary_keys_are_the_deterministic_identifiers(
    session_factory: SessionFactory,
) -> None:
    for table in S3_TABLES:
        assert await constraints(session_factory, table, "p") == ["PRIMARY KEY (id)"]


async def test_a_batch_has_one_unit_per_ordinal(
    session_factory: SessionFactory,
) -> None:
    unique = await constraints(session_factory, "knowledge_units", "u")

    assert "UNIQUE (version_id, chunking_policy_version, ordinal)" in unique


async def test_a_unit_has_one_chunk_per_rank(session_factory: SessionFactory) -> None:
    unique = await constraints(session_factory, "document_chunks", "u")

    assert "UNIQUE (unit_id, ordinal_in_unit)" in unique


async def test_units_and_chunks_reference_their_version_and_unit(
    session_factory: SessionFactory,
) -> None:
    unit_fks = await constraints(session_factory, "knowledge_units", "f")
    chunk_fks = await constraints(session_factory, "document_chunks", "f")

    assert any(
        "FOREIGN KEY (version_id) REFERENCES knowledge_base.document_versions(id)" in d
        for d in unit_fks
    )
    assert any(
        "FOREIGN KEY (unit_id) REFERENCES knowledge_base.knowledge_units(id)" in d
        for d in chunk_fks
    )
    assert any(
        "FOREIGN KEY (version_id) REFERENCES knowledge_base.document_versions(id)" in d
        for d in chunk_fks
    )


async def test_unit_types_are_constrained(session_factory: SessionFactory) -> None:
    checks = " ".join(await constraints(session_factory, "knowledge_units", "c"))

    for unit_type in ("QA", "SECTION", "DEFINITION"):
        assert unit_type in checks

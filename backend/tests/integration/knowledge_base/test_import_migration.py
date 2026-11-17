"""Migration S2 : ``documents``, ``document_versions``, ``idempotency_keys``.

``downgrade -1`` depuis ``head`` doit retirer exactement les tables de S2 et
laisser ``work_items`` (S0) intacte ; ``upgrade head`` les recrée. Le schéma
est figé ici (noms, types, nullabilité, contraintes).

``test_migration.py`` (S0) ancre son aller-retour sur la révision de S0, pas
sur ``head`` : il reste valable avec la migration S2.
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
S2_TABLES = ("documents", "document_versions", "idempotency_keys")

# colonne -> (type PostgreSQL, nullable)
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "documents": {
        "id": ("uuid", False),
        "title": ("text", False),
        "kind": ("text", False),
        "topics": ("ARRAY", False),
        "language": ("text", False),
        "status": ("text", False),
        "published_version_id": ("uuid", True),
        "visibility_revision": ("integer", False),
        "version": ("integer", False),
        "created_at": ("timestamp with time zone", False),
        "updated_at": ("timestamp with time zone", False),
    },
    "document_versions": {
        "id": ("uuid", False),
        "document_id": ("uuid", False),
        "number": ("integer", False),
        "status": ("text", False),
        "filename": ("text", False),
        "media_type": ("text", False),
        "size_bytes": ("bigint", False),
        "content_hash": ("text", False),
        "imported_at": ("timestamp with time zone", False),
        "failure_code": ("text", True),
        "failure_message": ("text", True),
        "failure_retryable": ("boolean", True),
        "created_at": ("timestamp with time zone", False),
    },
    "idempotency_keys": {
        "key": ("text", False),
        "fingerprint": ("text", False),
        "document_id": ("uuid", False),
        "version_id": ("uuid", False),
        "number": ("integer", False),
        "created": ("boolean", False),
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


async def test_downgrade_removes_the_s2_tables_and_keeps_work_items(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")
    for table in (*S2_TABLES, "work_items"):
        assert await existing(session_factory, table)

    alembic_ok("downgrade", "-1")

    for table in S2_TABLES:
        assert not await existing(session_factory, table)
    assert await existing(session_factory, "work_items")

    alembic_ok("upgrade", "head")
    for table in S2_TABLES:
        assert await existing(session_factory, table)


async def test_the_models_and_the_migration_do_not_drift() -> None:
    alembic_ok("check")


@pytest.mark.parametrize("table", S2_TABLES)
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


async def test_content_hash_is_globally_unique(
    session_factory: SessionFactory,
) -> None:
    unique = await constraints(session_factory, "document_versions", "u")

    assert "UNIQUE (content_hash)" in unique


async def test_version_numbers_are_unique_per_document(
    session_factory: SessionFactory,
) -> None:
    unique = await constraints(session_factory, "document_versions", "u")

    assert "UNIQUE (document_id, number)" in unique


async def test_versions_reference_their_document(
    session_factory: SessionFactory,
) -> None:
    foreign = await constraints(session_factory, "document_versions", "f")

    assert any(
        "FOREIGN KEY (document_id) REFERENCES knowledge_base.documents(id)" in d
        for d in foreign
    )


async def test_statuses_are_constrained(session_factory: SessionFactory) -> None:
    checks = " ".join(
        await constraints(session_factory, "documents", "c")
        + await constraints(session_factory, "document_versions", "c")
    )

    for status in ("ACTIVE", "WITHDRAWN", "RECEIVED", "EXTRACTED", "FAILED"):
        assert status in checks
    for status in ("PUBLISHED", "DISCARDED", "SUPERSEDED"):
        assert status in checks


async def test_at_most_one_published_version_per_document(
    session_factory: SessionFactory,
) -> None:
    # I4 : index unique partiel sur les versions PUBLISHED.
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'knowledge_base' "
                "AND tablename = 'document_versions'"
            )
        )
        definitions = [str(row[0]) for row in rows]

    assert any(
        "UNIQUE" in d and "(document_id)" in d and "PUBLISHED" in d for d in definitions
    )


async def test_idempotency_key_is_the_primary_key(
    session_factory: SessionFactory,
) -> None:
    primary = await constraints(session_factory, "idempotency_keys", "p")

    assert primary == ["PRIMARY KEY (key)"]

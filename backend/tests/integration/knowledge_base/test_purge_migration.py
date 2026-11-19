"""Migration S7 : ``knowledge_base.documents.purged_at`` (``timestamptz``,
nullable, sans défaut), date de purge d'un document retiré (ADR 0012 point 6).

L'aller-retour est ancré sur les révisions (``PURGE_REVISION`` et la
précédente), pas sur ``downgrade -1`` (même convention que S2 à S4). L'absence
de dérive entre ``tables.py`` et les migrations est vérifiée par
``alembic check`` (``test_indexing_migration``).
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

PURGE_REVISION = "9e4b7d2a6c13"
BEFORE_PURGE_REVISION = "c43c7e850745"  # clé étrangère de l'index actif (S4)


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


async def purged_at_column(factory: SessionFactory) -> tuple[str, str] | None:
    """(type, nullable) de la colonne, ``None`` si elle est absente."""
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'knowledge_base' "
                    "AND table_name = 'documents' AND column_name = 'purged_at'"
                )
            )
        ).first()
    return None if row is None else (str(row[0]), str(row[1]))


@pytest.fixture
def restore_head() -> Iterator[None]:
    yield
    alembic_ok("upgrade", "head")


async def test_purged_at_is_a_nullable_timestamptz(
    session_factory: SessionFactory,
) -> None:
    assert await purged_at_column(session_factory) == (
        "timestamp with time zone",
        "YES",
    )


async def test_downgrade_drops_purged_at_and_upgrade_restores_it(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")

    alembic_ok("downgrade", BEFORE_PURGE_REVISION)

    assert BEFORE_PURGE_REVISION in alembic_ok("current")
    assert await purged_at_column(session_factory) is None

    alembic_ok("upgrade", PURGE_REVISION)

    assert PURGE_REVISION in alembic_ok("current")
    assert await purged_at_column(session_factory) == (
        "timestamp with time zone",
        "YES",
    )

"""Migration S8 : ``knowledge_base.work_items.reopen_count`` (``integer NOT NULL
DEFAULT 0``, ``CHECK >= 0``), compteur des réouvertures d'un item ``FAILED``
(rejeu, réconciliation bornée d'une purge).

Même convention que les migrations de S2 à S7 : aller-retour ancré sur les
révisions ; un item existant avant la migration reçoit 0.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]

REOPEN_REVISION = "5d2f8c1a7b30"
BEFORE_REOPEN_REVISION = "9e4b7d2a6c13"  # documents.purged_at (S7)


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


async def reopen_count_column(factory: SessionFactory) -> tuple[str, str, str] | None:
    """(type, nullable, défaut) de la colonne, ``None`` si elle est absente."""
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'knowledge_base' "
                    "AND table_name = 'work_items' AND column_name = 'reopen_count'"
                )
            )
        ).first()
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]))


async def insert_item(factory: SessionFactory, key: str) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.work_items (id, kind, dedup_key, status) "
                "VALUES (:id, 'test_migration', :k, 'FAILED')"
            ),
            {"id": item_id, "k": key},
        )
    return item_id


@pytest.fixture
def restore_head() -> Iterator[None]:
    yield
    alembic_ok("upgrade", "head")


async def test_reopen_count_is_a_non_null_integer_defaulting_to_zero(
    session_factory: SessionFactory,
) -> None:
    assert await reopen_count_column(session_factory) == ("integer", "NO", "0")


async def test_a_negative_reopen_count_is_refused(
    session_factory: SessionFactory,
) -> None:
    item_id = await insert_item(session_factory, f"test:{uuid.uuid4()}")

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE knowledge_base.work_items SET reopen_count = -1 "
                    "WHERE id = :id"
                ),
                {"id": item_id},
            )


async def test_downgrade_drops_reopen_count_and_upgrade_gives_zero_to_old_items(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")

    alembic_ok("downgrade", BEFORE_REOPEN_REVISION)

    assert BEFORE_REOPEN_REVISION in alembic_ok("current")
    assert await reopen_count_column(session_factory) is None
    item_id = await insert_item(session_factory, f"test:{uuid.uuid4()}")

    alembic_ok("upgrade", REOPEN_REVISION)

    assert REOPEN_REVISION in alembic_ok("current")
    async with session_factory() as session:
        count = await session.scalar(
            text("SELECT reopen_count FROM knowledge_base.work_items WHERE id = :id"),
            {"id": item_id},
        )
    assert count == 0

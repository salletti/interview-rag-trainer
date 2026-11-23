"""I1 en base (revue S2 M-4, migration ``756db53032ab``) : un trigger
``BEFORE UPDATE`` sur ``knowledge_base.document_versions`` refuse toute
modification de la source d'une version ; l'état et la cause d'échec restent
modifiables. L'aller-retour est ancré sur la révision précédente
(``22df253e1e05``, S3), pas sur ``downgrade -1``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from runtime.database import SessionFactory
from tests.integration.knowledge_base.import_support import clean_documents
from tests.unit.knowledge_base.domain.builders import new_document, version_id

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]
REVISION = "756db53032ab"
BEFORE_REVISION = "22df253e1e05"  # migration S3
TRIGGER = "trg_document_versions_source_immutable"

# colonne de la source -> expression SQL d'une valeur différente
SOURCE_CHANGES = {
    "number": "number + 10",
    "content_hash": "'0' || content_hash",
    "filename": "'autre.md'",
    "media_type": "'text/plain'",
    "size_bytes": "size_bytes + 1",
    "imported_at": "imported_at + interval '1 second'",
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


@pytest.fixture
async def stored_version(session_factory: SessionFactory) -> AsyncIterator[UUID]:
    await clean_documents(session_factory)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(new_document(1))
        await uow.commit()
    yield version_id(1)
    await clean_documents(session_factory)


async def update_version(factory: SessionFactory, assignment: str, vid: UUID) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                f"UPDATE knowledge_base.document_versions SET {assignment} "
                "WHERE id = :id"
            ),
            {"id": vid},
        )


async def trigger_exists(factory: SessionFactory) -> bool:
    async with factory() as session:
        found = await session.scalar(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = :name"),
            {"name": TRIGGER},
        )
    return bool(found)


@pytest.mark.parametrize(("column", "change"), SOURCE_CHANGES.items())
async def test_changing_the_source_of_a_version_is_refused(
    session_factory: SessionFactory, stored_version: UUID, column: str, change: str
) -> None:
    with pytest.raises(DBAPIError, match="I1"):
        await update_version(session_factory, f"{column} = {change}", stored_version)


async def test_the_status_and_failure_remain_mutable(
    session_factory: SessionFactory, stored_version: UUID
) -> None:
    await update_version(
        session_factory,
        "status = 'FAILED', failure_code = 'X', failure_message = 'm', "
        "failure_retryable = false",
        stored_version,
    )

    async with session_factory() as session:
        status = await session.scalar(
            text("SELECT status FROM knowledge_base.document_versions WHERE id = :id"),
            {"id": stored_version},
        )
    assert status == "FAILED"


async def test_rewriting_the_same_source_values_is_allowed(
    session_factory: SessionFactory, stored_version: UUID
) -> None:
    await update_version(session_factory, "filename = filename", stored_version)


async def test_the_downgrade_removes_the_trigger_and_the_upgrade_restores_it(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")
    assert await trigger_exists(session_factory)

    alembic_ok("downgrade", BEFORE_REVISION)
    assert BEFORE_REVISION in alembic_ok("current")
    assert not await trigger_exists(session_factory)

    alembic_ok("upgrade", REVISION)
    assert REVISION in alembic_ok("current")
    assert await trigger_exists(session_factory)

    alembic_ok("upgrade", "head")


async def test_the_trigger_does_not_create_a_model_drift() -> None:
    alembic_ok("check")

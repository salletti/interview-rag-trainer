"""``SqlAlchemyDocumentPurger`` (S7) : la date de purge est celle de la
PREMIÈRE purge ; une purge rejouée (réconciliation S8, zombie) ne la déplace
pas. Les suppressions de chunks et d'unités sont couvertes de bout en bout
par ``test_s7_withdrawal_purge``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from knowledge_base.adapters.gateway.persistence.sqlalchemy_document_purger import (
    SqlAlchemyDocumentPurger,
)
from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

FIRST = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
REPLAY = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


@pytest.fixture
async def document_id(session_factory: SessionFactory) -> AsyncIterator[UUID]:
    """Document retiré sans version : la purge n'a que ``purged_at`` à écrire."""
    created = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.documents (id, title, kind, topics, "
                "language, status, visibility_revision, version) VALUES "
                "(:i, 'Retiré', 'COURSE', '{}', 'fr', 'WITHDRAWN', 1, 1)"
            ),
            {"i": created},
        )
    yield created
    async with session_factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM knowledge_base.documents WHERE id = :i"), {"i": created}
        )


async def purge(factory: SessionFactory, document_id: UUID, at: datetime) -> None:
    async with factory() as session, session.begin():
        await SqlAlchemyDocumentPurger(session).delete_derived_data(
            document_id, purged_at=at
        )


async def test_a_replayed_purge_keeps_the_first_purge_date(
    session_factory: SessionFactory, document_id: UUID
) -> None:
    await purge(session_factory, document_id, FIRST)

    await purge(session_factory, document_id, REPLAY)

    async with session_factory() as session:
        purged_at = await session.scalar(
            text("SELECT purged_at FROM knowledge_base.documents WHERE id = :i"),
            {"i": document_id},
        )
    assert purged_at == FIRST

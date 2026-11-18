"""Détenteurs d'un original (S7 ; ADR 0012 point 6) : documents non purgés
dont une version, dans quelque état que ce soit, porte l'empreinte. Une
requête dans une session courte, par l'index unique sur
``document_versions.content_hash`` ; lue hors de toute transaction métier,
juste avant la suppression du fichier."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.frameworks.sqlalchemy.tables import document_versions, documents

_v = document_versions.c
_d = documents.c


class SqlAlchemyBlobReferences:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def holders(self, content_hash: ContentHash) -> frozenset[UUID]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(_v.document_id)
                .join(documents, _d.id == _v.document_id)
                .where(_v.content_hash == content_hash.value)
                .where(_d.purged_at.is_(None))
                .distinct()
            )
            return frozenset(rows.scalars())

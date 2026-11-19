"""Purge des données dérivées d'un document retiré (S7 ; knowledge-base.md §6
ligne ``purge_document``, §10) sur la session de la transaction de
finalisation du work item : ne valide ni n'annule jamais (S0 §5).

Trois instructions ensemblistes, sans charger aucune ligne : les chunks
d'abord (ils référencent les unités), puis les unités, de toutes les versions
du document (jointure sur ``document_versions``, index
``(document_id, number)``) ; enfin ``purged_at``, posée à la première purge
seulement (``COALESCE``) : rejouée, la purge ne change rien.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.frameworks.sqlalchemy.tables import (
    document_chunks,
    document_versions,
    documents,
    knowledge_units,
)

_v = document_versions.c


class SqlAlchemyDocumentPurger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delete_derived_data(
        self, document_id: UUID, *, purged_at: datetime
    ) -> None:
        for derived in (document_chunks, knowledge_units):
            await self._session.execute(
                delete(derived)
                .where(derived.c.version_id == _v.id)
                .where(_v.document_id == document_id)
            )
        await self._session.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(purged_at=func.coalesce(documents.c.purged_at, purged_at))
        )


class SqlAlchemyPurgeTransaction:
    """``PurgeTransaction`` : ce que ``PurgeDocument.apply`` reçoit."""

    def __init__(self, session: AsyncSession) -> None:
        self._purger = SqlAlchemyDocumentPurger(session)

    @property
    def purger(self) -> SqlAlchemyDocumentPurger:
        return self._purger

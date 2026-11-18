"""Lecture de l'index actif pour la recherche (knowledge-base.md §12 étape 1) :
une requête, sans verrou, dans une session ouverte et fermée par l'appel."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_corpus_repository import (  # noqa: E501
    index_from_row,
)
from knowledge_base.domain.entities.knowledge_corpus import CORPUS_ID, SearchIndex
from knowledge_base.frameworks.sqlalchemy.tables import knowledge_corpus, search_indexes

_c = knowledge_corpus.c
_i = search_indexes.c


class SqlAlchemyActiveIndexReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_active_index(self) -> SearchIndex | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(search_indexes)
                    .join(knowledge_corpus, _c.active_index_id == _i.index_id)
                    .where(_c.corpus_id == CORPUS_ID)
                )
            ).one_or_none()
        return None if row is None else index_from_row(row)

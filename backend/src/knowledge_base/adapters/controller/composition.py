"""Composition des handlers de Knowledge Base qui ne sont ni des routes ni des
work items : le provisionnement de l'index initial (S4), appelé au démarrage
de la stack (``python -m runtime.provision``)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.use_cases.import_steps import utc_now
from knowledge_base.use_cases.provision_search_index.provision_search_index_handler import (  # noqa: E501
    ProvisionSearchIndexHandler,
)


def build_provision_search_index_handler(
    session_factory: async_sessionmaker[AsyncSession],
    vector_index: VectorIndex,
    *,
    id_factory: Callable[[], UUID] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProvisionSearchIndexHandler:
    return ProvisionSearchIndexHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
        vector_index=vector_index,
        id_factory=id_factory or uuid4,
        clock=clock or utc_now,
    )

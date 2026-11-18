"""Composition des handlers de Knowledge Base qui ne sont ni des routes ni des
work items : le provisionnement de l'index initial (S4), appelé au démarrage
de la stack (``python -m runtime.provision``), la recherche (S5), dont les
dépendances sont explicites ici (embedder et compteur de tokens injectables
par les tests) ; la fabrique publique, ``build_knowledge_search(resources)``,
la compose sur les ressources du contexte ; le nettoyage des blobs orphelins
(S7), dont le déclenchement périodique arrive avec la réconciliation (S8)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.contracts.knowledge_search_adapter import (
    KnowledgeSearchAdapter,
)
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_active_index_reader import (
    SqlAlchemyActiveIndexReader,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_blob_references import (
    SqlAlchemyBlobReferences,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_published_unit_reader import (  # noqa: E501
    SqlAlchemyPublishedUnitReader,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_handler import (
    CleanBlobOrphansHandler,
)
from knowledge_base.use_cases.import_steps import utc_now
from knowledge_base.use_cases.provision_search_index.provision_search_index_handler import (  # noqa: E501
    ProvisionSearchIndexHandler,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
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


def compose_knowledge_search(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_client: AsyncQdrantClient,
    settings: KnowledgeBaseSettings,
    *,
    embedder: Embedder,
    token_counter: TokenCounter,
) -> KnowledgeSearchAdapter:
    """Implémentation du contrat ``KnowledgeSearch`` (knowledge-base.md §14) ;
    ne se connecte à rien. ``token_counter`` compte la question dans le budget
    d'un passage : en production, celui de ``embedder``."""
    return KnowledgeSearchAdapter(
        SearchKnowledgeHandler(
            embedder=embedder,
            chunk_searcher=QdrantVectorIndex(qdrant_client),
            published_units=SqlAlchemyPublishedUnitReader(session_factory),
            active_indexes=SqlAlchemyActiveIndexReader(session_factory),
            token_counter=token_counter,
            max_passage_tokens=settings.max_passage_tokens,
            oversampling_factor=settings.search_oversampling_factor,
            complement_passes=settings.search_complement_passes,
        )
    )


def build_clean_blob_orphans_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    clock: Callable[[], datetime] | None = None,
) -> CleanBlobOrphansHandler:
    """Nettoyage des orphelins du volume ``settings.blob_root`` d'après les
    versions de PostgreSQL ; ne se connecte à rien. Le délai de grâce est
    celui de la commande (``settings.blob_orphan_grace`` en production)."""
    blobs = FileSystemBlobStore(settings.blob_root)
    return CleanBlobOrphansHandler(
        inventory=blobs,
        references=SqlAlchemyBlobReferences(session_factory),
        eraser=blobs,
        clock=clock or utc_now,
    )

"""Composition des handlers de Knowledge Base qui ne sont ni des routes ni des
work items : le provisionnement de l'index initial (S4), appelé au démarrage
de la stack (``python -m runtime.provision``), la recherche (S5), dont les
dépendances sont explicites ici (embedder et compteur de tokens injectables
par les tests) ; la fabrique publique, ``build_knowledge_search(resources)``,
la compose sur les ressources du contexte ; le nettoyage des blobs orphelins
(S7) ; la réconciliation, la consultation et le rejeu des échecs (S8), que
le work item ``reconcile_knowledge_base`` et les routes ``/failures``
appellent."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final
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
from knowledge_base.adapters.gateway.persistence.sqlalchemy_reconciliation_store import (  # noqa: E501
    SqlAlchemyReconciliationStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.reconciliation_store import CompletedIndexation
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_handler import (
    CleanBlobOrphansHandler,
)
from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_handler import (
    CleanStaleUploadsHandler,
)
from knowledge_base.use_cases.import_steps import utc_now
from knowledge_base.use_cases.list_failures.list_failures_handler import (
    ListFailuresHandler,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_handler import (  # noqa: E501
    ProvisionSearchIndexHandler,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.failed_work_reconciliation import (  # noqa: E501
    FailedWorkReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.indexation_reconciliation import (  # noqa: E501
    IndexationReconciliation,
    Sampler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.point_reconciliation import (
    PointReconciliation,
    ReconcilableIndex,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_handler import (  # noqa: E501
    ReconcileKnowledgeBaseHandler,
    ReconciliationPolicy,
)
from knowledge_base.use_cases.reconcile_knowledge_base.storage_housekeeping import (
    StorageHousekeeping,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_handler import (
    RetryIngestionHandler,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)

# Points lus par page lors du parcours d'un index (Qdrant ``scroll``).
SCAN_BATCH_SIZE: Final = 256


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


def build_reconcile_knowledge_base_handler(
    session_factory: async_sessionmaker[AsyncSession],
    vector_index: ReconcilableIndex,
    settings: KnowledgeBaseSettings,
    *,
    clock: Callable[[], datetime] | None = None,
    sampler: Sampler | None = None,
) -> ReconcileKnowledgeBaseHandler:
    """Un cycle sur PostgreSQL, ``vector_index`` (``QdrantVectorIndex`` en
    production : ``VectorIndex`` et ``PointInventory``) et le volume
    ``settings.blob_root`` ; ne se connecte à rien. ``sampler`` choisit les
    manifests revérifiés (défaut : tirage aléatoire)."""
    clock = clock or utc_now

    def unit_of_work() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    store = SqlAlchemyReconciliationStore(session_factory)
    blobs = FileSystemBlobStore(settings.blob_root)
    references = SqlAlchemyBlobReferences(session_factory)
    return ReconcileKnowledgeBaseHandler(
        unit_of_work=unit_of_work,
        points=PointReconciliation(
            unit_of_work=unit_of_work,
            store=store,
            vector_index=vector_index,
            scan_batch_size=SCAN_BATCH_SIZE,
        ),
        indexations=IndexationReconciliation(
            unit_of_work=unit_of_work,
            store=store,
            vector_index=vector_index,
            publisher=PublishDocumentVersionHandler(clock=clock),
            sampler=sampler or random_sample,
        ),
        failed_work=FailedWorkReconciliation(
            unit_of_work=unit_of_work,
            store=store,
            max_purge_reopenings=settings.reconcile_max_purge_reopenings,
        ),
        storage=StorageHousekeeping(
            quarantine=blobs,
            blob_references=references,
            blob_orphans=CleanBlobOrphansHandler(
                inventory=blobs, references=references, eraser=blobs, clock=clock
            ),
            stale_uploads=CleanStaleUploadsHandler(staging=blobs, clock=clock),
            store=store,
            clock=clock,
        ),
        policy=ReconciliationPolicy(
            manifest_sample_size=settings.reconcile_manifest_sample_size,
            blob_grace=settings.blob_orphan_grace,
            idempotency_key_ttl=settings.idempotency_key_ttl,
        ),
        clock=clock,
    )


def random_sample(
    candidates: Sequence[CompletedIndexation], k: int
) -> Sequence[CompletedIndexation]:
    """Échantillon uniforme sans remise : sur des cycles successifs, chaque
    indexation finit revérifiée sans parcourir tout le corpus à chaque fois."""
    return random.sample(list(candidates), min(k, len(candidates)))


def build_retry_ingestion_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> RetryIngestionHandler:
    return RetryIngestionHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory)
    )


def build_list_failures_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> ListFailuresHandler:
    return ListFailuresHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory)
    )

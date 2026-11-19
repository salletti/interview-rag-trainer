"""Points d'entrée publics de Knowledge Base, réexportés par le package
racine (docs/conception/005-project-structure.md, surface publique)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from celery import Celery
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.composition import (
    build_activate_search_index_handler,
    build_list_failures_handler,
    build_provision_search_index_handler,
    build_retry_ingestion_handler,
    build_start_index_migration_handler,
    compose_knowledge_search,
)
from knowledge_base.adapters.controller.rest.documents_router import (
    add_document_routes,
)
from knowledge_base.adapters.controller.rest.failures_router import (
    add_failure_routes,
)
from knowledge_base.adapters.controller.rest.resources import KnowledgeBaseResources
from knowledge_base.adapters.controller.rest.search_router import add_search_routes
from knowledge_base.adapters.controller.tasks.dispatch import (
    dispatch_pending,
    recover_expired_leases,
)
from knowledge_base.adapters.controller.tasks.drop_index_collection_handler import (
    build_drop_index_collection_handler,
)
from knowledge_base.adapters.controller.tasks.execution import (
    WorkOutcome,
    execute_work_item,
)
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    build_extract_version_handler,
)
from knowledge_base.adapters.controller.tasks.index_version_handler import (
    build_index_version_handler,
)
from knowledge_base.adapters.controller.tasks.purge_document_handler import (
    build_purge_document_handler,
)
from knowledge_base.adapters.controller.tasks.rechunk_version_handler import (
    build_rechunk_version_handler,
)
from knowledge_base.adapters.controller.tasks.reconcile_handler import (
    build_reconcile_work_handler,
    enqueue_reconciliation,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.controller.tasks.resources import (
    KnowledgeBaseWorkerResources,
)
from knowledge_base.adapters.controller.tasks.sync_visibility_handler import (
    build_sync_visibility_handler,
)
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.embedding.embedder_registry import (
    EmbedderRegistry,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from knowledge_base.frameworks.configuration.embedding_targets import (
    CATALOGUE,
    catalogued_profiles,
    migration_target,
)
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.activate_search_index.activate_search_index_command import (  # noqa: E501
    ActivateSearchIndex,
)
from knowledge_base.use_cases.import_document.import_document_handler import (
    ImportDocumentHandler,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from knowledge_base.use_cases.start_index_migration.start_index_migration_command import (  # noqa: E501
    StartIndexMigration,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (
    WithdrawDocumentHandler,
)

__all__ = [
    "KnowledgeBaseResources",
    "KnowledgeBaseSettings",
    "KnowledgeBaseWorkerResources",
    "WorkHandler",
    "activate_search_index",
    "build_knowledge_search",
    "build_work_handlers",
    "migration_target_names",
    "provision_search_index",
    "register",
    "register_worker",
    "start_index_migration",
]

EXECUTE_TASK: Final = "knowledge_base.work.execute"
DISPATCH_TASK: Final = "knowledge_base.work.dispatch"
RECOVER_TASK: Final = "knowledge_base.work.recover"
RECONCILE_TASK: Final = "knowledge_base.work.reconcile"

# File consommée par le worker par défaut : dispatch, recover et la
# planification de la réconciliation ne doivent jamais attendre derrière une
# extraction longue (ADR 0004, S0 §8.5).
CONTROL_QUEUE: Final = "interview_rag_trainer.default"

ResourcesProvider = Callable[[], KnowledgeBaseWorkerResources]


def register(app: FastAPI, resources: Callable[[], KnowledgeBaseResources]) -> None:
    """Monte les routes de Knowledge Base, à la construction de l'application.
    ``resources`` n'est appelé qu'au traitement d'une requête : la fabrique de
    sessions n'existe qu'après le démarrage (``lifespan``).

    La recherche est reconstruite à chaque requête sur les modèles des
    ressources : un seul modèle par profil et par jeu de ressources, partagé
    avec ``build_knowledge_search``."""
    add_document_routes(
        app.router,
        import_document_handler=lambda: build_import_document_handler(resources()),
        import_version_handler=lambda: build_import_version_handler(resources()),
        withdraw_document_handler=lambda: build_withdraw_document_handler(resources()),
    )
    add_search_routes(
        app.router, knowledge_search=lambda: build_knowledge_search(resources())
    )
    add_failure_routes(
        app.router,
        list_failures_handler=lambda: build_list_failures_handler(
            resources().session_factory
        ),
        retry_ingestion_handler=lambda: build_retry_ingestion_handler(
            resources().session_factory
        ),
    )


def build_knowledge_search(resources: KnowledgeBaseResources) -> KnowledgeSearch:
    """Implémentation du contrat public ``KnowledgeSearch`` (knowledge-base.md
    §14), sur l'index actif du corpus. Ne se connecte à rien. Le modèle
    d'embeddings est celui du profil de l'index actif, pris dans les
    ressources (chargé au premier usage, cache
    ``resources.settings.models_cache_dir``) : la route de recherche et tout
    consommateur composé sur les mêmes ressources le partagent."""
    return compose_knowledge_search(
        resources.session_factory,
        resources.qdrant_client,
        resources.settings,
        embedders=resources.embedders,
    )


def build_import_document_handler(
    resources: KnowledgeBaseResources,
) -> ImportDocumentHandler:
    return ImportDocumentHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(resources.session_factory),
        blob_store=FileSystemBlobStore(resources.settings.blob_root),
        max_upload_bytes=resources.settings.max_upload_bytes,
    )


def build_import_version_handler(
    resources: KnowledgeBaseResources,
) -> ImportDocumentVersionHandler:
    return ImportDocumentVersionHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(resources.session_factory),
        blob_store=FileSystemBlobStore(resources.settings.blob_root),
        max_upload_bytes=resources.settings.max_upload_bytes,
    )


def build_withdraw_document_handler(
    resources: KnowledgeBaseResources,
) -> WithdrawDocumentHandler:
    return WithdrawDocumentHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(resources.session_factory)
    )


def build_work_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_client: AsyncQdrantClient,
    settings: KnowledgeBaseSettings | None = None,
) -> tuple[WorkHandler, ...]:
    """Handlers de production des work items, tous sur la file
    ``knowledge_base.ingestion`` : ``extract_version`` (S3), ``index_version``
    et ``sync_visibility`` (S4), ``purge_document`` (S7),
    ``reconcile_knowledge_base`` (S8), ``rechunk_version`` et
    ``drop_index_collection`` (S9). Appelé dans chaque processus enfant, sur
    sa fabrique de sessions et son client Qdrant ; ne se connecte à rien.

    Un modèle d'embeddings par profil du catalogue, chacun chargé à sa
    première tâche (jamais dans le processus maître) : l'extraction compte les
    tokens avec le tokenizer du profil initial, l'indexation encode avec le
    modèle du profil de l'index visé (deux modèles pendant une migration)."""
    settings = settings or KnowledgeBaseSettings()
    embedders = EmbedderRegistry.lazy_sentence_transformers(
        catalogued_profiles(), cache_dir=settings.models_cache_dir
    )
    vector_index = QdrantVectorIndex(qdrant_client)
    return (
        build_extract_version_handler(
            session_factory,
            settings,
            token_counter=embedders.for_profile(MULTILINGUAL_E5_BASE).token_counter,
            policy=DEFAULT_CHUNKING_POLICY,
        ),
        build_index_version_handler(
            session_factory,
            settings,
            embedders=embedders,
            vector_index=vector_index,
            batch_size=settings.embedding_batch_size,
        ),
        build_rechunk_version_handler(session_factory, settings, embedders=embedders),
        build_drop_index_collection_handler(session_factory, collections=vector_index),
        build_sync_visibility_handler(session_factory, vector_index=vector_index),
        build_purge_document_handler(
            session_factory, settings, vector_index=vector_index
        ),
        build_reconcile_work_handler(
            session_factory, settings, vector_index=vector_index
        ),
    )


async def provision_search_index(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_client: AsyncQdrantClient,
) -> UUID:
    """Crée et active l'index initial (profil ``multilingual-e5-base``,
    politique de production) s'il n'existe pas ; idempotent. Rend l'index
    actif. Appelé au démarrage de la stack, avant l'API et les workers."""
    handler = build_provision_search_index_handler(
        session_factory, QdrantVectorIndex(qdrant_client)
    )
    return await handler.handle(
        ProvisionSearchIndex(
            profile=MULTILINGUAL_E5_BASE, chunking_policy=DEFAULT_CHUNKING_POLICY
        )
    )


def migration_target_names() -> tuple[str, ...]:
    """Noms des cibles de migration du catalogue (aide de
    ``python -m runtime.index_migration``)."""
    return tuple(sorted(CATALOGUE))


async def start_index_migration(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_client: AsyncQdrantClient,
    target_name: str,
) -> UUID:
    """Démarre une migration blue/green vers la cible ``target_name`` du
    catalogue : index ``BUILDING``, sa collection et le backfill du corpus.
    Rejouable : la même cible rend le même index. Rend l'index ``BUILDING``.
    Lève ``LookupError`` pour une cible inconnue."""
    target = migration_target(target_name)
    handler = build_start_index_migration_handler(
        session_factory, QdrantVectorIndex(qdrant_client)
    )
    return await handler.handle(
        StartIndexMigration(
            profile=target.profile, chunking_policy=target.chunking_policy
        )
    )


async def activate_search_index(
    session_factory: async_sessionmaker[AsyncSession],
    index_id: UUID,
    settings: KnowledgeBaseSettings | None = None,
) -> None:
    """Bascule la recherche sur l'index ``BUILDING`` ``index_id`` si sa
    couverture est complète et qu'aucune synchronisation n'est en attente ;
    l'ancien index est retiré, sa collection supprimée après
    ``KB_INDEX_DROP_DELAY_SECONDS``."""
    settings = settings or KnowledgeBaseSettings()
    handler = build_activate_search_index_handler(
        session_factory, drop_delay=settings.index_drop_delay
    )
    await handler.handle(ActivateSearchIndex(index_id=index_id))


def register_worker(
    celery_app: Celery,
    resources: ResourcesProvider,
    settings: KnowledgeBaseSettings | None = None,
) -> None:
    """Déclare les tâches, leurs routes et le ``beat_schedule`` de Knowledge
    Base. Appelé dans le processus maître : ``resources`` n'est appelé qu'à
    l'exécution d'une tâche, dans le processus enfant (S0 §9)."""
    settings = settings or KnowledgeBaseSettings()

    # ``shared=False`` : sans cela, Celery ajoute la tâche à toute application
    # créée ensuite, avec les réglages et les ressources de la première.
    @celery_app.task(
        name=EXECUTE_TASK,
        shared=False,
        acks_late=False,
        time_limit=settings.work_time_limit_seconds,
        ignore_result=True,
    )
    def execute(work_item_id: str) -> WorkOutcome:
        current = resources()
        return current.loop.run_until_complete(
            execute_work_item(UUID(work_item_id), current, settings)
        )

    @celery_app.task(name=DISPATCH_TASK, shared=False, ignore_result=True)
    def dispatch() -> int:
        current = resources()
        notifications: list[tuple[UUID, str]] = []
        count = current.loop.run_until_complete(
            dispatch_pending(
                current,
                settings,
                publish=lambda item_id, queue: notifications.append((item_id, queue)),
            )
        )
        # Publication Kombu (synchrone) hors de la boucle asyncio, après le
        # commit fait par dispatch_pending.
        for item_id, queue in notifications:
            execute.apply_async(args=[str(item_id)], queue=queue)
        return count

    @celery_app.task(name=RECOVER_TASK, shared=False, ignore_result=True)
    def recover() -> int:
        current = resources()
        return current.loop.run_until_complete(
            recover_expired_leases(current, settings)
        )

    @celery_app.task(name=RECONCILE_TASK, shared=False, ignore_result=True)
    def reconcile() -> str:
        """Enregistre le cycle de réconciliation du créneau courant ; la work
        queue l'exécute sur la file d'ingestion (S8)."""
        current = resources()
        item_id = current.loop.run_until_complete(
            enqueue_reconciliation(current.session_factory, settings)
        )
        return str(item_id)

    celery_app.conf.task_routes = (
        {
            DISPATCH_TASK: {"queue": CONTROL_QUEUE},
            RECOVER_TASK: {"queue": CONTROL_QUEUE},
            RECONCILE_TASK: {"queue": CONTROL_QUEUE},
        },
        *_as_routers(celery_app.conf.task_routes),
    )
    celery_app.conf.beat_schedule = {
        **(celery_app.conf.beat_schedule or {}),
        DISPATCH_TASK: {
            "task": DISPATCH_TASK,
            "schedule": timedelta(seconds=settings.work_dispatch_interval_seconds),
        },
        RECOVER_TASK: {
            "task": RECOVER_TASK,
            "schedule": timedelta(seconds=settings.work_recover_interval_seconds),
        },
        RECONCILE_TASK: {
            "task": RECONCILE_TASK,
            "schedule": settings.reconcile_interval,
        },
    }


def _as_routers(routes: Any) -> tuple[Any, ...]:
    """``task_routes`` accepte un routeur seul ou une séquence de routeurs."""
    if routes is None:
        return ()
    if isinstance(routes, list | tuple):
        return tuple(routes)
    return (routes,)

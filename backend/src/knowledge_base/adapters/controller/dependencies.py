"""Points d'entrée publics de Knowledge Base, réexportés par le package
racine (docs/conception/005-project-structure.md, surface publique)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from celery import Celery
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.rest.documents_router import (
    add_document_routes,
)
from knowledge_base.adapters.controller.rest.resources import KnowledgeBaseResources
from knowledge_base.adapters.controller.tasks.dispatch import (
    dispatch_pending,
    recover_expired_leases,
)
from knowledge_base.adapters.controller.tasks.execution import (
    WorkOutcome,
    execute_work_item,
)
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    build_extract_version_handler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.controller.tasks.resources import (
    KnowledgeBaseWorkerResources,
)
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.import_document.import_document_handler import (
    ImportDocumentHandler,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)

__all__ = [
    "KnowledgeBaseResources",
    "KnowledgeBaseSettings",
    "KnowledgeBaseWorkerResources",
    "WorkHandler",
    "build_work_handlers",
    "register",
    "register_worker",
]

EXECUTE_TASK: Final = "knowledge_base.work.execute"
DISPATCH_TASK: Final = "knowledge_base.work.dispatch"
RECOVER_TASK: Final = "knowledge_base.work.recover"

# File consommée par le worker par défaut : dispatch et recover ne doivent
# jamais attendre derrière une extraction longue (ADR 0004, S0 §8.5).
CONTROL_QUEUE: Final = "interview_rag_trainer.default"

ResourcesProvider = Callable[[], KnowledgeBaseWorkerResources]


def register(app: FastAPI, resources: Callable[[], KnowledgeBaseResources]) -> None:
    """Monte les routes de Knowledge Base, à la construction de l'application.
    ``resources`` n'est appelé qu'au traitement d'une requête : la fabrique de
    sessions n'existe qu'après le démarrage (``lifespan``)."""
    add_document_routes(
        app.router,
        import_document_handler=lambda: build_import_document_handler(resources()),
        import_version_handler=lambda: build_import_version_handler(resources()),
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


def build_work_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings | None = None,
) -> tuple[WorkHandler, ...]:
    """Handlers de production des work items (S3 : ``extract_version`` sur la
    file ``knowledge_base.ingestion``). Appelé dans chaque processus enfant, sur
    sa fabrique de sessions ; ne se connecte à rien."""
    settings = settings or KnowledgeBaseSettings()
    return (build_extract_version_handler(session_factory, settings),)


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

    celery_app.conf.task_routes = (
        {
            DISPATCH_TASK: {"queue": CONTROL_QUEUE},
            RECOVER_TASK: {"queue": CONTROL_QUEUE},
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
    }


def _as_routers(routes: Any) -> tuple[Any, ...]:
    """``task_routes`` accepte un routeur seul ou une séquence de routeurs."""
    if routes is None:
        return ()
    if isinstance(routes, list | tuple):
        return tuple(routes)
    return (routes,)

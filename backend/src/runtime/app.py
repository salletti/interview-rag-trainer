"""Fabrique de l'application FastAPI (composition root).

Charge la configuration, ouvre et ferme les ressources techniques dans le
``lifespan`` et monte les routeurs techniques. Les Bounded Contexts sont
enregistrés ici, par leur surface publique uniquement.

Lancement : ``uvicorn runtime.app:create_app --factory``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import knowledge_base
from runtime import health, system
from runtime.database import create_engine, create_session_factory
from runtime.logging_config import configure_logging
from runtime.settings import Settings, get_settings
from runtime.vector_store import create_qdrant_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)

    # Aucune de ces fabriques n'ouvre de connexion : une dépendance absente au
    # démarrage rend l'API « not_ready », jamais « down ».
    app.state.db_engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.qdrant_client = create_qdrant_client(settings)
    app.state.knowledge_base = knowledge_base.KnowledgeBaseResources(
        session_factory=app.state.session_factory,
        settings=app.state.knowledge_base_settings,
        qdrant_client=app.state.qdrant_client,
    )
    logger.info("startup environment=%s", settings.environment)
    try:
        yield
    finally:
        await app.state.qdrant_client.close()
        await app.state.db_engine.dispose()
        logger.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Fabrique d'application. ``settings`` est injectable pour les tests."""
    settings = settings or get_settings()
    docs_enabled = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    # Relus à chaque construction, sans cache : KB_* peut changer d'une
    # application à l'autre dans un même processus (tests d'intégration).
    app.state.knowledge_base_settings = knowledge_base.KnowledgeBaseSettings()
    app.include_router(health.router)
    app.include_router(system.router)

    def knowledge_base_resources() -> knowledge_base.KnowledgeBaseResources:
        resources: knowledge_base.KnowledgeBaseResources = app.state.knowledge_base
        return resources

    # Réglages KB_* : une seule source, les ressources construites dans le
    # ``lifespan`` à partir de ``app.state.knowledge_base_settings``.
    knowledge_base.register(app, knowledge_base_resources)
    return app

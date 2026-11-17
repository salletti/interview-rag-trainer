"""Fabrique des applications Celery et composition des workers de Knowledge
Base, sans effet de bord à l'import.

Ressources asyncio (docs/conception/bounded-contexts/knowledge-base-s0.md §9) :
les tâches sont déclarées à la construction de l'application, dans le
processus maître, sans aucune connexion. Chaque processus enfant prefork crée
au signal ``worker_process_init`` **une** boucle asyncio persistante, puis le
moteur SQLAlchemy sur cette boucle ; il les ferme à ``worker_process_shutdown``.

Ce module ne construit aucune application : ``runtime.worker`` construit
celle de production. Un processus ne doit en construire qu'une qui enregistre
les tâches de Knowledge Base : dans un enfant démarré en *spawn* (défaut de
billiard sous macOS), Celery partage chaque tâche entre toutes les
applications du processus et la première enregistrée sous un nom l'emporte.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown
from sqlalchemy.ext.asyncio import AsyncEngine

import knowledge_base
from runtime.database import create_engine, create_session_factory
from runtime.settings import Settings, get_settings

# Un enfant n'exécute qu'une tâche à la fois : deux connexions suffisent.
WORKER_CHILD_POOL_SIZE = 2


class _ChildProcessResources:
    """Ressources Knowledge Base d'un processus enfant, créées après le
    démarrage de l'enfant (fork sous Linux, spawn sous macOS).

    Les signaux Celery sont globaux au processus et l'enfant d'un pool spawn
    ne désigne pas l'application courante : chaque application construite par
    ``create_celery_app`` crée donc ses ressources. En exploitation, un
    processus n'en construit qu'une ; les connexions ne sont ouvertes qu'à la
    première requête.
    """

    def __init__(
        self,
        settings: Settings,
        handlers: tuple[knowledge_base.WorkHandler, ...],
    ) -> None:
        self._settings = settings
        self._handlers = handlers
        self._engine: AsyncEngine | None = None
        self._resources: knowledge_base.KnowledgeBaseWorkerResources | None = None

    def get(self) -> knowledge_base.KnowledgeBaseWorkerResources:
        if self._resources is None:
            raise RuntimeError(
                "ressources Knowledge Base absentes dans ce processus : elles "
                "sont créées au signal worker_process_init, émis par les pools "
                "prefork (dans chaque enfant) et solo ; un appel hors worker "
                "(API, beat) n'en dispose pas"
            )
        return self._resources

    def start(self, **_: Any) -> None:
        # Le pool solo émet aussi ce signal, à chaque démarrage de worker dans
        # le même processus (tests) : on garde les ressources déjà créées.
        if self._resources is not None:
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._engine = create_engine(self._settings, pool_size=WORKER_CHILD_POOL_SIZE)
        self._resources = knowledge_base.KnowledgeBaseWorkerResources(
            session_factory=create_session_factory(self._engine),
            loop=loop,
            handlers=self._handlers,
        )

    def stop(self, **_: Any) -> None:
        if self._resources is None or self._engine is None:
            return
        loop = self._resources.loop
        try:
            loop.run_until_complete(self._engine.dispose())
        finally:
            loop.close()
            self._resources = None
            self._engine = None


def create_celery_app(
    settings: Settings | None = None,
    *,
    work_handlers: Sequence[knowledge_base.WorkHandler] = (),
) -> Celery:
    """``work_handlers`` : registre de Knowledge Base, vide en production en
    S0 ; les tests y injectent leurs handlers."""
    settings = settings or get_settings()
    app = Celery(
        "interview_rag_trainer",
        broker=settings.broker_url,
        backend=settings.celery_result_backend,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=settings.external_timeout_seconds,
        result_expires=3600,
        task_default_queue="interview_rag_trainer.default",
        # Réglage du processus worker (S0 §8.1) : un seul message préchargé
        # par enfant ; worker_ingestion le répète sur sa ligne de commande.
        worker_prefetch_multiplier=1,
    )

    child = _ChildProcessResources(settings, tuple(work_handlers))
    knowledge_base.register_worker(app, child.get)
    worker_process_init.connect(child.start, weak=False)
    worker_process_shutdown.connect(child.stop, weak=False)
    return app

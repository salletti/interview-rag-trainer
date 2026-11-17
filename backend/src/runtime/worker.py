"""Application Celery et tâche technique de vérification.

RabbitMQ est le broker ; le result backend ``rpc://`` réutilise RabbitMQ, ce
qui évite d'installer Redis pour un simple aller-retour technique.

Les tâches Celery définies ici sont de l'outillage technique. Elles ne sont
**pas** les Integration Events entre Bounded Contexts
(docs/conception/004-integration-contracts.md) : ceux-ci seront des contrats
JSON versionnés publiés via une outbox dans une phase ultérieure.

Lancement : ``celery -A runtime.worker:celery_app worker``.
"""

from __future__ import annotations

from celery import Celery

from runtime.settings import Settings, get_settings


def create_celery_app(settings: Settings | None = None) -> Celery:
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
    )
    return app


celery_app = create_celery_app()


@celery_app.task(name="runtime.ping")
def ping() -> str:
    """Tâche technique : prouve que broker, worker et result backend
    fonctionnent de bout en bout."""
    return "pong"

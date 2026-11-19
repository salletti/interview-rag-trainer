"""Planification de la réconciliation par Celery beat (S8 ; ADR 0004 point 9 :
« réconciliation propre au contexte, planifiée » ; S0 §8.5).

Contrat :

- ``register_worker`` déclare la tâche ``knowledge_base.work.reconcile`` et
  l'inscrit au ``beat_schedule`` à ``settings.reconcile_interval``. Cette tâche
  ne réconcilie PAS elle-même : elle enregistre (``enqueue``) un item
  ``reconcile_knowledge_base`` dont la clé de dédoublonnage porte le créneau
  (``floor(now / intervalle)``), puis rend la main. Le travail est donc
  exécuté par la work queue (lease, fencing, retry, échec consultable), sur la
  file d'ingestion : le cycle est sérialisé avec les extractions, indexations
  et purges (``--concurrency=1``), ce qui écarte les courses entre la
  réconciliation et un traitement en vol.
- Comme ``dispatch`` et ``recover``, la tâche de planification va sur la file
  PAR DÉFAUT (jamais derrière une extraction longue).
- Le registre de production sert le kind ``reconcile_knowledge_base`` sur
  ``knowledge_base.ingestion`` (``build_work_handlers``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery import Celery
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from knowledge_base import KnowledgeBaseSettings, register_worker
from knowledge_base.adapters.controller.dependencies import build_work_handlers
from knowledge_base.adapters.controller.tasks.registry import HandlerRegistry
from runtime.worker import celery_app
from tests.unit.knowledge_base.s8_support import (
    INGESTION_QUEUE,
    RECONCILE_KIND,
    RECONCILE_TASK,
)

DEFAULT_QUEUE = "interview_rag_trainer.default"


def never_called() -> Any:
    raise AssertionError("les ressources ne doivent pas être créées à l'enregistrement")


def make_app(settings: KnowledgeBaseSettings | None = None) -> Celery:
    app = Celery("test_kb_reconcile")
    register_worker(
        app, never_called, settings or KnowledgeBaseSettings(_env_file=None)
    )
    return app


def seconds(schedule: Any) -> float:
    if isinstance(schedule, timedelta):
        return schedule.total_seconds()
    if isinstance(schedule, int | float):
        return float(schedule)
    return float(schedule.run_every.total_seconds())


def entries(app: Celery, task: str) -> list[dict[str, Any]]:
    return [e for e in app.conf.beat_schedule.values() if e["task"] == task]


def test_register_worker_declares_the_reconcile_task() -> None:
    assert RECONCILE_TASK in make_app().tasks


def test_the_reconcile_task_is_scheduled_once_at_the_configured_interval() -> None:
    settings = KnowledgeBaseSettings(_env_file=None, reconcile_interval_seconds=123)

    (entry,) = entries(make_app(settings), RECONCILE_TASK)

    assert seconds(entry["schedule"]) == 123


def test_the_default_interval_comes_from_the_settings() -> None:
    settings = KnowledgeBaseSettings(_env_file=None)

    (entry,) = entries(make_app(settings), RECONCILE_TASK)

    assert seconds(entry["schedule"]) == settings.reconcile_interval.total_seconds()


def test_the_reconcile_task_goes_to_the_default_queue_never_the_ingestion_queue() -> (
    None
):
    app = make_app()

    assert str(app.amqp.router.route({}, RECONCILE_TASK)["queue"].name) == DEFAULT_QUEUE


def test_the_existing_beat_entries_are_kept() -> None:
    app = make_app()

    assert len(entries(app, "knowledge_base.work.dispatch")) == 1
    assert len(entries(app, "knowledge_base.work.recover")) == 1


def test_the_production_worker_schedules_the_reconciliation() -> None:
    assert len(entries(celery_app, RECONCILE_TASK)) == 1


def test_production_composition_serves_reconcile_on_the_ingestion_queue() -> None:
    handlers = build_work_handlers(
        async_sessionmaker(),
        AsyncQdrantClient(url="http://localhost:1", check_compatibility=False),
        KnowledgeBaseSettings(_env_file=None),
    )

    registry = HandlerRegistry(handlers)

    assert registry.get(RECONCILE_KIND).queue == INGESTION_QUEUE
    assert registry.max_attempts_by_kind()[RECONCILE_KIND] >= 1

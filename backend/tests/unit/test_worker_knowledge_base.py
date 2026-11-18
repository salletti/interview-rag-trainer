"""Le composition root ``runtime.worker`` câble Knowledge Base (S0 §9, §10) ;
depuis S3, les processus enfants enregistrent les handlers de production."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from runtime.settings import Settings
from runtime.worker import celery_app
from runtime.worker_factory import _ChildProcessResources


def test_production_worker_registers_the_knowledge_base_tasks() -> None:
    assert {
        "knowledge_base.work.execute",
        "knowledge_base.work.dispatch",
        "knowledge_base.work.recover",
    } <= set(celery_app.tasks)


def test_production_worker_schedules_dispatch_and_recover() -> None:
    tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert {"knowledge_base.work.dispatch", "knowledge_base.work.recover"} <= tasks


def test_production_worker_keeps_the_technical_ping_task() -> None:
    assert "runtime.ping" in celery_app.tasks


def test_production_worker_prefetches_a_single_message() -> None:
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_production_worker_acknowledges_work_messages_early() -> None:
    task: Any = celery_app.tasks["knowledge_base.work.execute"]
    assert task.acks_late is False


@pytest.fixture
def production_child() -> Iterator[_ChildProcessResources]:
    """Ressources d'un enfant de production, créées comme au signal
    ``worker_process_init`` : boucle et moteur, sans aucune connexion."""
    child = _ChildProcessResources(Settings())
    child.start()
    try:
        yield child
    finally:
        child.stop()
        asyncio.set_event_loop(None)


def test_a_production_child_serves_extract_version_on_the_ingestion_queue(
    production_child: _ChildProcessResources,
) -> None:
    registry = production_child.get().registry

    assert registry.kinds_by_queue() == {
        "knowledge_base.ingestion": frozenset(
            {
                "extract_version",
                "index_version",
                "sync_visibility",
                "purge_document",
                "reconcile_knowledge_base",
            }
        )
    }
    assert registry.get("extract_version").kind == "extract_version"

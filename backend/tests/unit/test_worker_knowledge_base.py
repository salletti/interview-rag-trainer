"""Le composition root ``runtime.worker`` câble Knowledge Base (S0 §9, §10)."""

from __future__ import annotations

from typing import Any

from runtime.worker import celery_app


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

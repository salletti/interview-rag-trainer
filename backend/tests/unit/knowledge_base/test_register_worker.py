"""``knowledge_base.register_worker`` : déclaration des tâches, routes et
planification dans le processus maître, sans aucune ressource (S0 §9, §8).

Aucune connexion : l'application Celery n'est jamais démarrée.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from celery import Celery

import knowledge_base
from knowledge_base import KnowledgeBaseSettings, register_worker

EXECUTE = "knowledge_base.work.execute"
DISPATCH = "knowledge_base.work.dispatch"
RECOVER = "knowledge_base.work.recover"
DEFAULT_QUEUE = "interview_rag_trainer.default"


def never_called() -> Any:
    raise AssertionError("les ressources ne doivent pas être créées à l'enregistrement")


def make_app(settings: KnowledgeBaseSettings | None = None) -> Celery:
    app = Celery("test_kb")  # file par défaut de Celery : "celery"
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


def scheduled(app: Celery, task: str) -> dict[str, Any]:
    entries = [e for e in app.conf.beat_schedule.values() if e["task"] == task]
    assert len(entries) == 1, f"une seule entrée beat attendue pour {task}"
    entry: dict[str, Any] = entries[0]
    return entry


def queue_of(app: Celery, task: str) -> str:
    return str(app.amqp.router.route({}, task)["queue"].name)


def test_public_surface_is_reexported_from_the_package_root() -> None:
    for name in (
        "register_worker",
        "KnowledgeBaseWorkerResources",
        "KnowledgeBaseSettings",
        "WorkHandler",
    ):
        assert hasattr(knowledge_base, name), name


def test_register_worker_declares_the_three_work_tasks() -> None:
    app = make_app()
    assert {EXECUTE, DISPATCH, RECOVER} <= set(app.tasks)


def test_register_worker_does_not_call_the_resources_provider() -> None:
    # Le fournisseur lève s'il est appelé : l'enregistrement est paresseux.
    make_app()


def test_execute_acknowledges_early_explicitly() -> None:
    task = make_app().tasks[EXECUTE]
    assert task.acks_late is False


def test_execute_uses_the_hard_time_limit_from_the_settings() -> None:
    default = make_app().tasks[EXECUTE]
    custom = make_app(
        KnowledgeBaseSettings(_env_file=None, work_time_limit_seconds=120)
    ).tasks[EXECUTE]

    assert default.time_limit == 3600
    assert custom.time_limit == 120


def test_register_worker_leaves_the_prefetch_setting_to_the_composition_root() -> None:
    # Réglage du processus worker, posé par runtime (S0 §8.1), pas par le contexte.
    app = Celery("test_kb_prefetch")
    default = app.conf.worker_prefetch_multiplier
    register_worker(app, never_called, KnowledgeBaseSettings(_env_file=None))
    assert app.conf.worker_prefetch_multiplier == default


def test_dispatch_and_recover_go_to_the_default_queue_never_the_ingestion_queue() -> (
    None
):
    app = make_app()

    assert queue_of(app, DISPATCH) == DEFAULT_QUEUE
    assert queue_of(app, RECOVER) == DEFAULT_QUEUE


def test_beat_schedule_uses_the_default_intervals() -> None:
    app = make_app()

    assert seconds(scheduled(app, DISPATCH)["schedule"]) == 2
    assert seconds(scheduled(app, RECOVER)["schedule"]) == 30


def test_beat_schedule_follows_the_configured_intervals() -> None:
    app = make_app(
        KnowledgeBaseSettings(
            _env_file=None,
            work_dispatch_interval_seconds=7,
            work_recover_interval_seconds=11,
        )
    )

    assert seconds(scheduled(app, DISPATCH)["schedule"]) == 7
    assert seconds(scheduled(app, RECOVER)["schedule"]) == 11


def test_beat_does_not_schedule_the_execute_task() -> None:
    app = make_app()
    assert all(e["task"] != EXECUTE for e in app.conf.beat_schedule.values())


def test_register_worker_reads_the_environment_when_no_settings_are_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_WORK_DISPATCH_INTERVAL_SECONDS", "9")
    app = Celery("test_kb_env")
    register_worker(app, never_called)

    assert seconds(scheduled(app, DISPATCH)["schedule"]) == 9

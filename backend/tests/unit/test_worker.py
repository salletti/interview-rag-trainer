from __future__ import annotations

from runtime.worker import celery_app, ping


def test_ping_task_is_registered_under_a_technical_name() -> None:
    assert "runtime.ping" in celery_app.tasks
    assert ping.name == "runtime.ping"


def test_ping_runs_synchronously_without_a_broker() -> None:
    # Appel direct : aucune connexion, aucune sérialisation.
    assert ping() == "pong"


def test_worker_uses_json_only_and_rpc_result_backend() -> None:
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.result_backend.startswith("rpc://")

"""Scénario 13 : ressources asyncio dans un vrai processus enfant prefork.

Un worker ``celery worker --pool=prefork --concurrency=1`` est lancé en
sous-processus avec ``prefork_app`` (composition réelle de ``runtime``).
Deux tâches successives doivent s'exécuter dans le même enfant, sur la même
boucle et avec le même moteur, sans erreur « attached to a different loop ».

API figée côté ``runtime.worker_factory`` (``runtime.worker`` en construit
l'application de production) :
``create_celery_app(settings=None, *, work_handlers=())`` crée l'application,
appelle ``knowledge_base.register_worker`` avec un fournisseur paresseux et
branche ``worker_process_init`` (boucle + moteur ``pool_size=2`` +
``KnowledgeBaseWorkerResources``) et ``worker_process_shutdown``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from celery import Celery
from sqlalchemy import text

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.support import (
    build_app,
    enqueue_committed,
    fetch_item,
    prefork_worker_process,
    wait_for_status,
)

pytestmark = pytest.mark.integration

PROBE_KIND = "test.prefork_probe"


@pytest.fixture
def prefork_worker(
    integration_settings: Settings, tmp_path: Path
) -> Iterator[tuple[str, Path]]:
    queue = f"kb_test.prefork.{uuid.uuid4().hex[:8]}"
    log_path = tmp_path / "worker.log"
    with prefork_worker_process(integration_settings, queue, log_path):
        yield queue, log_path


async def test_two_tasks_in_a_prefork_child_reuse_the_same_loop_and_engine(
    integration_settings: Settings,
    session_factory: SessionFactory,
    prefork_worker: tuple[str, Path],
) -> None:
    queue, log_path = prefork_worker
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kb_test_probe (item_id uuid, pid int, "
                "loop_id bigint, engine_id bigint)"
            )
        )
        await session.execute(text("TRUNCATE kb_test_probe"))
    client: Celery = build_app(integration_settings)
    first = await enqueue_committed(session_factory, PROBE_KIND)
    second = await enqueue_committed(session_factory, PROBE_KIND)

    try:
        client.send_task("knowledge_base.work.execute", args=[str(first)], queue=queue)
        await wait_for_status(session_factory, first, "SUCCEEDED", limit=90)
        client.send_task("knowledge_base.work.execute", args=[str(second)], queue=queue)
        await wait_for_status(session_factory, second, "SUCCEEDED", limit=30)
    except TimeoutError:
        pytest.fail(
            "worker prefork : tâche non terminée\n"
            + log_path.read_text(errors="replace")[-3000:]
        )

    async with session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT pid, loop_id, engine_id FROM kb_test_probe")
            )
        ).all()
    log = log_path.read_text(errors="replace")
    assert len(rows) == 2, log[-3000:]
    assert rows[0] == rows[1], "même enfant, même boucle, même moteur attendus"
    assert rows[0].pid != os.getpid()
    assert "attached to a different loop" not in log
    assert (await fetch_item(session_factory, second))["attempts"] == 1

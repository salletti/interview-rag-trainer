"""Scénario 14 : message préchargé non acquitté au-delà d'un ``consumer_timeout``
court (S0 §8.1, implication résiduelle de ``acks_late = False``).

Le scénario exige un vrai worker prefork : c'est le processus maître qui
acquitte le message de la tâche longue et précharge le suivant pendant que
l'enfant travaille. Dans le pool solo in-process, l'acquittement anticipé est
différé (``hub.call_soon``) jusqu'à la fin de la tâche : le message suivant
n'est jamais livré et le scénario ne se produit pas.

RabbitMQ ne vérifie ce délai que périodiquement et le comportement exact
(``notify_consumer``, fermeture du canal, redélivrance) dépend du type de
file : le test attend donc plus longtemps que le délai et n'affirme que
l'invariant qui compte, indépendant du mécanisme de reprise (redélivrance du
broker ou redispatch) : chaque item atteint ``SUCCEEDED`` avec **un seul**
effet métier grâce au claim conditionnel. Ce qui a été observé est imprimé
(``pytest -s``) et consigné dans
``docs/conception/bounded-contexts/knowledge-base-s0.md`` §12.2 (#14).

Activation : ``KB_TEST_CONSUMER_TIMEOUT_SECONDS=<délai configuré côté broker>``
dans l'environnement de ``pytest``. ``make test-integration`` monte
``tests/integration/rabbitmq_test/20-consumer-timeout.conf`` dans
``rabbitmq_test`` et exporte la valeur correspondante. Sans cette variable, le
test est ignoré explicitement (pas omis).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import pytest

from knowledge_base import KnowledgeBaseWorkerResources, WorkHandler
from knowledge_base.adapters.controller.tasks.dispatch import dispatch_pending
from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.support import (
    build_app,
    effects_of,
    enqueue_committed,
    fetch_item,
    kb_settings,
    prefork_worker_process,
    queue_depth,
    wait_for_status,
)

pytestmark = pytest.mark.integration

CONFIGURED_TIMEOUT = os.environ.get("KB_TEST_CONSUMER_TIMEOUT_SECONDS")
EXECUTE = "knowledge_base.work.execute"
SLOW_KIND = "test.prefork_slow"  # SlowHandler de prefork_app
WORKER_LOG_EVENTS = re.compile(
    r"cancel|restor|redeliver|PRECONDITION|delivery tag|reconnect|Connection",
    re.IGNORECASE,
)


def dispatch_side_handler(queue: str) -> WorkHandler:
    """Ce que le dispatch du test doit savoir du handler : son ``kind`` et sa
    file. L'exécution a lieu dans le worker prefork, pas ici."""

    class SlowHandlerRoute:
        kind: ClassVar[str] = SLOW_KIND
        max_attempts: ClassVar[int] = 5

        async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
            raise AssertionError("exécuté par le worker prefork, jamais ici")

    cls: type[Any] = type("SlowHandlerRoute", (SlowHandlerRoute,), {"queue": queue})
    handler: WorkHandler = cls()
    return handler


@pytest.mark.skipif(
    CONFIGURED_TIMEOUT is None,
    reason=(
        "rabbitmq_test n'a pas de consumer_timeout court : monter un "
        "rabbitmq.conf (consumer_timeout) puis exporter "
        "KB_TEST_CONSUMER_TIMEOUT_SECONDS (voir S0 §11 et §12.2 #14)"
    ),
)
async def test_a_prefetched_message_outliving_consumer_timeout_causes_no_double_effect(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    timeout = float(CONFIGURED_TIMEOUT or "0")
    queue = f"kb_test.consumer_timeout.{uuid.uuid4().hex[:8]}"
    release, log_path = tmp_path / "release", tmp_path / "worker.log"
    settings = kb_settings(work_redispatch_after_seconds=timeout, work_max_in_flight=5)
    # Enqueue successifs : ``long_item`` est le plus ancien, donc dispatché en
    # premier ; ``waiting`` attend derrière lui.
    long_item = await enqueue_committed(
        session_factory, SLOW_KIND, payload={"slow": "yes"}
    )
    waiting = await enqueue_committed(session_factory, SLOW_KIND)
    client = build_app(integration_settings)
    resources = KnowledgeBaseWorkerResources(
        session_factory=session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(dispatch_side_handler(queue),),
    )

    def publish(item_id: UUID, target: str) -> None:
        client.send_task(EXECUTE, args=[str(item_id)], queue=target)

    with prefork_worker_process(
        integration_settings, queue, log_path, {"KB_TEST_RELEASE_FILE": str(release)}
    ):
        assert await dispatch_pending(resources, settings, publish=publish) == 2
        await wait_for_status(session_factory, long_item, "RUNNING", limit=90)

        # ``message_count`` ne compte que les messages prêts : 0 tant que le
        # message de ``waiting`` est préchargé (livré, non acquitté), 1 s'il est
        # revenu dans la file. On relève chaque changement dans le temps.
        loop = asyncio.get_running_loop()
        started = loop.time()
        depths: list[tuple[int, int]] = []
        while (elapsed := loop.time() - started) < timeout * 2 + 70:
            depth = await asyncio.to_thread(queue_depth, integration_settings, queue)
            if not depths or depths[-1][1] != depth:
                depths.append((round(elapsed), depth))
            await asyncio.sleep(0.5)
        waiting_before_release = (await fetch_item(session_factory, waiting))["status"]

        await asyncio.to_thread(release.touch)
        await wait_for_status(session_factory, long_item, "SUCCEEDED", limit=120)
        redispatched = await dispatch_pending(resources, settings, publish=publish)
        await wait_for_status(session_factory, waiting, "SUCCEEDED", limit=120)

    for item_id in (long_item, waiting):
        assert len(await effects_of(session_factory, item_id)) == 1
        assert (await fetch_item(session_factory, item_id))["status"] == "SUCCEEDED"

    log = log_path.read_text(errors="replace")
    # Trace du comportement observé (S0 §12.2 #14), visible avec ``-s``.
    print(  # noqa: T201
        f"#14 depths(s, prêts)={depths} "
        f"waiting_avant_liberation={waiting_before_release} "
        f"redispatched={redispatched} "
        f"attempts_waiting={(await fetch_item(session_factory, waiting))['attempts']} "
        f"executions_recues={log.count('Task ' + EXECUTE)}\n"
        + "\n".join(line for line in log.splitlines() if WORKER_LOG_EVENTS.search(line))
    )

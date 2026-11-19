"""Application Celery du scénario 13, lancée par un vrai worker prefork.

Importée dans le processus maître **et** dans l'enfant : elle ne crée ni
boucle ni moteur (c'est la composition de ``runtime.worker_factory`` qui le
fait au signal ``worker_process_init``). Elle importe la fabrique et non
``runtime.worker`` : ce dernier construit l'application de production, et un
enfant *spawn* (macOS) exécuterait alors ses tâches homonymes, sans le handler
de test. Le handler enregistre ce qu'il observe dans l'enfant
(pid, identité de la boucle et du moteur) dans ``kb_test_probe``.

``KB_TEST_PREFORK_QUEUE`` (file unique) est posée par le test.
``SlowHandler`` (scénario 14) bloque les items ``{"slow": "yes"}`` jusqu'à
l'apparition du fichier ``KB_TEST_RELEASE_FILE``, puis écrit son effet dans
``kb_test_effects`` (même table que ``support.write_effect``).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import ClassVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from runtime.worker_factory import create_celery_app

PROBE_KIND = "test.prefork_probe"
SLOW_KIND = "test.prefork_slow"
QUEUE = os.environ.get("KB_TEST_PREFORK_QUEUE", "kb_test.prefork")


class ProbeHandler:
    kind: ClassVar[str] = PROBE_KIND
    queue: ClassVar[str] = QUEUE
    max_attempts: ClassVar[int] = 3

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        loop_id = id(asyncio.get_running_loop())

        async def callback(session: AsyncSession) -> None:
            await session.execute(
                text(
                    "INSERT INTO kb_test_probe (item_id, pid, loop_id, engine_id) "
                    "VALUES (:item_id, :pid, :loop_id, :engine_id)"
                ),
                {
                    "item_id": item.id,
                    "pid": os.getpid(),
                    "loop_id": loop_id,
                    "engine_id": id(session.get_bind()),
                },
            )

        await ctx.finalize(callback)


class SlowHandler:
    kind: ClassVar[str] = SLOW_KIND
    queue: ClassVar[str] = QUEUE
    max_attempts: ClassVar[int] = 5

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        if item.payload.get("slow") == "yes":
            release = Path(os.environ["KB_TEST_RELEASE_FILE"])
            while not await asyncio.to_thread(release.exists):  # noqa: ASYNC110
                await asyncio.sleep(0.1)

        async def callback(session: AsyncSession) -> None:
            await session.execute(
                text(
                    "INSERT INTO kb_test_effects (item_id, fencing_token, note) "
                    "VALUES (:item_id, :token, 'done')"
                ),
                {"item_id": item.id, "token": item.fencing_token},
            )

        await ctx.finalize(callback)


app = create_celery_app(work_handlers=[ProbeHandler(), SlowHandler()])
# Un enfant spawn (macOS) ne reconstruit les tâches qu'en important les
# modules de ``include`` : il doit réimporter ce module de composition.
app.conf.include = [__name__]

"""Aides des tests d'intégration S0 : handlers de test, lectures SQL, rig de
worker in-process.

Le handler de test est un artefact de test : il n'existe pas dans le code de
production (S0 §12.2, §14). Son « effet métier » est une ligne dans
``kb_test_effects`` (table de test, schéma ``public``), écrite uniquement dans
``ctx.finalize`` : c'est ce qui permet de compter les effets appliqués.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from celery import Celery
from celery.contrib.testing.worker import start_worker
from kombu.connection import Connection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from knowledge_base import (
    KnowledgeBaseSettings,
    KnowledgeBaseWorkerResources,
    WorkHandler,
    register_worker,
)
from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
    SqlAlchemyWorkQueue,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from runtime.database import SessionFactory, create_engine, create_session_factory
from runtime.settings import Settings

BACKEND = Path(__file__).resolve().parents[3]
PREFORK_APP = "tests.integration.knowledge_base.prefork_app:app"

Behaviour = Callable[[ClaimedWorkItem, WorkContext], Awaitable[None]]
FailureHook = Callable[[AsyncSession, ClaimedWorkItem, FailureInfo], Awaitable[None]]

NewHandler = Callable[..., WorkHandler]

CREATE_EFFECTS = """
CREATE TABLE IF NOT EXISTS kb_test_effects (
    id serial PRIMARY KEY,
    item_id uuid NOT NULL,
    fencing_token bigint NOT NULL,
    note text NOT NULL
)
"""


def kb_settings(**overrides: float) -> KnowledgeBaseSettings:
    return KnowledgeBaseSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --- handlers de test ---------------------------------------------------------------


async def write_effect(
    session: AsyncSession, item: ClaimedWorkItem, note: str = "done"
) -> None:
    await session.execute(
        text(
            "INSERT INTO kb_test_effects (item_id, fencing_token, note) "
            "VALUES (:item_id, :token, :note)"
        ),
        {"item_id": item.id, "token": item.fencing_token, "note": note},
    )


async def finalize_with_effect(item: ClaimedWorkItem, ctx: WorkContext) -> None:
    """Comportement nominal : un effet métier, écrit dans la finalisation."""

    async def callback(session: AsyncSession) -> None:
        await write_effect(session, item)

    await ctx.finalize(callback)


class ScriptedHandler:
    """Handler dont le comportement est fourni par le test."""

    kind: ClassVar[str] = "test.scripted"
    queue: ClassVar[str] = "kb_test.scripted"
    max_attempts: ClassVar[int] = 5

    def __init__(
        self, behaviour: Behaviour, failure_hook: FailureHook | None = None
    ) -> None:
        self._behaviour = behaviour
        self._failure_hook = failure_hook

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await self._behaviour(item, ctx)

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        if self._failure_hook is not None:
            await self._failure_hook(session, item, error)


class HooklessHandler:
    """Handler sans ``on_terminal_failure`` : le hook est facultatif (§5)."""

    kind: ClassVar[str] = "test.hookless"
    queue: ClassVar[str] = "kb_test.hookless"
    max_attempts: ClassVar[int] = 5

    def __init__(self, behaviour: Behaviour) -> None:
        self._behaviour = behaviour

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await self._behaviour(item, ctx)


def unique_handler(
    behaviour: Behaviour,
    failure_hook: FailureHook | None = None,
    *,
    hookless: bool = False,
    max_attempts: int | None = None,
) -> WorkHandler:
    """Handler avec un ``kind`` et une file uniques : aucune interférence avec
    les messages laissés dans RabbitMQ par un autre test ni avec le
    ``worker_test`` de compose."""
    suffix = uuid.uuid4().hex[:10]
    base: type[Any] = HooklessHandler if hookless else ScriptedHandler
    attrs: dict[str, object] = {"kind": f"test.{suffix}", "queue": f"kb_test.{suffix}"}
    if max_attempts is not None:
        attrs["max_attempts"] = max_attempts
    cls = type(f"Handler_{suffix}", (base,), attrs)
    handler: WorkHandler = cls(behaviour) if hookless else cls(behaviour, failure_hook)
    return handler


# --- lectures et écritures SQL de test ----------------------------------------------


async def fetch_item(factory: SessionFactory, item_id: UUID) -> dict[str, Any]:
    async with factory() as session:
        result = await session.execute(
            text("SELECT * FROM knowledge_base.work_items WHERE id = :id"),
            {"id": item_id},
        )
        return dict(result.mappings().one())


async def fetch_scalar(factory: SessionFactory, sql: str, **params: object) -> Any:
    async with factory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def count_items(factory: SessionFactory) -> int:
    return int(
        await fetch_scalar(factory, "SELECT count(*) FROM knowledge_base.work_items")
    )


async def effects_of(factory: SessionFactory, item_id: UUID) -> list[int]:
    """Jetons de fencing des effets métier appliqués pour cet item."""
    async with factory() as session:
        rows = await session.execute(
            text("SELECT fencing_token FROM kb_test_effects WHERE item_id = :id"),
            {"id": item_id},
        )
        return [int(row[0]) for row in rows]


async def effect_notes(factory: SessionFactory, item_id: UUID) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT note FROM kb_test_effects WHERE item_id = :id"),
            {"id": item_id},
        )
        return [str(row[0]) for row in rows]


async def enqueue_committed(
    factory: SessionFactory,
    kind: str,
    dedup_key: str | None = None,
    *,
    max_attempts: int | None = None,
    payload: dict[str, str] | None = None,
) -> UUID:
    async with factory() as session, session.begin():
        return await SqlAlchemyWorkQueue(session).enqueue(
            kind,
            dedup_key or f"{kind}:{uuid.uuid4()}",
            payload,
            max_attempts=max_attempts,
        )


async def cancel_committed(factory: SessionFactory, dedup_key: str) -> bool:
    async with factory() as session, session.begin():
        return await SqlAlchemyWorkQueue(session).cancel(dedup_key)


async def set_item(factory: SessionFactory, item_id: UUID, **columns: str) -> None:
    """Manipule directement la ligne (``columns`` : colonne -> expression SQL)."""
    assignments = ", ".join(f"{name} = {expr}" for name, expr in columns.items())
    async with factory() as session, session.begin():
        await session.execute(
            text(f"UPDATE knowledge_base.work_items SET {assignments} WHERE id = :id"),
            {"id": item_id},
        )


async def wait_for(
    condition: Callable[[], Awaitable[bool]], *, limit: float = 30.0
) -> None:
    async with asyncio.timeout(limit):
        while not await condition():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def wait_for_status(
    factory: SessionFactory, item_id: UUID, status: str, *, limit: float = 30.0
) -> None:
    async def reached() -> bool:
        return bool((await fetch_item(factory, item_id))["status"] == status)

    await wait_for(reached, limit=limit)


# --- RabbitMQ -----------------------------------------------------------------------


def queue_depth(settings: Settings, queue: str) -> int:
    with Connection(settings.broker_url) as connection:
        declared = connection.default_channel.queue_declare(queue=queue, passive=True)
        return int(declared.message_count)


def pop_message_with_ack(settings: Settings, queue: str) -> bool:
    """Consomme et acquitte un message (``no_ack``) : simule un worker qui
    l'a reçu puis a disparu avant le claim. Faux si la file est vide."""
    with Connection(settings.broker_url) as connection:
        message = connection.default_channel.basic_get(queue=queue, no_ack=True)
        return message is not None


def delete_queue(settings: Settings, queue: str) -> None:
    with Connection(settings.broker_url) as connection:
        connection.default_channel.queue_delete(queue=queue)


def declare_queue(settings: Settings, queue: str) -> None:
    with Connection(settings.broker_url) as connection:
        connection.default_channel.queue_declare(queue=queue, durable=True)


# --- worker Celery in-process (pool solo) -------------------------------------------


@dataclass(frozen=True)
class WorkerRig:
    app: Celery
    resources: KnowledgeBaseWorkerResources
    settings: KnowledgeBaseSettings


def build_app(pg_settings: Settings) -> Celery:
    app = Celery("kb_test", broker=pg_settings.broker_url)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        task_default_queue=f"kb_test.default.{uuid.uuid4().hex[:8]}",
    )
    return app


@contextmanager
def consuming(rig: WorkerRig) -> Iterator[None]:
    """Démarre le worker solo (thread) sur les files des handlers du rig."""
    queues = sorted({handler.queue for handler in rig.resources.handlers})
    with start_worker(
        rig.app, perform_ping_check=False, queues=queues, loglevel="WARNING"
    ):
        yield


@contextmanager
def worker_rig(
    pg_settings: Settings,
    handlers: Sequence[WorkHandler],
    settings: KnowledgeBaseSettings,
    *,
    consume: bool = True,
) -> Iterator[WorkerRig]:
    """Worker Celery solo dans un thread du processus de test : broker et base
    réels, registre de test. La boucle et le moteur du worker sont distincts de
    ceux du test (comme dans un processus enfant). ``consume=False`` construit
    l'application et les ressources sans démarrer le worker : les messages
    publiés ne sont pas consommés tant que le test n'utilise pas
    ``consuming(rig)``."""
    loop = asyncio.new_event_loop()
    engine = create_engine(pg_settings)
    resources = KnowledgeBaseWorkerResources(
        session_factory=create_session_factory(engine),
        loop=loop,
        handlers=tuple(handlers),
    )
    app = build_app(pg_settings)
    register_worker(app, lambda: resources, settings)
    rig = WorkerRig(app=app, resources=resources, settings=settings)
    try:
        if consume:
            with consuming(rig):
                yield rig
        else:
            yield rig
    finally:
        # Le test tourne dans sa propre boucle (thread courant) : la boucle du
        # worker se ferme dans un autre thread, comme elle a tourné.
        with ThreadPoolExecutor(max_workers=1) as closer:
            closer.submit(_close_worker_loop, loop, engine).result()


def _close_worker_loop(loop: asyncio.AbstractEventLoop, engine: AsyncEngine) -> None:
    try:
        loop.run_until_complete(engine.dispose())
    finally:
        loop.close()


def run_dispatch_task(rig: WorkerRig) -> int:
    """Exécute la tâche ``dispatch`` en ligne (sans file) ; la publication des
    messages d'``execute`` passe, elle, par le vrai RabbitMQ. À appeler hors de
    la boucle du test (``asyncio.to_thread``) : la tâche pilote la boucle du
    worker avec ``run_until_complete``. Renvoie le nombre d'items notifiés."""
    return int(rig.app.tasks["knowledge_base.work.dispatch"].apply().get())


def run_recover_task(rig: WorkerRig) -> int:
    """Idem pour ``recover`` : renvoie le nombre d'items repris."""
    return int(rig.app.tasks["knowledge_base.work.recover"].apply().get())


async def seconds_from_now(
    factory: SessionFactory, item_id: UUID, column: str
) -> float:
    """Écart ``colonne - now()`` en secondes, calculé par PostgreSQL (aucune
    dépendance à l'horloge de l'hôte)."""
    return float(
        await fetch_scalar(
            factory,
            f"SELECT extract(epoch FROM {column} - now()) "
            "FROM knowledge_base.work_items WHERE id = :id",
            id=item_id,
        )
    )


# --- worker Celery prefork en sous-processus ----------------------------------------


@contextmanager
def prefork_worker_process(
    pg_settings: Settings,
    queue: str,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> Iterator[None]:
    """Vrai ``celery worker --pool=prefork --concurrency=1`` sur ``queue``, avec
    ``prefork_app`` (composition réelle de ``runtime``). Le maître acquitte et
    précharge pendant que l'enfant exécute : c'est ce que le pool solo
    in-process ne reproduit pas. La file est supprimée à la sortie."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(BACKEND / "src"), str(BACKEND)]),
        "KB_TEST_PREFORK_QUEUE": queue,
        **(extra_env or {}),
    }
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        PREFORK_APP,
        "worker",
        "--pool=prefork",
        "--concurrency=1",
        "--prefetch-multiplier=1",
        "-Q",
        queue,
        "--loglevel=INFO",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
    ]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command, cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            yield
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            delete_queue(pg_settings, queue)

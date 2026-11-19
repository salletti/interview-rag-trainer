"""``index_version`` : lease prolongé pendant ``prepare`` (S4 ; S0 §7 ;
knowledge-base.md §7.3).

- Les embeddings tournent dans un thread : la boucle envoie les heartbeats
  pendant un ``prepare`` lent, puis l'item est finalisé une fois.
- Un heartbeat refusé (``StaleAttempt``) arrête la tentative sans
  finalisation ET interrompt ``prepare`` : aucun upsert ``visible=false`` ne
  suit le refus (tentative zombie, §7.3 condition 6).
- La composition de production prolonge le lease au rythme des réglages.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base import KnowledgeBaseSettings
from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.index_version_handler import (
    IndexVersionWorkHandler,
    build_index_version_handler,
)
from knowledge_base.adapters.controller.tasks.retry_policy import StaleAttempt
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    HashEmbedder,
    IndexingRig,
    IndexingUnitOfWork,
    make_indexing_rig,
    qa_sheet,
    single_profile,
)

SESSION = cast(AsyncSession, object())
HEARTBEAT = timedelta(seconds=0.02)
EMBED_SECONDS = 0.1
QUESTIONS = 3  # un chunk par question, un lot par chunk (batch_size=1)


class SlowEmbedder(HashEmbedder):
    """Bloque son thread comme un encodage E5 sur CPU."""

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        await asyncio.to_thread(time.sleep, EMBED_SECONDS)
        return await super().embed_passages(texts)


class Context:
    """Double de ``WorkContext`` : heartbeats comptés (refusés si ``stale``),
    finalisation dans une Unit of Work validée."""

    def __init__(self, rig: IndexingRig, *, stale: bool = False) -> None:
        self._rig = rig
        self._stale = stale
        self.current: IndexingUnitOfWork | None = None
        self.heartbeats = 0
        self.finalize_calls = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1
        if self._stale:
            raise StaleAttempt("lease repris par une autre tentative")

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1
        async with self._rig.unit_of_work() as uow:
            self.current = uow
            await callback(SESSION)
            await uow.commit()


def handler(rig: IndexingRig, ctx: Context) -> IndexVersionWorkHandler:
    def transaction(session: AsyncSession) -> IndexingUnitOfWork:
        assert ctx.current is not None
        return ctx.current

    return IndexVersionWorkHandler(
        use_case=rig.complete, transaction=transaction, heartbeat_interval=HEARTBEAT
    )


def item(command: RecordExtraction, index_id: UUID) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind="index_version",
        payload={
            "document_id": str(command.document_id),
            "version_id": str(command.version_id),
            "index_id": str(index_id),
        },
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


async def ready(rig: IndexingRig) -> tuple[RecordExtraction, UUID]:
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet(QUESTIONS))
    await rig.extract(command)
    return command, index_id


async def test_heartbeats_are_sent_while_the_embeddings_run_in_a_thread() -> None:
    rig = make_indexing_rig(batch_size=1, embedder=SlowEmbedder())
    ctx = Context(rig)
    command, index_id = await ready(rig)

    await handler(rig, ctx).execute(item(command, index_id), cast(WorkContext, ctx))

    # 3 lots de 0,1 s, un heartbeat toutes les 0,02 s : bien plus de 3 si la
    # boucle reste libre ; 0 si l'encodage la bloquait.
    assert ctx.heartbeats >= 3
    assert ctx.finalize_calls == 1
    assert rig.indexation(command, index_id).status is IndexationStatus.COMPLETED
    assert rig.status(command) is VersionStatus.PUBLISHED


async def test_a_refused_heartbeat_interrupts_prepare_and_never_finalizes() -> None:
    rig = make_indexing_rig(batch_size=1, embedder=SlowEmbedder())
    ctx = Context(rig, stale=True)
    command, index_id = await ready(rig)

    with pytest.raises(StaleAttempt):
        await handler(rig, ctx).execute(item(command, index_id), cast(WorkContext, ctx))
    # Le zombie aurait eu le temps d'écrire ses 3 lots s'il continuait.
    await asyncio.sleep(EMBED_SECONDS * (QUESTIONS + 1))

    assert ctx.heartbeats == 1
    assert ctx.finalize_calls == 0
    assert rig.vector.upsert_sizes == []
    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING
    assert rig.status(command) is VersionStatus.EXTRACTED


def test_the_production_handler_keeps_the_lease_at_the_configured_interval() -> None:
    settings = KnowledgeBaseSettings(
        _env_file=None, work_lease_ttl_seconds=30, work_heartbeat_interval_seconds=7
    )

    built = build_index_version_handler(
        cast("async_sessionmaker[AsyncSession]", object()),
        settings,
        embedders=single_profile(HashEmbedder()),
        vector_index=make_indexing_rig().vector,
    )

    assert built.heartbeat_interval == timedelta(seconds=7)

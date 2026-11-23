"""``extract_version`` : original manquant et travail CPU long (S3 ; S0 §5, §7).

- Original absent du volume : ``TerminalWorkError(BLOB_MISSING)``, sans
  finalisation ni retry (incohérence durable) ; la version échouée reste
  ``retryable`` (une restauration du blob rend ``RetryIngestion`` utile).
- Parsing, segmentation et chunking tournent dans un thread : la boucle
  asyncio envoie les heartbeats pendant le calcul ; un heartbeat refusé arrête
  la tentative (``StaleAttempt``) sans finalisation.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    BLOB_MISSING,
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.controller.tasks.retry_policy import (
    StaleAttempt,
    TerminalWorkError,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.providers.document_blob_reader import (
    BlobMissingError,
    DocumentBlobReader,
)
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.structured_document import StructuredDocument
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    RecordExtractionHandler,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER, policy
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    ExtractionRig,
    ExtractionUnitOfWork,
    make_rig,
)

SESSION = cast(AsyncSession, object())
COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"
PARSE_SECONDS = 0.3
HEARTBEAT = timedelta(seconds=0.02)


class MissingBlobs:
    async def read(self, content_hash: ContentHash) -> bytes:
        raise BlobMissingError(content_hash)


class SlowParser:
    """Bloque son thread comme un parsing Docling long (CPU ou I/O)."""

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        time.sleep(PARSE_SECONDS)
        return MarkdownLiteParser().parse(content, media_type, filename)


class Context:
    """Double de ``WorkContext`` : heartbeats comptés, finalisation dans une
    Unit of Work validée."""

    def __init__(self, rig: ExtractionRig, *, stale: bool = False) -> None:
        self._rig = rig
        self._stale = stale
        self.current: ExtractionUnitOfWork | None = None
        self.heartbeats = 0
        self.finalize_calls = 0
        self.finalized = False

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
        self.finalized = True


def handler(
    rig: ExtractionRig,
    ctx: Context,
    use_case: RecordExtractionHandler,
    heartbeat: timedelta | None = None,
) -> ExtractVersionWorkHandler:
    def transaction(session: AsyncSession) -> ExtractionUnitOfWork:
        assert ctx.current is not None
        return ctx.current

    return ExtractVersionWorkHandler(
        use_case=use_case, transaction=transaction, heartbeat_interval=heartbeat
    )


def use_case(
    rig: ExtractionRig,
    *,
    blobs: DocumentBlobReader | None = None,
    parser: DocumentParser | None = None,
) -> RecordExtractionHandler:
    return RecordExtractionHandler(
        unit_of_work=rig.unit_of_work,
        blob_reader=blobs or rig.blobs,
        parser=parser or rig.parser,
        token_counter=COUNTER,
        policy=policy(),
    )


def claimed(command: RecordExtraction) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind="extract_version",
        payload={
            "document_id": str(command.document_id),
            "version_id": str(command.version_id),
        },
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


def status(rig: ExtractionRig, command: RecordExtraction) -> VersionStatus:
    document = rig.harness.state.documents[command.document_id]
    return document.get_version(command.version_id).status


# --- original manquant ----------------------------------------------------------


async def test_a_missing_original_is_a_terminal_blob_missing_without_finalize() -> None:
    rig = make_rig()
    ctx = Context(rig)
    command = await rig.import_document(COURSE)
    work = handler(rig, ctx, use_case(rig, blobs=MissingBlobs()))

    with pytest.raises(TerminalWorkError) as raised:
        await work.execute(claimed(command), cast(WorkContext, ctx))

    assert raised.value.code == BLOB_MISSING
    assert ctx.finalize_calls == 0
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.RECEIVED


async def test_the_version_failed_on_a_missing_blob_stays_retryable() -> None:
    rig = make_rig()
    ctx = Context(rig)
    command = await rig.import_document(COURSE)
    work = handler(rig, ctx, use_case(rig, blobs=MissingBlobs()))

    async with rig.unit_of_work() as uow:
        ctx.current = uow
        await work.on_terminal_failure(
            SESSION, claimed(command), FailureInfo(BLOB_MISSING, "introuvable")
        )
        await uow.commit()

    version = rig.harness.state.documents[command.document_id].get_version(
        command.version_id
    )
    assert version.status is VersionStatus.FAILED
    assert version.failure is not None
    assert (version.failure.code, version.failure.retryable) == (BLOB_MISSING, True)


# --- travail long et lease -------------------------------------------------------


async def test_heartbeats_are_sent_while_the_parser_runs_in_a_thread() -> None:
    rig = make_rig()
    ctx = Context(rig)
    command = await rig.import_document(COURSE)
    work = handler(rig, ctx, use_case(rig, parser=SlowParser()), heartbeat=HEARTBEAT)

    await work.execute(claimed(command), cast(WorkContext, ctx))

    # 0,3 s de parsing, un heartbeat toutes les 0,02 s : bien plus de 3 si la
    # boucle reste libre ; 0 si le parsing la bloquait.
    assert ctx.heartbeats >= 3
    assert ctx.finalize_calls == 1
    assert status(rig, command) is VersionStatus.EXTRACTED


async def test_a_refused_heartbeat_stops_the_attempt_without_finalize() -> None:
    rig = make_rig()
    ctx = Context(rig, stale=True)
    command = await rig.import_document(COURSE)
    work = handler(rig, ctx, use_case(rig, parser=SlowParser()), heartbeat=HEARTBEAT)

    with pytest.raises(StaleAttempt):
        await work.execute(claimed(command), cast(WorkContext, ctx))

    assert ctx.finalize_calls == 0
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.RECEIVED


async def test_no_heartbeat_task_outlives_a_short_extraction() -> None:
    rig = make_rig()
    ctx = Context(rig)
    command = await rig.import_document(COURSE)
    work = handler(rig, ctx, use_case(rig), heartbeat=timedelta(seconds=60))

    await work.execute(claimed(command), cast(WorkContext, ctx))

    assert ctx.heartbeats == 0
    assert ctx.finalize_calls == 1

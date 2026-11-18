"""Handlers de work items ``index_version`` et ``sync_visibility`` : ponts entre
la work queue de S0 et les use cases ``CompleteIndexation`` et
``SyncDocumentVisibility`` (S4 ; S0 §5).

API figée (``knowledge_base.adapters.controller.tasks``), même forme que
``extract_version_handler`` (S3) :

- ``index_version_handler.IndexVersionWorkHandler(use_case:
  CompleteIndexationHandler, transaction: Callable[[AsyncSession],
  <UnitOfWork étendue>])`` ; ``kind = "index_version"``, ``queue =
  "knowledge_base.ingestion"`` (aucune file nouvelle à câbler), ``max_attempts``
  ; ``HandlesTerminalFailure``. ``execute`` : payload ``document_id``,
  ``version_id``, ``index_id`` ; ``use_case.prepare`` hors transaction ;
  ``IndexManifestMismatchError`` -> ``TransientWorkError`` ;
  ``CollectionMismatchError`` -> ``TerminalWorkError("COLLECTION_MISMATCH")`` ;
  payload illisible -> ``TerminalWorkError("INVALID_PAYLOAD")`` ; sinon UN appel
  à ``ctx.finalize`` dont le callback fait ``use_case.apply`` (même quand
  ``prepare`` n'a rien à faire : l'item doit se terminer).
  ``on_terminal_failure(session, item, error)`` : ``use_case.fail`` avec
  ``FailureReason(error.code, error.message, retryable=True)`` ; payload
  illisible : ne fait rien.
- ``build_index_version_handler(session_factory, settings, *, embedder,
  vector_index, clock=None, batch_size=32) -> IndexVersionWorkHandler`` :
  composition de production, sans connexion à la construction.
- ``sync_visibility_handler.SyncVisibilityWorkHandler(use_case:
  SyncDocumentVisibilityHandler)`` ; ``kind = "sync_visibility"``, même file ;
  ``execute`` : payload ``document_id`` et ``visibility_revision`` (entier en
  texte) -> ``use_case.handle`` puis UN ``ctx.finalize`` dont le callback
  n'écrit rien (fencing + item ``SUCCEEDED``) ; payload illisible ->
  ``TerminalWorkError("INVALID_PAYLOAD")``.
- ``build_sync_visibility_handler(session_factory, *, vector_index)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from knowledge_base.adapters.controller.tasks.registry import (
    HandlerRegistry,
    HandlesTerminalFailure,
)
from knowledge_base.adapters.controller.tasks.retry_policy import (
    TerminalWorkError,
    TransientWorkError,
)
from knowledge_base.adapters.controller.tasks.sync_visibility_handler import (
    SyncVisibilityWorkHandler,
    build_sync_visibility_handler,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    BrokenEmbedder,
    HashEmbedder,
    IndexingRig,
    IndexingUnitOfWork,
    drop_first,
    make_indexing_rig,
    qa_sheet,
)

SESSION = cast(AsyncSession, object())


class FakeContext:
    """Rejoue ``WorkContext.finalize`` : callback dans une transaction que l'on
    valide, une seule fois, sur succès du callback."""

    def __init__(self, rig: IndexingRig) -> None:
        self._rig = rig
        self.current: IndexingUnitOfWork | None = None
        self.finalize_calls = 0

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1
        async with self._rig.unit_of_work() as uow:
            self.current = uow
            await callback(SESSION)
            await uow.commit()


def as_context(ctx: FakeContext) -> WorkContext:
    return cast(WorkContext, ctx)


def index_handler(rig: IndexingRig, ctx: FakeContext) -> IndexVersionWorkHandler:
    def transaction(session: AsyncSession) -> IndexingUnitOfWork:
        assert session is SESSION
        assert ctx.current is not None
        return ctx.current

    return IndexVersionWorkHandler(use_case=rig.complete, transaction=transaction)


def claimed(kind: str, payload: dict[str, str]) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind=kind,
        payload=payload,
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


def index_item(command: RecordExtraction, index_id: UUID) -> ClaimedWorkItem:
    return claimed(
        "index_version",
        {
            "document_id": str(command.document_id),
            "version_id": str(command.version_id),
            "index_id": str(index_id),
        },
    )


async def ready(rig: IndexingRig) -> tuple[RecordExtraction, UUID]:
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    return command, index_id


# --- déclaration --------------------------------------------------------------------


def test_the_handlers_serve_their_kinds_on_the_ingestion_queue() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    indexing = index_handler(rig, ctx)
    syncing = SyncVisibilityWorkHandler(use_case=rig.sync)

    registry = HandlerRegistry([indexing, syncing])

    assert registry.kinds_by_queue() == {
        "knowledge_base.ingestion": frozenset({"index_version", "sync_visibility"})
    }
    attempts = registry.max_attempts_by_kind()
    assert attempts["index_version"] >= 1
    assert attempts["sync_visibility"] >= 1
    assert isinstance(indexing, HandlesTerminalFailure)


# --- index_version ------------------------------------------------------------------


async def test_execute_finalizes_once_completing_and_publishing() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)

    await index_handler(rig, ctx).execute(
        index_item(command, index_id), as_context(ctx)
    )

    assert ctx.finalize_calls == 1
    assert rig.indexation(command, index_id).status is IndexationStatus.COMPLETED
    assert rig.status(command) is VersionStatus.PUBLISHED


async def test_a_manifest_divergence_is_a_transient_error_and_never_finalizes() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)
    rig.vector.after_upsert = drop_first

    with pytest.raises(TransientWorkError):
        await index_handler(rig, ctx).execute(
            index_item(command, index_id), as_context(ctx)
        )

    assert ctx.finalize_calls == 0
    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING


async def test_a_collection_mismatch_is_a_terminal_error_and_never_finalizes() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)
    rig.vector.conforming = False

    with pytest.raises(TerminalWorkError) as raised:
        await index_handler(rig, ctx).execute(
            index_item(command, index_id), as_context(ctx)
        )

    assert raised.value.code == "COLLECTION_MISMATCH"
    assert ctx.finalize_calls == 0


async def test_execute_still_finalizes_when_there_is_nothing_to_do() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)
    await rig.import_next_version(command.document_id, qa_sheet(1, tag="v2-"))

    await index_handler(rig, ctx).execute(
        index_item(command, index_id), as_context(ctx)
    )

    assert ctx.finalize_calls == 1
    assert rig.indexation(command, index_id).status is not IndexationStatus.COMPLETED
    assert rig.vector.points(index_id) == {}


async def test_an_unexpected_embedding_error_is_not_swallowed() -> None:
    rig = make_indexing_rig(embedder=BrokenEmbedder())
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)

    with pytest.raises(RuntimeError, match="modèle"):
        await index_handler(rig, ctx).execute(
            index_item(command, index_id), as_context(ctx)
        )

    assert ctx.finalize_calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_id": str(UUID(int=1)), "version_id": str(UUID(int=2))},
        {"document_id": "x", "version_id": "y", "index_id": "z"},
    ],
    ids=["empty", "no-index", "not-uuids"],
)
async def test_a_malformed_index_payload_is_a_terminal_error(
    payload: dict[str, str],
) -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)

    with pytest.raises(TerminalWorkError) as raised:
        await index_handler(rig, ctx).execute(
            claimed("index_version", payload), as_context(ctx)
        )

    assert raised.value.code == "INVALID_PAYLOAD"
    assert ctx.finalize_calls == 0


async def fail_with(
    rig: IndexingRig, item: ClaimedWorkItem, error: FailureInfo
) -> None:
    ctx = FakeContext(rig)
    handler = index_handler(rig, ctx)
    async with rig.unit_of_work() as uow:
        ctx.current = uow
        await handler.on_terminal_failure(SESSION, item, error)
        await uow.commit()


async def test_the_failure_hook_marks_the_indexation_failed_with_the_item_error() -> (
    None
):
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await fail_with(
        rig, index_item(command, index_id), FailureInfo("COLLECTION_MISMATCH", "dim")
    )

    indexation = rig.indexation(command, index_id)
    assert indexation.status is IndexationStatus.FAILED
    assert indexation.failure is not None
    assert (indexation.failure.code, indexation.failure.message) == (
        "COLLECTION_MISMATCH",
        "dim",
    )
    assert indexation.failure.retryable is True
    assert rig.status(command) is VersionStatus.EXTRACTED  # ses chunks restent valides


async def test_the_failure_hook_ignores_a_malformed_payload() -> None:
    rig = make_indexing_rig()

    await fail_with(rig, claimed("index_version", {}), FailureInfo("X", "y"))

    assert rig.indexing.indexations == {}


# --- sync_visibility ----------------------------------------------------------------


async def test_the_sync_handler_converges_then_finalizes_once() -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)
    command, index_id = await ready(rig)
    await rig.index(command, index_id)
    handler = SyncVisibilityWorkHandler(use_case=rig.sync)
    item = claimed(
        "sync_visibility",
        {"document_id": str(command.document_id), "visibility_revision": "1"},
    )

    await handler.execute(item, as_context(ctx))

    assert ctx.finalize_calls == 1
    assert all(p.visible for p in rig.points_of(command, index_id))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_id": str(UUID(int=1))},
        {"document_id": str(UUID(int=1)), "visibility_revision": "abc"},
        {"document_id": "x", "visibility_revision": "1"},
    ],
    ids=["empty", "no-revision", "revision-not-int", "not-uuid"],
)
async def test_a_malformed_sync_payload_is_a_terminal_error(
    payload: dict[str, str],
) -> None:
    rig = make_indexing_rig()
    ctx = FakeContext(rig)

    with pytest.raises(TerminalWorkError) as raised:
        await SyncVisibilityWorkHandler(use_case=rig.sync).execute(
            claimed("sync_visibility", payload), as_context(ctx)
        )

    assert raised.value.code == "INVALID_PAYLOAD"
    assert ctx.finalize_calls == 0


# --- composition de production ------------------------------------------------------


def test_the_production_builders_need_no_connection() -> None:
    factory = cast("async_sessionmaker[AsyncSession]", object())
    vector = make_indexing_rig().vector

    indexing = build_index_version_handler(
        factory,
        KnowledgeBaseSettings(_env_file=None),
        embedder=HashEmbedder(),
        vector_index=vector,
    )
    syncing = build_sync_visibility_handler(factory, vector_index=vector)

    assert (indexing.kind, indexing.queue) == (
        "index_version",
        "knowledge_base.ingestion",
    )
    assert (syncing.kind, syncing.queue) == (
        "sync_visibility",
        "knowledge_base.ingestion",
    )

"""Handler de work item ``extract_version`` : pont entre la work queue de S0 et
le use case ``RecordExtraction`` (S3 ; S0 §5).

API figée (``knowledge_base.adapters.controller.tasks.extract_version_handler``) :

- ``ExtractVersionWorkHandler(use_case: RecordExtractionHandler, transaction:
  Callable[[AsyncSession], ExtractionTransaction])`` ; c'est un ``WorkHandler``
  (``kind = "extract_version"``, ``queue = "knowledge_base.ingestion"``,
  ``max_attempts``) avec le hook facultatif ``on_terminal_failure``.
  ``transaction(session)`` donne l'accès aux dépôts sur la session de la
  transaction de finalisation (ou d'échec) fournie par S0.
- ``build_extract_version_handler(session_factory, settings, *, parser=None,
  token_counter=None, policy=None) -> ExtractVersionWorkHandler`` : composition
  de production (Units of Work SQLAlchemy de S2, lecteur de blobs sur
  ``settings.blob_root``, ``RoutingDocumentParser`` Docling/TXT, compteur
  provisoire, ``ChunkingPolicy`` par défaut) ; chaque défaut est remplaçable ;
  ne se connecte à rien et n'importe pas Docling à la construction.
- ``execute(item, ctx)`` : payload ``document_id`` + ``version_id`` ->
  ``use_case.prepare`` (hors transaction) ; ``DocumentParseError`` ->
  ``TerminalWorkError(code, message)`` (pas de retry), ``ChunkBudgetError`` ->
  ``TerminalWorkError("CHUNKING_FAILED")`` ; sinon UN appel à
  ``ctx.finalize`` dont le callback fait ``use_case.apply`` (même quand
  ``prepare`` n'a rien à faire : l'item doit se terminer). Payload illisible :
  ``TerminalWorkError("INVALID_PAYLOAD")``.
- ``on_terminal_failure(session, item, error: FailureInfo)`` : ``use_case.fail``
  avec ``FailureReason(error.code, error.message, retryable)`` ; ``retryable``
  est celui du catalogue (S8) : faux pour les codes de contenu d'une
  ``DocumentParseError`` (``EMPTY_DOCUMENT``, ``INVALID_DOCUMENT``,
  ``DOCUMENT_TOO_COMPLEX``, ``OCR_REQUIRED``) et pour ``CHUNKING_FAILED``,
  vrai pour ``TOO_MANY_PAGES`` (borne de configuration, revue S8 I1).
  Payload illisible : ne fait rien et ne lève pas.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.controller.tasks.registry import (
    HandlerRegistry,
    HandlesTerminalFailure,
)
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    ParseErrorCode,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    ExtractionRig,
    ExtractionUnitOfWork,
    make_rig,
)
from tests.unit.knowledge_base.use_cases.fakes import stream

SESSION = cast(AsyncSession, object())
COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"


class FakeContext:
    """Rejoue ``WorkContext.finalize`` : callback dans une transaction que l'on
    valide, comme S0 (une seule fois, sur succès du callback)."""

    def __init__(self, rig: ExtractionRig) -> None:
        self._rig = rig
        self.current: ExtractionUnitOfWork | None = None
        self.finalize_calls = 0
        self.finalized = False

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1
        async with self._rig.unit_of_work() as uow:
            self.current = uow
            await callback(SESSION)
            await uow.commit()
        self.finalized = True


def make_handler(rig: ExtractionRig, ctx: FakeContext) -> ExtractVersionWorkHandler:
    def transaction(session: AsyncSession) -> ExtractionUnitOfWork:
        assert session is SESSION
        assert ctx.current is not None
        return ctx.current

    return ExtractVersionWorkHandler(use_case=rig.handler, transaction=transaction)


def claimed(command: RecordExtraction | None, **payload: str) -> ClaimedWorkItem:
    body = (
        {
            "document_id": str(command.document_id),
            "version_id": str(command.version_id),
        }
        if command is not None
        else payload
    )
    return ClaimedWorkItem(
        id=uuid4(),
        kind="extract_version",
        payload=body,
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


def as_context(ctx: FakeContext) -> WorkContext:
    return cast(WorkContext, ctx)


def status(rig: ExtractionRig, command: RecordExtraction) -> VersionStatus:
    document = rig.harness.state.documents[command.document_id]
    return document.get_version(command.version_id).status


# --- déclaration --------------------------------------------------------------------


def test_the_handler_serves_extract_version_on_the_ingestion_queue() -> None:
    rig = make_rig()
    handler = make_handler(rig, FakeContext(rig))

    registry = HandlerRegistry([handler])

    assert handler.kind == "extract_version"
    assert registry.kinds_by_queue() == {
        "knowledge_base.ingestion": frozenset({"extract_version"})
    }
    assert registry.max_attempts_by_kind()["extract_version"] >= 1
    assert isinstance(handler, HandlesTerminalFailure)


# --- execute ------------------------------------------------------------------------


async def test_execute_finalizes_once_with_units_chunks_and_extracted_status() -> None:
    rig = make_rig()
    ctx = FakeContext(rig)
    command = await rig.import_document(COURSE)

    await make_handler(rig, ctx).execute(claimed(command), as_context(ctx))

    assert ctx.finalize_calls == 1
    assert status(rig, command) is VersionStatus.EXTRACTED
    (batch,) = rig.store.batches
    assert batch.version_id == command.version_id
    assert batch.units and batch.chunks


async def test_execute_still_finalizes_when_the_version_is_gone() -> None:
    rig = make_rig()
    ctx = FakeContext(rig)
    command = await rig.import_document(COURSE)
    await rig.harness.import_version.handle(
        ImportDocumentVersion(
            document_id=command.document_id,
            filename="cours.md",
            media_type="text/markdown",
            content=stream(b"# Cours asyncio\n\nV2.\n"),
        )
    )

    await make_handler(rig, ctx).execute(claimed(command), as_context(ctx))

    assert ctx.finalized
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.DISCARDED


async def test_an_empty_document_is_a_terminal_error_and_never_finalizes() -> None:
    rig = make_rig()
    ctx = FakeContext(rig)
    command = await rig.import_document(b"\n \n")

    with pytest.raises(TerminalWorkError) as raised:
        await make_handler(rig, ctx).execute(claimed(command), as_context(ctx))

    assert raised.value.code == EMPTY_DOCUMENT
    assert ctx.finalize_calls == 0
    assert rig.store.batches == []


async def test_an_unreadable_file_is_a_terminal_invalid_document() -> None:
    rig = make_rig()
    ctx = FakeContext(rig)
    command = await rig.import_document(b"# Caf\xe9\n")

    with pytest.raises(TerminalWorkError) as raised:
        await make_handler(rig, ctx).execute(claimed(command), as_context(ctx))

    assert raised.value.code == INVALID_DOCUMENT


async def test_a_budget_without_room_for_content_is_a_terminal_chunking_failure() -> (
    None
):
    # Deux tokens : l'en-tête tronqué (« … ») et le marqueur ne laissent
    # aucune place au contenu ; le refaire échouerait pareillement.
    rig = make_rig(max_tokens=2)
    ctx = FakeContext(rig)
    command = await rig.import_document(COURSE)

    with pytest.raises(TerminalWorkError) as raised:
        await make_handler(rig, ctx).execute(claimed(command), as_context(ctx))

    assert raised.value.code == "CHUNKING_FAILED"
    assert ctx.finalize_calls == 0
    assert rig.store.batches == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"document_id": str(UUID(int=1))}, {"document_id": "x", "version_id": "y"}],
    ids=["empty", "no-version", "not-uuids"],
)
async def test_a_malformed_payload_is_a_terminal_error(payload: dict[str, str]) -> None:
    rig = make_rig()
    ctx = FakeContext(rig)

    with pytest.raises(TerminalWorkError) as raised:
        await make_handler(rig, ctx).execute(claimed(None, **payload), as_context(ctx))

    assert raised.value.code == "INVALID_PAYLOAD"
    assert ctx.finalize_calls == 0


# --- on_terminal_failure ------------------------------------------------------------


async def fail_with(
    rig: ExtractionRig, item: ClaimedWorkItem, error: FailureInfo
) -> None:
    ctx = FakeContext(rig)
    handler = make_handler(rig, ctx)
    async with rig.unit_of_work() as uow:
        ctx.current = uow
        await handler.on_terminal_failure(SESSION, item, error)
        await uow.commit()


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (EMPTY_DOCUMENT, False),
        (INVALID_DOCUMENT, False),
        (DOCUMENT_TOO_COMPLEX, False),
        ("CHUNKING_FAILED", False),
        ("TRANSIENT", True),
        ("UNEXPECTED_ERROR", True),
    ],
)
async def test_the_failure_hook_marks_the_version_failed_with_the_item_error(
    code: str, retryable: bool
) -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await fail_with(rig, claimed(command), FailureInfo(code, "diagnostic"))

    document = rig.harness.state.documents[command.document_id]
    version = document.get_version(command.version_id)
    assert version.status is VersionStatus.FAILED
    assert version.failure is not None
    assert (version.failure.code, version.failure.message) == (code, "diagnostic")
    assert version.failure.retryable is retryable


# TOO_MANY_PAGES dépend de la borne de pages configurée, pas du seul contenu :
# rejouable une fois la borne relevée (revue S8, I1 ; O2 interdit le réimport).
CONTENT_PARSE_ERRORS = [
    c for c in ParseErrorCode if c is not ParseErrorCode.TOO_MANY_PAGES
]


@pytest.mark.parametrize("code", CONTENT_PARSE_ERRORS)
async def test_every_content_parse_error_code_fails_the_version_as_not_retryable(
    code: ParseErrorCode,
) -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await fail_with(rig, claimed(command), FailureInfo(code.value, "diagnostic"))

    document = rig.harness.state.documents[command.document_id]
    failure = document.get_version(command.version_id).failure
    assert failure is not None
    assert failure.retryable is False


async def test_a_page_limit_failure_is_replayable_once_the_limit_is_raised() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await fail_with(
        rig,
        claimed(command),
        FailureInfo(ParseErrorCode.TOO_MANY_PAGES.value, "301 pages"),
    )

    document = rig.harness.state.documents[command.document_id]
    failure = document.get_version(command.version_id).failure
    assert failure is not None
    assert failure.retryable is True


async def test_the_failure_hook_ignores_a_malformed_payload() -> None:
    rig = make_rig()

    await fail_with(rig, claimed(None), FailureInfo(EMPTY_DOCUMENT, "x"))

    assert rig.store.batches == []


# --- composition de production -----------------------------------------------------


def test_the_production_builder_needs_no_connection_and_serves_extract_version() -> (
    None
):
    from knowledge_base import KnowledgeBaseSettings
    from knowledge_base.adapters.controller.tasks.extract_version_handler import (
        build_extract_version_handler,
    )

    handler = build_extract_version_handler(
        cast("async_sessionmaker[AsyncSession]", object()),
        KnowledgeBaseSettings(_env_file=None),
    )

    assert handler.kind == "extract_version"
    assert handler.queue == "knowledge_base.ingestion"

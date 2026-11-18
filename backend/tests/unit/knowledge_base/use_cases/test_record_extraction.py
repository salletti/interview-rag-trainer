"""``RecordExtraction`` : lecture, parsing, segmentation, chunking HORS
transaction (``prepare``), puis écriture dans la transaction de finalisation
(``apply``) ou de l'échec (``fail``) (S3 ; knowledge-base.md §6, §7.3, I7).

API figée (``knowledge_base.use_cases.record_extraction``) :

- ``record_extraction_command.RecordExtraction(document_id, version_id)``
  (le payload de l'item ``extract_version``).
- ``record_extraction_handler.RecordExtractionHandler(*, unit_of_work, blob_reader,
  parser, token_counter, policy)`` ; ``unit_of_work`` est une fabrique de Units
  of Work (lecture seule, sans ``commit``).
- ``await prepare(command) -> PreparedExtraction | None`` : lit le document
  (version ``RECEIVED``, document ``ACTIVE``), lit le blob, parse, segmente
  selon ``document.kind``, planifie les chunks ; n'écrit RIEN et ne valide
  jamais de transaction. ``None`` (aucun travail) si le document ou la version
  est inconnu, la version n'est plus ``RECEIVED`` ou le document est retiré.
  ``PreparedExtraction`` expose ``document_id``, ``version_id``,
  ``policy_version``, ``units``, ``chunks``. Une ``DocumentParseError`` du
  parseur remonte telle quelle.
- ``await apply(transaction, prepared) -> bool`` : DANS la transaction de
  finalisation, relit l'agrégat ; si la version est toujours ``RECEIVED`` et le
  document ``ACTIVE`` : ``record_extraction`` + ``save`` + lot
  ``(version_id, policy_version)`` ; vrai. Sinon n'écrit rien ; faux. N'ouvre
  aucune Unit of Work et ne fait jamais de ``commit`` (la transaction
  appartient à ``ctx.finalize``).
- ``await fail(transaction, command, reason: FailureReason)`` : version
  ``RECEIVED`` -> ``FAILED`` avec la raison ; sans effet ni erreur si la
  version n'est plus ``RECEIVED`` (supplantée entre-temps : un hook qui lève
  ferait boucler l'item, O14).
- Choix S3 : une version supplantée ou un document retiré entre le claim et la
  finalisation est IGNORÉ (l'item se termine ``SUCCEEDED`` sans écriture), pas
  une erreur terminale. Aucune ``Indexation`` en S3 (S4).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.domain.services.support import POLICY_VERSION
from tests.unit.knowledge_base.use_cases.extraction_fakes import ExtractionRig, make_rig
from tests.unit.knowledge_base.use_cases.fakes import stream

QA_SHEET = (
    b"# Fiche FastAPI\n\n"
    b"## Question : Qu'est-ce que Depends ?\n\n"
    b"Depends injecte une dependance.\n\n"
    b"```python\ndef f(db=Depends(get_db)): ...\n```\n\n"
    b"## Question : Qu'est-ce que lifespan ?\n\n"
    b"Il gere le cycle de vie.\n"
)
COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"


def status(rig: ExtractionRig, command: RecordExtraction) -> VersionStatus:
    document = rig.harness.state.documents[command.document_id]
    return document.get_version(command.version_id).status


async def finalize(
    rig: ExtractionRig, command: RecordExtraction, *, commit: bool = True
) -> bool:
    prepared = await rig.handler.prepare(command)
    assert prepared is not None
    async with rig.unit_of_work() as uow:
        applied = await rig.handler.apply(uow, prepared)
        if commit:
            await uow.commit()
    return applied


# --- prepare : tout le travail, aucune écriture ---------------------------------------


async def test_prepare_segments_by_the_document_kind_and_cites_the_source() -> None:
    rig = make_rig()
    command = await rig.import_document(QA_SHEET, kind=DocumentKind.QA_SHEET)

    prepared = await rig.handler.prepare(command)

    assert prepared is not None
    assert (prepared.document_id, prepared.version_id) == (
        command.document_id,
        command.version_id,
    )
    assert prepared.policy_version == POLICY_VERSION
    assert [(u.unit_type, u.question_text) for u in prepared.units] == [
        (UnitType.QA, "Qu'est-ce que Depends ?"),
        (UnitType.QA, "Qu'est-ce que lifespan ?"),
    ]
    assert len(prepared.chunks) == 2
    for chunk in prepared.chunks:
        assert chunk.citation.document_id == command.document_id
        assert chunk.citation.version_id == command.version_id
        assert chunk.citation.version_number == 1
        assert chunk.citation.document_title == "Python avancé"
        assert chunk.citation.source_filename == "cours.md"
        assert chunk.contextual_text.startswith("Python avancé\n")


async def test_prepare_uses_the_declared_kind_not_the_question_headings() -> None:
    rig = make_rig()
    command = await rig.import_document(QA_SHEET, kind=DocumentKind.COURSE)

    prepared = await rig.handler.prepare(command)

    assert prepared is not None
    assert {u.unit_type for u in prepared.units} == {UnitType.SECTION}


async def test_prepare_writes_nothing_and_never_commits() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await rig.handler.prepare(command)

    assert rig.store.batches == []
    assert rig.store.commits == 0
    assert status(rig, command) is VersionStatus.RECEIVED


async def test_prepare_twice_gives_identical_units_chunks_and_identifiers() -> None:
    rig = make_rig()
    command = await rig.import_document(QA_SHEET, kind=DocumentKind.QA_SHEET)

    first = await rig.handler.prepare(command)
    second = await rig.handler.prepare(command)

    assert first is not None
    assert first == second


async def test_prepare_reads_the_blob_of_the_version_once() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await rig.handler.prepare(command)

    document = rig.harness.state.documents[command.document_id]
    digest = document.get_version(command.version_id).source.content_hash.value
    assert rig.blobs.reads == [digest]
    assert rig.parser.calls == 1


async def test_prepare_cites_the_filename_of_the_imported_version() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE, filename="asyncio.md")

    prepared = await rig.handler.prepare(command)

    assert prepared is not None
    assert prepared.chunks[0].citation.source_filename == "asyncio.md"


# --- prepare : rien à faire -----------------------------------------------------------


async def test_prepare_returns_nothing_for_a_version_that_is_no_longer_received() -> (
    None
):
    rig = make_rig()
    command = await rig.import_document(COURSE)
    await rig.harness.import_version.handle(
        ImportDocumentVersion(
            document_id=command.document_id,
            filename="cours.md",
            media_type="text/markdown",
            content=stream(b"# Cours asyncio\n\nUne autre version.\n"),
        )
    )
    assert status(rig, command) is VersionStatus.DISCARDED
    rig.blobs.reads.clear()

    assert await rig.handler.prepare(command) is None
    assert rig.blobs.reads == []
    assert rig.parser.calls == 0


async def test_prepare_returns_nothing_for_an_unknown_version() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    unknown = RecordExtraction(
        document_id=command.document_id,
        version_id=UUID(int=424242),
    )

    assert await rig.handler.prepare(unknown) is None


async def test_prepare_returns_nothing_for_a_withdrawn_document() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    await withdraw(rig, command)

    assert await rig.handler.prepare(command) is None
    assert rig.parser.calls == 0


async def withdraw(rig: ExtractionRig, command: RecordExtraction) -> None:
    async with rig.unit_of_work() as uow:
        document = await uow.documents.get(command.document_id)
        assert isinstance(document, KnowledgeDocument)
        document.withdraw()
        await uow.documents.save(document)
        await uow.commit()


# --- erreurs de parsing ---------------------------------------------------------------


async def test_an_empty_document_raises_the_parse_error_and_writes_nothing() -> None:
    rig = make_rig()
    command = await rig.import_document(b"\n  \n")

    with pytest.raises(DocumentParseError) as raised:
        await rig.handler.prepare(command)

    assert raised.value.code == EMPTY_DOCUMENT
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.RECEIVED


# --- apply : la transaction de finalisation ------------------------------------------


async def test_apply_writes_units_chunks_and_the_extracted_status_together() -> None:
    rig = make_rig()
    command = await rig.import_document(QA_SHEET, kind=DocumentKind.QA_SHEET)
    prepared = await rig.handler.prepare(command)
    assert prepared is not None

    applied = await finalize(rig, command)

    assert applied is True
    assert status(rig, command) is VersionStatus.EXTRACTED
    (batch,) = rig.store.batches
    assert batch.version_id == command.version_id
    assert batch.policy_version == POLICY_VERSION
    assert batch.units == prepared.units
    assert batch.chunks == prepared.chunks
    assert {c.unit_id for c in batch.chunks} == {u.unit_id for u in batch.units}


async def test_apply_makes_nothing_visible_until_the_caller_commits() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    applied = await finalize(rig, command, commit=False)

    assert applied is True
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.RECEIVED
    assert rig.store.commits == 0


async def test_apply_works_inside_the_transaction_it_receives_only() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    prepared = await rig.handler.prepare(command)
    assert prepared is not None
    opened_before = rig.store.opened

    async with rig.unit_of_work() as uow:
        await rig.handler.apply(uow, prepared)

    assert rig.store.opened == opened_before + 1  # la nôtre, aucune autre


async def test_apply_ignores_a_version_superseded_since_prepare() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    prepared = await rig.handler.prepare(command)
    assert prepared is not None
    await rig.harness.import_version.handle(
        ImportDocumentVersion(
            document_id=command.document_id,
            filename="cours.md",
            media_type="text/markdown",
            content=stream(b"# Cours asyncio\n\nUne nouvelle version.\n"),
        )
    )

    async with rig.unit_of_work() as uow:
        applied = await rig.handler.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.DISCARDED


async def test_apply_ignores_a_document_withdrawn_since_prepare() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    prepared = await rig.handler.prepare(command)
    assert prepared is not None
    await withdraw(rig, command)

    async with rig.unit_of_work() as uow:
        applied = await rig.handler.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.store.batches == []
    assert status(rig, command) is VersionStatus.RECEIVED


async def test_applying_twice_writes_a_single_batch() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    prepared = await rig.handler.prepare(command)
    assert prepared is not None

    for _ in range(2):
        async with rig.unit_of_work() as uow:
            await rig.handler.apply(uow, prepared)
            await uow.commit()

    assert len(rig.store.batches) == 1
    assert status(rig, command) is VersionStatus.EXTRACTED


# --- fail : le hook d'échec terminal --------------------------------------------------


REASON = FailureReason(code="EMPTY_DOCUMENT", message="aucun contenu", retryable=False)


async def test_fail_marks_the_received_version_failed_with_its_reason() -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    async with rig.unit_of_work() as uow:
        await rig.handler.fail(uow, command, REASON)
        await uow.commit()

    document = rig.harness.state.documents[command.document_id]
    version = document.get_version(command.version_id)
    assert version.status is VersionStatus.FAILED
    assert version.failure == REASON
    assert rig.store.batches == []


@pytest.mark.parametrize("target", ["discard", "extract"])
async def test_fail_is_a_no_op_on_a_version_that_moved_on(target: str) -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)
    if target == "extract":
        assert await finalize(rig, command)
    else:
        await rig.harness.import_version.handle(
            ImportDocumentVersion(
                document_id=command.document_id,
                filename="cours.md",
                media_type="text/markdown",
                content=stream(b"# Cours asyncio\n\nV2.\n"),
            )
        )
    before = status(rig, command)

    async with rig.unit_of_work() as uow:
        await rig.handler.fail(uow, command, REASON)
        await uow.commit()

    assert status(rig, command) is before
    assert before in {VersionStatus.EXTRACTED, VersionStatus.DISCARDED}

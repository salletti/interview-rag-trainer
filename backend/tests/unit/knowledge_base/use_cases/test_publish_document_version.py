"""``PublishDocumentVersion`` : publication gardée par I5 et I9 (S4 ;
knowledge-base.md §8, §10, §13).

API figée (``knowledge_base.use_cases.publish_document_version``) :

- ``publish_document_version_command.PublishDocumentVersion(document_id,
  version_id)``.
- ``publish_document_version_handler.PublishDocumentVersionHandler(*,
  unit_of_work, clock)`` :
  - ``await handle(command)`` : ouvre sa transaction, publie, ``commit`` ;
    sans ``commit`` si la publication est refusée.
  - ``await apply(transaction, command)`` : même logique DANS la transaction
    fournie (celle de la finalisation de ``CompleteIndexation``), sans jamais
    de ``commit``.
  - Gardes : l'index actif est lu avec ``corpus.get_shared()`` (``FOR SHARE``,
    jamais ``get_for_update``) ; I9 : l'``Indexation`` de la version dans CET
    index doit être ``COMPLETED`` ; puis ``KnowledgeDocument.publish`` (version
    ``EXTRACTED``, document ``ACTIVE``, I5). Succès : enqueue
    ``sync_visibility`` (dedup ``sync_visibility:<document_id>:<revision>``,
    payload ``document_id`` et ``visibility_revision`` en texte).
- ``errors`` : ``PublicationRefusedError`` (base), ``NoActiveIndexError`` (corpus
  absent ou sans index actif), ``IndexationNotCompletedError`` (indexation
  absente ou non ``COMPLETED`` dans l'index actif). Les refus de l'agrégat
  (``KnowledgeDocumentError``) remontent tels quels.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    InvalidVersionTransitionError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.publish_document_version.errors import (
    IndexationNotCompletedError,
    NoActiveIndexError,
    PublicationRefusedError,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_command import (  # noqa: E501
    PublishDocumentVersion,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    IndexingRig,
    make_indexing_rig,
    qa_sheet,
)

BUILDING = UUID(int=555)


def publish_command(command: RecordExtraction) -> PublishDocumentVersion:
    return PublishDocumentVersion(
        document_id=command.document_id, version_id=command.version_id
    )


async def extracted(rig: IndexingRig) -> tuple[RecordExtraction, UUID]:
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    return command, index_id


# --- I9 : indexation COMPLETED dans l'index actif -------------------------------------


async def test_publication_is_refused_while_the_active_indexation_is_pending() -> None:
    rig = make_indexing_rig()
    command, _ = await extracted(rig)

    with pytest.raises(IndexationNotCompletedError):
        await rig.publish.handle(publish_command(command))

    assert rig.status(command) is VersionStatus.EXTRACTED
    assert rig.document(command.document_id).published_version_id is None
    assert rig.sync_keys() == []


async def test_publication_is_refused_after_a_failed_indexation() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.indexation(command, index_id).fail(FailureReason("X", "y", retryable=True))

    with pytest.raises(IndexationNotCompletedError):
        await rig.publish.handle(publish_command(command))

    assert rig.status(command) is VersionStatus.EXTRACTED


async def test_publication_is_refused_when_only_a_building_index_is_completed() -> None:
    rig = make_indexing_rig()
    await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    rig.mark_completed(command, BUILDING)

    with pytest.raises(IndexationNotCompletedError):
        await rig.publish.handle(publish_command(command))

    assert rig.status(command) is VersionStatus.EXTRACTED


async def test_publication_is_refused_without_any_indexation() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    del rig.indexing.indexations[(command.version_id, index_id)]

    with pytest.raises(IndexationNotCompletedError):
        await rig.publish.handle(publish_command(command))


async def test_publication_is_refused_when_the_corpus_has_no_active_index() -> None:
    rig = make_indexing_rig()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)  # pas de corpus du tout

    with pytest.raises(NoActiveIndexError):
        await rig.publish.handle(publish_command(command))

    assert rig.status(command) is VersionStatus.EXTRACTED


def test_both_refusals_are_publication_refusals() -> None:
    assert issubclass(NoActiveIndexError, PublicationRefusedError)
    assert issubclass(IndexationNotCompletedError, PublicationRefusedError)


# --- succès ---------------------------------------------------------------------------


async def test_a_completed_indexation_in_the_active_index_publishes_the_version() -> (
    None
):
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)

    await rig.publish.handle(publish_command(command))

    document = rig.document(command.document_id)
    assert rig.status(command) is VersionStatus.PUBLISHED
    assert document.published_version_id == command.version_id
    assert document.visibility_revision == 1


async def test_publication_schedules_the_visibility_synchronization() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)

    await rig.publish.handle(publish_command(command))

    key = f"sync_visibility:{command.document_id}:1"
    assert rig.sync_keys() == [key]
    item = rig.harness.state.items[key]
    assert item.kind == "sync_visibility"
    assert item.payload == {
        "document_id": str(command.document_id),
        "visibility_revision": "1",
    }


async def test_the_active_index_is_read_under_a_shared_lock_only() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)
    rig.indexing.locks.clear()

    await rig.publish.handle(publish_command(command))

    assert rig.indexing.locks == ["shared"]


async def test_publishing_a_newer_version_supersedes_the_published_one() -> None:
    rig = make_indexing_rig()
    first, index_id = await extracted(rig)
    rig.mark_completed(first, index_id)
    await rig.publish.handle(publish_command(first))
    second = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n2-"))
    await rig.extract(second)
    rig.mark_completed(second, index_id)

    await rig.publish.handle(publish_command(second))

    assert rig.status(first) is VersionStatus.SUPERSEDED
    assert rig.status(second) is VersionStatus.PUBLISHED
    assert rig.document(first.document_id).visibility_revision == 2
    assert rig.sync_keys() == [
        f"sync_visibility:{first.document_id}:1",
        f"sync_visibility:{first.document_id}:2",
    ]


# --- refus de l'agrégat ---------------------------------------------------------------


async def test_a_discarded_version_is_not_published_even_if_indexed() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)
    await rig.import_next_version(command.document_id, qa_sheet(1, tag="n2-"))

    with pytest.raises(InvalidVersionTransitionError):
        await rig.publish.handle(publish_command(command))

    assert rig.status(command) is VersionStatus.DISCARDED
    assert rig.sync_keys() == []


async def test_a_withdrawn_document_is_not_published() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)
    rig.document(command.document_id).withdraw()

    with pytest.raises(DocumentWithdrawnError):
        await rig.publish.handle(publish_command(command))

    document = rig.document(command.document_id)
    assert document.status is DocumentStatus.WITHDRAWN
    assert document.published_version_id is None


# --- transaction ----------------------------------------------------------------------


async def test_apply_writes_inside_the_given_transaction_and_never_commits() -> None:
    rig = make_indexing_rig()
    command, index_id = await extracted(rig)
    rig.mark_completed(command, index_id)

    async with rig.unit_of_work() as uow:
        await rig.publish.apply(uow, publish_command(command))

    assert rig.status(command) is VersionStatus.EXTRACTED
    assert rig.sync_keys() == []


async def test_a_refused_handle_commits_nothing() -> None:
    rig = make_indexing_rig()
    command, _ = await extracted(rig)
    commits = rig.store.commits

    with pytest.raises(PublicationRefusedError):
        await rig.publish.handle(publish_command(command))

    assert rig.store.commits == commits

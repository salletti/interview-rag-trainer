"""Import de la version N+1 : annulation du travail d'indexation de N (S7 ;
knowledge-base.md §4.1 I6, §5.3, §8 point 1, §10 ligne « Abandon »).

Report de S4 (knowledge-base-progress.md) : « l'import de N+1 n'annule pas les
indexations ni les items ``index_version`` de N ». Contrat attendu de
``ImportDocumentVersionHandler`` (sa Unit of Work expose ``corpus`` et
``indexations``, comme celle de production) : dans LA transaction de l'import,
pour chaque version passée ``DISCARDED`` par cet import, et pour chaque index
vivant du corpus (``corpus.get_shared().live_indexes()``) :

- l'item ``index_version:<version_id>:<index_id>`` est annulé
  (``work_queue.cancel``) ;
- son ``Indexation`` ``PENDING`` ou ``FAILED`` passe ``CANCELLED`` ; une
  ``COMPLETED`` n'est pas touchée (aucune transition n'existe) ; une version
  absente d'un index n'a pas d'indexation : rien à faire ;
- la version publiée, elle, n'est jamais touchée.

L'item ``extract_version`` de la version écartée est déjà annulé depuis S2 ; la
version N+1 garde son propre item. Aucun de ces tests n'importe de module
nouveau : ils échouent sur leur assertion tant que l'annulation n'existe pas.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.fakes import stream
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import S7Rig, make_s7_rig

BUILDING = UUID(int=555)


def index_key(command: RecordExtraction, index_id: UUID) -> str:
    return f"index_version:{command.version_id}:{index_id}"


async def extracted(rig: S7Rig, index_id: UUID) -> RecordExtraction:
    """Version 1 ``EXTRACTED`` : indexation ``PENDING`` et item ``index_version``
    ``PENDING`` dans chaque index vivant."""
    command = await rig.base.import_document(qa_sheet(2))
    await rig.base.extract(command)
    assert rig.item(index_key(command, index_id)) == "PENDING"
    return command


async def test_importing_the_next_version_cancels_the_pending_indexation() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)

    await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.base.status(first) is VersionStatus.DISCARDED
    assert rig.base.indexation(first, index_id).status is IndexationStatus.CANCELLED


@pytest.mark.parametrize("status", ["PENDING", "RUNNING"])
async def test_importing_the_next_version_cancels_the_index_version_item(
    status: str,
) -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)
    rig.base.harness.state.items[index_key(first, index_id)].status = status

    await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.item(index_key(first, index_id)) == "CANCELLED"


async def test_a_failed_indexation_of_a_discarded_version_is_cancelled_too() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)
    rig.base.indexation(first, index_id).fail(
        FailureReason(code="COLLECTION_MISMATCH", message="dim 17", retryable=True)
    )

    await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.base.indexation(first, index_id).status is IndexationStatus.CANCELLED


async def test_a_completed_indexation_of_a_discarded_version_is_left_as_is() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)
    rig.base.mark_completed(first, index_id)

    await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.base.status(first) is VersionStatus.DISCARDED
    assert rig.base.indexation(first, index_id).status is IndexationStatus.COMPLETED


async def test_the_work_of_every_live_index_is_cancelled() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    rig.base.add_building_index(BUILDING)
    first = await rig.base.import_document(qa_sheet(2))
    await rig.base.extract(first)

    await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    for index in (index_id, BUILDING):
        assert rig.base.indexation(first, index).status is IndexationStatus.CANCELLED
        assert rig.item(index_key(first, index)) == "CANCELLED"


async def test_the_published_version_keeps_its_indexation_and_its_work() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    published = await rig.published(index_id)
    assert rig.base.status(published) is VersionStatus.PUBLISHED

    await rig.import_next(published.document_id, qa_sheet(3, tag="b"))

    assert rig.base.status(published) is VersionStatus.PUBLISHED
    assert rig.base.indexation(published, index_id).status is (
        IndexationStatus.COMPLETED
    )
    assert index_key(published, index_id) not in rig.base.harness.state.cancel_calls


async def test_only_the_versions_discarded_by_this_import_are_cancelled() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)
    second = await rig.import_next(first.document_id, qa_sheet(3, tag="b"))
    await rig.base.extract(second)

    # Une troisième version : la première est déjà écartée et annulée, la
    # reprendre lèverait une transition d'indexation invalide.
    third = await rig.import_next(first.document_id, qa_sheet(4, tag="c"))

    assert rig.base.status(second) is VersionStatus.DISCARDED
    assert rig.base.indexation(second, index_id).status is IndexationStatus.CANCELLED
    assert rig.base.indexation(first, index_id).status is IndexationStatus.CANCELLED
    assert rig.item(f"extract_version:{third.version_id}") == "PENDING"


async def test_the_new_version_keeps_its_own_extraction_item() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)

    second = await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.item(f"extract_version:{second.version_id}") == "PENDING"


async def test_the_cancellation_belongs_to_the_import_transaction() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    first = await extracted(rig, index_id)
    rig.base.harness.state.fail_commit = True

    with pytest.raises(RuntimeError):
        await rig.import_version_handler().handle(
            ImportDocumentVersion(
                document_id=first.document_id,
                filename="fiche.md",
                media_type="text/markdown",
                content=stream(qa_sheet(3, tag="b")),
            )
        )

    assert rig.base.status(first) is VersionStatus.EXTRACTED
    assert rig.base.indexation(first, index_id).status is IndexationStatus.PENDING
    assert rig.item(index_key(first, index_id)) == "PENDING"


async def test_a_corpus_without_index_leaves_the_import_unchanged() -> None:
    rig = make_s7_rig()
    first = await rig.base.import_document(qa_sheet(2))

    second = await rig.import_next(first.document_id, qa_sheet(3, tag="b"))

    assert rig.base.status(first) is VersionStatus.DISCARDED
    assert rig.base.status(second) is VersionStatus.RECEIVED

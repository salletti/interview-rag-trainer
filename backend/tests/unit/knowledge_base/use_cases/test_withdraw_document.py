"""``WithdrawDocument`` : retrait logique immédiat, travail en cours annulé,
purge planifiée (S7 ; knowledge-base.md §5.1, §10 ligne « Retrait », I12).

API figée (``knowledge_base.use_cases.withdraw_document``) :

- ``withdraw_document_command.WithdrawDocument(document_id)``.
- ``withdraw_document_handler.WithdrawDocumentHandler(*, unit_of_work)`` ;
  ``await handle(command) -> withdraw_document_result.WithdrawDocumentResult``
  (``document_id``, ``visibility_revision`` : la nouvelle révision). La Unit of
  Work expose ``documents``, ``work_queue``, ``corpus`` et ``indexations``.
  Dans UNE transaction :
  1. ``document.withdraw()`` (``WITHDRAWN``, révision + 1) ; les états des
     versions ne changent pas (§5.2) ;
  2. travail en cours annulé : pour chaque version non terminale du document
     (``RECEIVED``, ``EXTRACTED``, ``FAILED``), item ``extract_version`` ; pour
     chaque version, et chaque index vivant, item ``index_version`` et
     indexation ``PENDING`` ou ``FAILED`` -> ``CANCELLED`` (une ``COMPLETED``
     reste telle quelle) ;
  3. item ``purge_document`` : dedup ``purge_document:<document_id>``, payload
     ``{"document_id": "<uuid>"}``.
  Un document déjà retiré lève ``DocumentWithdrawnError`` (domaine), un document
  inconnu ``DocumentNotFoundError`` (``use_cases.errors``) : rien n'est écrit.
  Un verrou optimiste perdu (``ConcurrentModificationError``) est rejoué UNE
  fois sur l'état à jour, comme l'import : un import concurrent qui a créé une
  version entre la lecture et l'écriture ne doit pas échapper à l'annulation ;
  une seconde perte remonte (couvert en intégration).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.exceptions.document_errors import DocumentWithdrawnError
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.errors import DocumentNotFoundError
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_command import (
    WithdrawDocument,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import S7Rig, make_s7_rig

BUILDING = UUID(int=555)


def purge_key(document_id: UUID) -> str:
    return f"purge_document:{document_id}"


def index_key(command: RecordExtraction, index_id: UUID) -> str:
    return f"index_version:{command.version_id}:{index_id}"


async def withdraw(rig: S7Rig, document_id: UUID) -> int:
    result = await rig.withdraw_handler().handle(
        WithdrawDocument(document_id=document_id)
    )
    assert result.document_id == document_id
    return int(result.visibility_revision)


async def test_withdrawing_marks_the_document_withdrawn_and_returns_the_revision() -> (
    None
):
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    published = await rig.published(index_id)
    before = rig.base.document(published.document_id).visibility_revision

    revision = await withdraw(rig, published.document_id)

    document = rig.base.document(published.document_id)
    assert document.status is DocumentStatus.WITHDRAWN
    assert document.visibility_revision == before + 1
    assert revision == document.visibility_revision


async def test_withdrawing_plans_one_purge_of_the_document() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())

    await withdraw(rig, command.document_id)

    assert rig.items_of_kind("purge_document") == [purge_key(command.document_id)]
    item = rig.base.harness.state.items[purge_key(command.document_id)]
    assert item.status == "PENDING"
    assert item.payload == {"document_id": str(command.document_id)}


async def test_withdrawing_does_not_change_the_state_of_the_versions() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    published = await rig.published(index_id)
    await rig.import_next(published.document_id, qa_sheet(3, tag="b"))
    before = rig.base.document(published.document_id).versions

    await withdraw(rig, published.document_id)

    document = rig.base.document(published.document_id)
    assert document.versions == before
    assert [v.status for v in document.versions] == [
        VersionStatus.PUBLISHED,
        VersionStatus.RECEIVED,
    ]
    assert document.published_version_id == published.version_id


async def test_withdrawing_cancels_the_extraction_in_progress() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    extraction = f"extract_version:{command.version_id}"
    assert rig.item(extraction) == "PENDING"

    await withdraw(rig, command.document_id)

    assert rig.item(extraction) == "CANCELLED"


async def test_withdrawing_cancels_the_indexation_in_progress() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    command = await rig.base.import_document(qa_sheet())
    await rig.base.extract(command)

    await withdraw(rig, command.document_id)

    assert rig.item(index_key(command, index_id)) == "CANCELLED"
    assert rig.base.indexation(command, index_id).status is IndexationStatus.CANCELLED


async def test_withdrawing_cancels_a_failed_indexation() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    command = await rig.base.import_document(qa_sheet())
    await rig.base.extract(command)
    rig.base.indexation(command, index_id).fail(
        FailureReason(code="COLLECTION_MISMATCH", message="dim 17", retryable=True)
    )

    await withdraw(rig, command.document_id)

    assert rig.base.indexation(command, index_id).status is IndexationStatus.CANCELLED


async def test_withdrawing_cancels_the_work_of_every_live_index() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    rig.base.add_building_index(BUILDING)
    command = await rig.base.import_document(qa_sheet())
    await rig.base.extract(command)

    await withdraw(rig, command.document_id)

    for index in (index_id, BUILDING):
        assert rig.item(index_key(command, index)) == "CANCELLED"
        assert rig.base.indexation(command, index).status is (
            IndexationStatus.CANCELLED
        )


async def test_withdrawing_leaves_a_completed_indexation_alone() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    published = await rig.published(index_id)

    await withdraw(rig, published.document_id)

    assert rig.base.indexation(published, index_id).status is (
        IndexationStatus.COMPLETED
    )


async def test_withdrawing_does_not_cancel_the_work_of_other_documents() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    mine = await rig.base.import_document(qa_sheet())
    other = await rig.base.import_document(qa_sheet(tag="autre"), title="Autre")
    await rig.base.extract(other)

    await withdraw(rig, mine.document_id)

    assert rig.item(f"extract_version:{other.version_id}") == "PENDING"
    assert rig.item(index_key(other, index_id)) == "PENDING"
    assert rig.base.indexation(other, index_id).status is IndexationStatus.PENDING
    assert rig.items_of_kind("purge_document") == [purge_key(mine.document_id)]


async def test_withdrawing_a_withdrawn_document_is_refused_and_plans_nothing_more() -> (
    None
):
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    revision = await withdraw(rig, command.document_id)

    with pytest.raises(DocumentWithdrawnError):
        await withdraw(rig, command.document_id)

    document = rig.base.document(command.document_id)
    assert document.visibility_revision == revision
    assert rig.items_of_kind("purge_document") == [purge_key(command.document_id)]


async def test_withdrawing_an_unknown_document_is_refused_and_writes_nothing() -> None:
    rig = make_s7_rig()
    await rig.base.import_document(qa_sheet())
    before = set(rig.base.harness.state.items)

    with pytest.raises(DocumentNotFoundError):
        await withdraw(rig, uuid4())

    assert set(rig.base.harness.state.items) == before


async def test_the_withdrawal_is_one_transaction() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    command = await rig.base.import_document(qa_sheet())
    await rig.base.extract(command)
    rig.base.harness.state.fail_commit = True

    with pytest.raises(RuntimeError):
        await withdraw(rig, command.document_id)

    document = rig.base.document(command.document_id)
    assert document.status is DocumentStatus.ACTIVE
    assert rig.items_of_kind("purge_document") == []
    assert rig.item(index_key(command, index_id)) == "PENDING"
    assert rig.base.indexation(command, index_id).status is IndexationStatus.PENDING


async def test_a_lost_optimistic_lock_surfaces_and_changes_nothing() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    rig.base.harness.state.conflict_on_save = True

    with pytest.raises(ConcurrentModificationError):
        await withdraw(rig, command.document_id)

    assert rig.base.document(command.document_id).status is DocumentStatus.ACTIVE
    assert rig.items_of_kind("purge_document") == []

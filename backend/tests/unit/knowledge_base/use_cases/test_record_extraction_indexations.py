"""``RecordExtraction`` crée les indexations de la version (S4 ;
knowledge-base.md §6, §13 ; ADR 0011 §5). Nouveau comportement, testé à part
des tests de S3.

API figée : la transaction reçue par ``RecordExtractionHandler.apply`` expose
aussi ``corpus`` et ``indexations`` (la ``UnitOfWork`` étendue de S4). Quand
l'extraction est appliquée (version ``RECEIVED`` -> ``EXTRACTED``), DANS la même
transaction :

- le corpus est lu avec ``corpus.get_shared()`` (``FOR SHARE``) ;
- pour chaque index ``ACTIVE`` ou ``BUILDING`` : une ``Indexation`` ``PENDING``
  ``(version_id, index_id)`` portant la ``chunking_policy_version`` du lot, et
  un item ``index_version`` (dedup ``index_version:<version_id>:<index_id>``,
  payload ``document_id``, ``version_id``, ``index_id`` en texte) ;
- aucun corpus, ou index ``RETIRED`` : aucune indexation, aucun item ;
- une extraction ignorée (version écartée, document retiré) ne crée rien.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from tests.unit.knowledge_base.domain.services.support import POLICY_IDENTITY
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    make_indexing_rig,
    qa_sheet,
)

BUILDING = UUID(int=555)
SECOND_BUILDING = UUID(int=556)


def index_items(rig_items: Mapping[str, object]) -> list[str]:
    return sorted(k for k in rig_items if k.startswith("index_version:"))


async def test_an_extraction_creates_a_pending_indexation_for_the_active_index() -> (
    None
):
    rig = make_indexing_rig()
    active = await rig.provision()
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    indexation = rig.indexation(command, active)
    assert indexation.status is IndexationStatus.PENDING
    assert indexation.chunking_policy_version == POLICY_IDENTITY
    assert indexation.manifest_digest is None
    assert len(rig.indexing.indexations) == 1


async def test_an_extraction_enqueues_one_index_version_item_per_indexation() -> None:
    rig = make_indexing_rig()
    active = await rig.provision()
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    key = f"index_version:{command.version_id}:{active}"
    assert index_items(rig.harness.state.items) == [key]
    item = rig.harness.state.items[key]
    assert item.kind == "index_version"
    assert item.payload == {
        "document_id": str(command.document_id),
        "version_id": str(command.version_id),
        "index_id": str(active),
    }


async def test_building_indexes_get_their_own_indexation_and_item() -> None:
    rig = make_indexing_rig()
    active = await rig.provision()
    rig.add_building_index(BUILDING)
    rig.add_building_index(SECOND_BUILDING)
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    assert set(rig.indexing.indexations) == {
        (command.version_id, active),
        (command.version_id, BUILDING),
        (command.version_id, SECOND_BUILDING),
    }
    assert index_items(rig.harness.state.items) == sorted(
        f"index_version:{command.version_id}:{i}"
        for i in (active, BUILDING, SECOND_BUILDING)
    )


async def test_a_retired_index_gets_no_indexation() -> None:
    rig = make_indexing_rig()
    active = await rig.provision()
    rig.add_building_index(BUILDING)
    assert rig.indexing.corpus is not None
    rig.indexing.corpus.retire_index(BUILDING, now=NOW)
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    assert set(rig.indexing.indexations) == {(command.version_id, active)}


async def test_without_a_corpus_the_extraction_still_succeeds_and_indexes_nothing() -> (
    None
):
    rig = make_indexing_rig()
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    assert rig.status(command) is VersionStatus.EXTRACTED
    assert rig.indexing.indexations == {}
    assert index_items(rig.harness.state.items) == []


async def test_the_corpus_is_read_under_a_shared_lock_only() -> None:
    rig = make_indexing_rig()
    await rig.provision()
    command = await rig.import_document(qa_sheet())
    rig.indexing.locks.clear()

    await rig.extract(command)

    assert rig.indexing.locks == ["shared"]


async def test_nothing_is_created_when_the_extraction_is_ignored() -> None:
    rig = make_indexing_rig()
    await rig.provision()
    command = await rig.import_document(qa_sheet())
    prepared = await rig.extract_handler.prepare(command)
    assert prepared is not None
    await rig.import_next_version(command.document_id, qa_sheet(1, tag="v2-"))

    async with rig.unit_of_work() as uow:
        applied = await rig.extract_handler.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.indexing.indexations == {}
    assert index_items(rig.harness.state.items) == []


async def test_the_indexations_are_written_in_the_transaction_of_the_extraction() -> (
    None
):
    rig = make_indexing_rig()
    await rig.provision()
    command = await rig.import_document(qa_sheet())
    prepared = await rig.extract_handler.prepare(command)
    assert prepared is not None

    async with rig.unit_of_work() as uow:
        assert await rig.extract_handler.apply(uow, prepared)
        # pas de commit : rollback

    assert rig.status(command) is VersionStatus.RECEIVED
    assert rig.indexing.indexations == {}
    assert index_items(rig.harness.state.items) == []

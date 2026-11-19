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
- une version ``EXTRACTED`` sans aucune indexation est journalisée en
  ``WARNING`` avec le code ``EXTRACTED_WITHOUT_INDEXATION`` (message et
  attribut ``code`` de l'enregistrement), repère de la réconciliation S8 ;
- une extraction ignorée (version écartée, document retiré) ne crée rien.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    EXTRACTED_WITHOUT_INDEXATION,
)
from tests.unit.knowledge_base.domain.services.support import POLICY_IDENTITY
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    make_indexing_rig,
    qa_sheet,
)

BUILDING = UUID(int=555)


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


async def test_the_building_index_gets_its_own_indexation_and_item() -> None:
    # Un seul index BUILDING par corpus (revue S9) : l'actif et lui.
    rig = make_indexing_rig()
    active = await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())

    await rig.extract(command)

    assert set(rig.indexing.indexations) == {
        (command.version_id, active),
        (command.version_id, BUILDING),
    }
    assert index_items(rig.harness.state.items) == sorted(
        f"index_version:{command.version_id}:{i}" for i in (active, BUILDING)
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


def unindexed_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "code", None) == EXTRACTED_WITHOUT_INDEXATION
    ]


async def test_an_extraction_without_any_indexation_is_logged_with_its_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_indexing_rig()
    command = await rig.import_document(qa_sheet())  # pas de corpus

    with caplog.at_level(logging.WARNING):
        await rig.extract(command)

    (record,) = unindexed_records(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert f"code={EXTRACTED_WITHOUT_INDEXATION}" in message
    assert str(command.version_id) in message
    assert getattr(record, "version_id", None) == str(command.version_id)
    assert getattr(record, "document_id", None) == str(command.document_id)


async def test_an_indexed_extraction_logs_no_missing_indexation_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_indexing_rig()
    await rig.provision()
    command = await rig.import_document(qa_sheet())

    with caplog.at_level(logging.WARNING):
        await rig.extract(command)

    assert unindexed_records(caplog) == []


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

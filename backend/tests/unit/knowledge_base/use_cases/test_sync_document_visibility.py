"""``SyncDocumentVisibility`` : convergence de Qdrant vers l'état SQL du
document (S4 ; knowledge-base.md §6, §10, §11 ; ADR 0010 §4, §5, §7).

API figée (``knowledge_base.use_cases.sync_document_visibility``) :

- ``sync_document_visibility_command.SyncDocumentVisibility(document_id,
  visibility_revision)`` (payload de l'item ``sync_visibility`` ; dedup
  ``sync_visibility:<document_id>:<visibility_revision>``).
- ``sync_document_visibility_handler.SyncDocumentVisibilityHandler(*,
  unit_of_work, vector_index)`` ; ``await handle(command)`` : lit l'état SQL
  COURANT (lecture seule, transaction fermée avant tout appel Qdrant ; la
  révision de la commande n'est qu'une identité, la convergence vise l'état
  courant) puis, dans chaque index non ``RETIRED`` (``corpus.get_shared()``) :
  - document ``WITHDRAWN`` : ``delete_points`` de tout le document ;
  - sinon : ``set_visibility(visible=True)`` pour la version publiée, et
    ``delete_points`` des versions ``SUPERSEDED`` ou ``DISCARDED`` (jamais
    celles qui sont encore ``EXTRACTED`` : une indexation peut être en cours) ;
  - document ``ACTIVE`` sans version publiée : rien n'est rendu visible.
  Document inconnu : sans effet. Aucune écriture PostgreSQL.
"""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_command import (  # noqa: E501
    SyncDocumentVisibility,
)
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    IndexingRig,
    make_indexing_rig,
    qa_sheet,
)

BUILDING = UUID(int=555)


def sync_command(command: RecordExtraction, revision: int) -> SyncDocumentVisibility:
    return SyncDocumentVisibility(
        document_id=command.document_id, visibility_revision=revision
    )


async def published(
    rig: IndexingRig, index_id: UUID | None = None, content: bytes | None = None
) -> tuple[RecordExtraction, UUID]:
    """Version importée, extraite, indexée, donc publiée (points invisibles)."""
    index_id = index_id or await rig.provision()
    command = await rig.import_document(content or qa_sheet())
    await rig.extract(command)
    await rig.index(command, index_id)
    assert rig.status(command) is VersionStatus.PUBLISHED
    return command, index_id


async def test_the_points_of_the_published_version_become_visible() -> None:
    rig = make_indexing_rig()
    command, index_id = await published(rig)
    assert all(not p.visible for p in rig.points_of(command, index_id))

    await rig.sync.handle(sync_command(command, 1))

    points = rig.points_of(command, index_id)
    assert len(points) == len(rig.chunks_of(command))
    assert all(p.visible for p in points)


async def test_the_synchronization_makes_visible_in_every_live_index() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    await rig.index(command, BUILDING)
    await rig.index(command, index_id)

    await rig.sync.handle(sync_command(command, 1))

    for target in (index_id, BUILDING):
        points = rig.points_of(command, target)
        assert points
        assert all(p.visible for p in points)


async def test_a_superseded_version_is_deleted_and_the_new_one_is_visible() -> None:
    rig = make_indexing_rig()
    first, index_id = await published(rig)
    await rig.sync.handle(sync_command(first, 1))
    second = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n2-"))
    await rig.extract(second)
    await rig.index(second, index_id)
    assert rig.status(first) is VersionStatus.SUPERSEDED

    await rig.sync.handle(sync_command(second, 2))

    assert rig.points_of(first, index_id) == []
    new_points = rig.points_of(second, index_id)
    assert new_points
    assert all(p.visible for p in new_points)


async def test_a_version_still_being_indexed_is_left_alone() -> None:
    rig = make_indexing_rig()
    first, index_id = await published(rig)
    second = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n2-"))
    await rig.extract(second)
    await rig.index(second, index_id)  # v2 publiée, v1 supplantée
    third = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n3-"))
    await rig.extract(third)
    prepared = await rig.complete.prepare(rig.complete_command(third, index_id))
    assert prepared is not None  # points de v3 écrits, indexation non finalisée
    assert rig.status(third) is VersionStatus.EXTRACTED

    await rig.sync.handle(sync_command(second, 2))

    assert rig.points_of(first, index_id) == []
    assert all(p.visible for p in rig.points_of(second, index_id))
    third_points = rig.points_of(third, index_id)
    assert len(third_points) == len(rig.chunks_of(third))
    assert all(not p.visible for p in third_points)


async def test_a_withdrawn_document_loses_all_its_points_in_every_index() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    await rig.index(command, index_id)
    await rig.index(command, BUILDING)
    rig.document(command.document_id).withdraw()

    await rig.sync.handle(sync_command(command, 2))

    assert rig.vector.points(index_id) == {}
    assert rig.vector.points(BUILDING) == {}


async def test_the_other_documents_are_not_touched() -> None:
    rig = make_indexing_rig()
    mine, index_id = await published(rig)
    other = await rig.import_document(qa_sheet(2, tag="autre-"), title="Autre fiche")
    await rig.extract(other)
    await rig.index(other, index_id)
    rig.document(mine.document_id).withdraw()

    await rig.sync.handle(sync_command(mine, 2))

    assert rig.points_of(mine, index_id) == []
    other_points = rig.points_of(other, index_id)
    assert len(other_points) == len(rig.chunks_of(other))
    assert all(not p.visible for p in other_points)


async def test_a_retired_index_is_not_touched() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    await rig.index(command, BUILDING)
    await rig.index(command, index_id)
    assert rig.indexing.corpus is not None
    rig.indexing.corpus.retire_index(BUILDING, now=NOW)
    before = dict(rig.vector.points(BUILDING))

    await rig.sync.handle(sync_command(command, 1))

    assert rig.vector.points(BUILDING) == before
    assert all(p.visible for p in rig.points_of(command, index_id))


async def test_an_active_document_without_a_published_version_shows_nothing() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    await rig.complete.prepare(rig.complete_command(command, index_id))

    await rig.sync.handle(sync_command(command, 0))

    points = rig.points_of(command, index_id)
    assert points
    assert all(not p.visible for p in points)


async def test_the_synchronization_is_idempotent() -> None:
    rig = make_indexing_rig()
    first, index_id = await published(rig)
    second = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n2-"))
    await rig.extract(second)
    await rig.index(second, index_id)

    await rig.sync.handle(sync_command(second, 2))
    after_once = dict(rig.vector.points(index_id))
    await rig.sync.handle(sync_command(second, 2))

    assert rig.vector.points(index_id) == after_once
    assert after_once  # la convergence a bien produit un état non vide


async def test_a_late_command_converges_to_the_current_state_not_to_its_revision() -> (
    None
):
    rig = make_indexing_rig()
    first, index_id = await published(rig)
    second = await rig.import_next_version(first.document_id, qa_sheet(1, tag="n2-"))
    await rig.extract(second)
    await rig.index(second, index_id)

    await rig.sync.handle(sync_command(first, 1))  # révision périmée

    assert rig.points_of(first, index_id) == []
    assert all(p.visible for p in rig.points_of(second, index_id))


async def test_an_unknown_document_is_ignored() -> None:
    rig = make_indexing_rig()
    await rig.provision()

    await rig.sync.handle(
        SyncDocumentVisibility(document_id=UUID(int=404), visibility_revision=1)
    )

    assert rig.vector.calls == ["ensure_collection"]


async def test_the_synchronization_works_outside_a_transaction_and_writes_no_sql() -> (
    None
):
    rig = make_indexing_rig()
    command, _ = await published(rig)
    commits = rig.store.commits

    await rig.sync.handle(sync_command(command, 1))

    assert rig.violations == []
    assert rig.store.commits == commits

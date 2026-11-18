"""``PurgeDocument`` : suppression physique des données d'un document retiré
(S7 ; knowledge-base.md §6 ligne ``purge_document``, §7.3, §10 ; ADR 0012 point 6).

API figée (``knowledge_base.use_cases.purge_document``) :

- ``purge_document_command.PurgeDocument(document_id)``.
- ``purge_document_handler.PurgeDocumentHandler(*, unit_of_work, vector_index,
  blob_eraser, blob_references, clock)``, coupé en deux comme
  ``CompleteIndexation`` (la finalisation tourne sous fencing) :

  ``await prepare(command) -> PreparedPurge | None``, hors transaction :
  lit le document et les index vivants (``documents.get``,
  ``corpus.get_shared().live_indexes()``, transaction FERMÉE avant tout appel
  externe) ; ``None``, sans AUCUN effet, si le document est inconnu ou n'est PAS
  ``WITHDRAWN`` (une purge ne détruit jamais un document actif) ; sinon, dans
  l'ordre : (1) ``vector_index.delete_points(index_id, PointSelector(document_id))``
  dans chaque index vivant ; (2) pour l'empreinte de chaque version, suppression
  du blob (``blob_eraser.delete``) SAUF si ``blob_references.holders(hash)``
  contient un autre document que celui-ci (``holders`` : documents qui portent
  l'empreinte et dont la purge n'est pas enregistrée ; le document purgé lui-même
  y figure donc jusqu'à son ``apply`` et ne se protège pas lui-même).
  Les lignes de version (tombstones) portent encore l'empreinte mais ne
  retiennent jamais leur propre blob. Une erreur
  de Qdrant ou du volume remonte (l'item sera rejoué) et interrompt les étapes
  suivantes. N'écrit RIEN dans PostgreSQL.

  ``await apply(transaction, prepared) -> None``, dans la transaction de
  finalisation : ``transaction.purger.delete_derived_data(document_id,
  purged_at=clock())``. Les lignes document et versions restent (tombstones).

Idempotente : rejouée, la purge converge vers le même état sans erreur.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.use_cases.purge_document.purge_document_command import (
    PurgeDocument,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import (
    PURGED_AT,
    S7Rig,
    make_s7_rig,
)

BUILDING = UUID(int=555)


async def two_documents(rig: S7Rig) -> tuple[UUID, UUID, UUID]:
    """``mine`` (versions 1 et 2, points des deux versions présents dans
    l'index) et ``other`` (publié). Rend (mine, other, index_id)."""
    index_id = await rig.base.provision()
    first = await rig.published(index_id, qa_sheet(2), title="Mine")
    second = await rig.import_next(first.document_id, qa_sheet(3, tag="v2"))
    await rig.base.extract(second)
    await rig.base.index(second, index_id)
    other = await rig.published(index_id, qa_sheet(tag="autre"), title="Autre")
    assert rig.base.status(second) is VersionStatus.PUBLISHED
    return first.document_id, other.document_id, index_id


async def test_every_point_of_the_document_is_deleted_in_every_live_index() -> None:
    rig = make_s7_rig()
    index_id = await rig.base.provision()
    rig.base.add_building_index(BUILDING)
    command = await rig.base.import_document(qa_sheet())
    await rig.base.extract(command)
    await rig.base.index(command, index_id)
    await rig.base.index(command, BUILDING)
    assert rig.points_of_document(command.document_id, index_id)
    assert rig.points_of_document(command.document_id, BUILDING)
    rig.mark_withdrawn(command.document_id)

    assert await rig.run_purge(command.document_id) is True

    assert rig.points_of_document(command.document_id, index_id) == []
    assert rig.points_of_document(command.document_id, BUILDING) == []


async def test_the_points_of_every_version_are_deleted_not_only_the_published_one() -> (
    None
):
    rig = make_s7_rig()
    mine, _other, index_id = await two_documents(rig)
    versions = {p.version_id for p in rig.base.vector.points(index_id).values()} & {
        v.version_id for v in rig.base.document(mine).versions
    }
    assert len(versions) == 2  # les points des versions 1 et 2 coexistent
    rig.mark_withdrawn(mine)

    await rig.run_purge(mine)

    assert rig.points_of_document(mine, index_id) == []


async def test_the_points_of_other_documents_are_left_alone() -> None:
    rig = make_s7_rig()
    mine, other, index_id = await two_documents(rig)
    expected = rig.points_of_document(other, index_id)
    assert expected
    rig.mark_withdrawn(mine)

    await rig.run_purge(mine)

    assert rig.points_of_document(other, index_id) == expected


async def test_prepare_writes_nothing_to_postgresql_and_apply_deletes_the_chunks() -> (
    None
):
    rig = make_s7_rig()
    mine, other, _index_id = await two_documents(rig)
    my_versions = {v.version_id for v in rig.base.document(mine).versions}
    other_versions = {v.version_id for v in rig.base.document(other).versions}
    rig.mark_withdrawn(mine)
    use_case = rig.purge_handler()

    prepared = await use_case.prepare(PurgeDocument(document_id=mine))

    assert prepared is not None
    assert my_versions <= rig.versions_with_chunks()  # rien encore en base
    assert rig.ledger.purged_at == {}
    async with rig.purge_unit_of_work() as uow:
        await use_case.apply(uow, prepared)
        await uow.commit()
    assert rig.versions_with_chunks() == other_versions
    assert rig.ledger.purged_at == {mine: PURGED_AT}


async def test_the_apply_is_only_visible_after_the_commit() -> None:
    rig = make_s7_rig()
    mine, _other, _index_id = await two_documents(rig)
    rig.mark_withdrawn(mine)
    use_case = rig.purge_handler()
    prepared = await use_case.prepare(PurgeDocument(document_id=mine))
    assert prepared is not None
    before = rig.versions_with_chunks()

    async with rig.purge_unit_of_work() as uow:
        await use_case.apply(uow, prepared)
        # pas de commit : sortie du bloc = rollback

    assert rig.versions_with_chunks() == before
    assert rig.ledger.purged_at == {}


async def test_the_blobs_of_every_version_are_deleted_and_the_others_kept() -> None:
    rig = make_s7_rig()
    mine, other, _index_id = await two_documents(rig)
    mine_hashes = {
        v.source.content_hash.value for v in rig.base.document(mine).versions
    }
    other_hashes = {
        v.source.content_hash.value for v in rig.base.document(other).versions
    }
    assert len(mine_hashes) == 2
    assert (mine_hashes | other_hashes) <= rig.blobs.present
    rig.mark_withdrawn(mine)

    await rig.run_purge(mine)

    assert rig.blobs.present & mine_hashes == set()
    assert other_hashes <= rig.blobs.present


async def test_a_blob_still_held_by_another_document_is_not_deleted() -> None:
    rig = make_s7_rig()
    mine, _other, _index_id = await two_documents(rig)
    shared = rig.base.document(mine).versions[0].source.content_hash
    private = rig.base.document(mine).versions[1].source.content_hash
    rig.references.also_held_by(shared, uuid4())
    rig.mark_withdrawn(mine)

    await rig.run_purge(mine)

    assert shared.value in rig.blobs.present
    assert private.value not in rig.blobs.present


async def test_the_tombstone_versions_do_not_count_as_references() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.mark_withdrawn(command.document_id)
    # La ligne de version, qui reste, porte encore l'empreinte.
    assert command.document_id in await rig.references.holders(digest)

    await rig.run_purge(command.document_id)

    assert digest.value not in rig.blobs.present


async def test_the_tombstone_rows_are_kept_and_the_purge_date_recorded() -> None:
    rig = make_s7_rig()
    mine, _other, _index_id = await two_documents(rig)
    rig.mark_withdrawn(mine)
    before = rig.base.document(mine).versions

    await rig.run_purge(mine)

    document = rig.base.document(mine)
    assert document.status is DocumentStatus.WITHDRAWN
    assert document.versions == before
    assert rig.ledger.purged_at == {mine: PURGED_AT}


async def test_a_second_purge_converges_without_error() -> None:
    rig = make_s7_rig()
    mine, other, index_id = await two_documents(rig)
    rig.mark_withdrawn(mine)
    await rig.run_purge(mine)
    points = dict(rig.base.vector.points(index_id))
    blobs = set(rig.blobs.present)
    chunked = rig.versions_with_chunks()

    assert await rig.run_purge(mine) is True

    assert dict(rig.base.vector.points(index_id)) == points
    assert rig.blobs.present == blobs
    assert rig.versions_with_chunks() == chunked
    assert rig.points_of_document(other, index_id)


async def test_a_document_that_is_not_withdrawn_is_never_purged() -> None:
    rig = make_s7_rig()
    mine, _other, index_id = await two_documents(rig)
    points = dict(rig.base.vector.points(index_id))
    blobs = set(rig.blobs.present)
    chunked = rig.versions_with_chunks()

    assert await rig.run_purge(mine) is False

    assert dict(rig.base.vector.points(index_id)) == points
    assert rig.blobs.present == blobs
    assert rig.versions_with_chunks() == chunked
    assert rig.ledger.purged_at == {}
    assert rig.blobs.delete_calls == []


async def test_an_unknown_document_is_ignored() -> None:
    rig = make_s7_rig()
    await rig.base.provision()

    assert await rig.run_purge(uuid4()) is False

    assert rig.blobs.delete_calls == []
    assert rig.base.vector.calls.count("delete_points") == 0


async def test_a_document_never_indexed_still_loses_its_blobs() -> None:
    rig = make_s7_rig()  # aucun corpus : aucun index vivant
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.mark_withdrawn(command.document_id)

    assert await rig.run_purge(command.document_id) is True

    assert rig.base.vector.calls == []
    assert digest.value not in rig.blobs.present


async def test_when_qdrant_fails_nothing_else_is_deleted_and_a_retry_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_s7_rig()
    mine, _other, index_id = await two_documents(rig)
    rig.mark_withdrawn(mine)
    original = rig.base.vector.delete_points

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Qdrant indisponible")

    monkeypatch.setattr(rig.base.vector, "delete_points", unavailable)
    blobs = set(rig.blobs.present)
    chunked = rig.versions_with_chunks()

    with pytest.raises(RuntimeError, match="Qdrant indisponible"):
        await rig.run_purge(mine)

    # Les points d'abord : tant qu'ils existent, ni blob ni chunk ne disparaît.
    assert rig.blobs.present == blobs
    assert rig.versions_with_chunks() == chunked
    assert rig.ledger.purged_at == {}
    monkeypatch.setattr(rig.base.vector, "delete_points", original)
    await rig.run_purge(mine)
    assert rig.points_of_document(mine, index_id) == []
    assert rig.ledger.purged_at == {mine: PURGED_AT}


async def test_no_external_call_is_made_while_a_transaction_is_open() -> None:
    rig = make_s7_rig()
    mine, _other, _index_id = await two_documents(rig)
    rig.mark_withdrawn(mine)

    await rig.run_purge(mine)

    assert rig.base.violations == []

"""Réconciliation PostgreSQL / Qdrant (S8 ; knowledge-base.md §7.3 condition 6,
§8, §9, §10, §11 ; roadmap S8), sur PostgreSQL, Qdrant et work queue réels.

Chaque écart est INJECTÉ (SQL, ou écriture directe dans Qdrant), détecté par le
rapport du premier cycle, puis corrigé : le travail que le cycle planifie est
exécuté (``drain``), et le cycle suivant est propre (``settle``). Un cycle sur
un état sain ne crée aucun travail et ne modifie rien.

Contrat vérifié (voir ``s8_support`` pour la composition) :

- indexation ``COMPLETED`` non publiée -> publiée (I5, I9) ;
- version ``EXTRACTED`` sans indexation dans un index vivant -> indexation
  ``PENDING`` et item ``index_version`` ; une version dont l'indexation existe
  (même ``PENDING``) n'est pas un écart ;
- point sans propriétaire (document inconnu, version inconnue, version
  ``DISCARDED`` ou ``SUPERSEDED``) -> supprimé ; les points légitimes sont
  intouchés ;
- point d'une version ``PUBLISHED`` de document ``ACTIVE`` avec ``visible=false``
  -> ``sync_visibility`` ; pas de doublon si une synchronisation est déjà en
  attente ;
- document ``WITHDRAWN`` et purgé qui a encore des points -> ``purge_document``
  rejouée ; un document retiré dont la purge est simplement en attente n'est
  PAS touché ; item ``purge_document`` ``FAILED`` -> rouvert ;
- manifest : au plus ``reconcile_manifest_sample_size`` indexations ``COMPLETED``
  revérifiées par cycle (digest enregistré, présence et ``chunk_hash`` dans
  Qdrant) ; un écart ré-ouvre l'indexation (``COMPLETED`` -> ``PENDING``) ;
  la réparation d'une version PUBLIÉE ne réécrit que les points manquants ou
  divergents : le document reste trouvable pendant toute la réparation
  (revue S8, I3).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from knowledge_base.domain.services.reconciliation import DiscrepancyKind as K
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    QA_SHEET,
    QA_SHEET_V2,
    payload,
)
from tests.integration.knowledge_base.s7_support import (
    PYDANTIC_SHEET,
    TIMEOUT,
    GatedVectorIndex,
)
from tests.integration.knowledge_base.s8_support import (
    S8Rig,
    first_k,
    open_s8_rig,
    settings_with,
)

pytestmark = pytest.mark.integration
QUESTIONS = 3  # QA_SHEET : une question = un chunk = un point


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S8Rig]:
    async with open_s8_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


# --- état sain -----------------------------------------------------------------------


async def test_a_healthy_corpus_is_left_untouched_and_no_work_is_created(
    rig: S8Rig,
) -> None:
    first, second = await rig.publish_in_two_documents()
    items = await rig.snapshot()
    points = {
        d: await rig.point_ids_of(d) for d in (first.document_id, second.document_id)
    }

    report = await rig.cycle()

    assert report.clean, report
    assert await rig.snapshot() == items
    assert {d: await rig.point_ids_of(d) for d in points} == points
    assert await rig.found() == {first.document_id, second.document_id}


async def test_a_cycle_on_an_empty_knowledge_base_is_clean(rig: S8Rig) -> None:
    assert (await rig.cycle()).clean


class PublishingBeforeScan:
    """Qdrant, dont le parcours commence par la publication d'un document :
    l'écriture concurrente qu'un cycle en cours peut rencontrer."""

    def __init__(self, inner: Any, publish: Callable[[], Awaitable[object]]) -> None:
        self._inner = inner
        self._publish = publish

    async def scan_points(
        self, index_id: UUID, *, batch_size: int = 256
    ) -> AsyncIterator[Any]:
        await self._publish()
        async for point in self._inner.scan_points(index_id, batch_size=batch_size):
            yield point

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_document_published_while_the_cycle_runs_is_never_taken_for_an_orphan(
    rig: S8Rig,
) -> None:
    """Qdrant se lit AVANT PostgreSQL : un point écrit après la lecture SQL
    serait pris pour un point sans propriétaire et supprimé (perte de données).
    Les versions ne disparaissent jamais de PostgreSQL : lire le SQL en dernier
    ne peut produire qu'un point « de trop » manqué, jamais un point légitime
    supprimé."""
    imported: list[Imported] = []

    async def publish() -> None:
        imported.append(await rig.s7.publish(QA_SHEET, title="Concurrent"))

    report = await rig.cycle(vector=PublishingBeforeScan(rig.vector, publish))

    (concurrent,) = imported
    assert report.count(K.ORPHAN_POINTS) == 0
    assert await rig.visibility(concurrent.document_id) == [True] * QUESTIONS
    assert await rig.found() == {concurrent.document_id}


# --- indexations COMPLETED non publiées ----------------------------------------------


async def test_a_completed_indexation_of_an_unpublished_version_gets_published(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    before = await rig.document(imported.document_id)
    # État laissé par une publication refusée à la finalisation de l'indexation.
    await rig.execute(
        "UPDATE knowledge_base.document_versions SET status = 'EXTRACTED' "
        "WHERE id = :v",
        v=imported.version_id,
    )
    await rig.execute(
        "UPDATE knowledge_base.documents SET published_version_id = NULL WHERE id = :d",
        d=imported.document_id,
    )
    assert await rig.found() == set(), "précondition : rien n'est publié"

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.COMPLETED_NOT_PUBLISHED) == 1
    version = await rig.version(imported.version_id)
    document = await rig.document(imported.document_id)
    assert version["status"] == "PUBLISHED"
    assert document["published_version_id"] == imported.version_id
    assert document["visibility_revision"] > before["visibility_revision"]
    assert await rig.found() == {imported.document_id}


async def test_an_older_completed_version_is_not_published_over_a_newer_one(
    rig: S8Rig,
) -> None:
    """I5 : la réconciliation ne republie jamais une version plus ancienne."""
    imported = await rig.s7.publish(QA_SHEET)
    newer = await rig.s7.post_version(imported.document_id, QA_SHEET_V2)
    await rig.s7.extract(newer)
    assert await rig.indexing.run_index(newer.version_id, rig.index_id) == "succeeded"
    await rig.s7.synchronize(imported.document_id)
    await rig.execute(
        "UPDATE knowledge_base.document_versions SET status = 'EXTRACTED' "
        "WHERE id = :v",
        v=imported.version_id,
    )  # la version 1, ancienne, ``COMPLETED``, ne doit pas revenir

    await rig.settle()

    document = await rig.document(imported.document_id)
    assert document["published_version_id"] == newer.version_id


# --- versions EXTRACTED sans indexation ----------------------------------------------


async def test_an_extracted_version_without_indexation_is_indexed_and_published(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)
    await rig.execute(
        "DELETE FROM knowledge_base.indexations WHERE version_id = :v",
        v=imported.version_id,
    )
    await rig.execute(
        "DELETE FROM knowledge_base.work_items WHERE kind = 'index_version'"
    )

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.EXTRACTED_WITHOUT_INDEXATION) == 1
    assert (await rig.indexation(imported.version_id))["status"] == "COMPLETED"
    assert (await rig.version(imported.version_id))["status"] == "PUBLISHED"
    assert await rig.found() == {imported.document_id}


async def test_an_extracted_version_being_indexed_is_not_an_anomaly(rig: S8Rig) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)  # indexation et item PENDING
    items = await rig.snapshot()

    report = await rig.cycle()

    assert report.count(K.EXTRACTED_WITHOUT_INDEXATION) == 0
    assert report.clean, report
    assert await rig.snapshot() == items
    assert (await rig.indexation(imported.version_id))["status"] == "PENDING"


# --- points en trop ------------------------------------------------------------------


async def test_points_without_an_owner_are_deleted_and_legitimate_ones_kept(
    rig: S8Rig,
) -> None:
    kept = await rig.s7.publish(QA_SHEET, title="Légitime")
    legitimate = await rig.point_ids_of(kept.document_id)
    second = await rig.s7.import_extract(PYDANTIC_SHEET, title="Deux versions")
    newest = await rig.s7.post_version(second.document_id, QA_SHEET_V2)
    assert (await rig.version(second.version_id))["status"] == "DISCARDED"
    unknown_document = rig.fake_point(uuid.uuid4(), uuid.uuid4())
    unknown_version = rig.fake_point(kept.document_id, uuid.uuid4())
    discarded_version = rig.fake_point(second.document_id, second.version_id)
    await rig.write_points(unknown_document, unknown_version, discarded_version)

    report = await rig.cycle()
    await rig.settle()  # exécute aussi l'extraction de la version 2 en attente

    assert report.count(K.ORPHAN_POINTS) == 3
    owners = {
        (p.payload["document_id"], p.payload["version_id"])
        for p in await rig.indexing.points(rig.index_id)
        if p.payload
    }
    assert owners == {
        (str(kept.document_id), str(kept.version_id)),
        (str(second.document_id), str(newest.version_id)),
    }
    assert legitimate <= await rig.indexing.point_ids(rig.index_id)
    assert await rig.visibility(kept.document_id) == [True] * QUESTIONS
    assert await rig.found() == {kept.document_id, second.document_id}


async def test_the_points_of_a_superseded_version_are_deleted(rig: S8Rig) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    newer = await rig.s7.post_version(imported.document_id, QA_SHEET_V2)
    await rig.s7.extract(newer)
    assert await rig.indexing.run_index(newer.version_id, rig.index_id) == "succeeded"
    await rig.s7.synchronize(imported.document_id)
    assert (await rig.version(imported.version_id))["status"] == "SUPERSEDED"
    await rig.write_points(rig.fake_point(imported.document_id, imported.version_id))

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.ORPHAN_POINTS) == 1
    remaining = await rig.s7.points_of(imported.document_id)
    assert {p.payload["version_id"] for p in remaining if p.payload} == {
        str(newer.version_id)
    }


# --- points invisibles d'une version publiée -----------------------------------------


async def test_hidden_points_of_a_published_version_become_visible_again(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    # Tentative zombie d'index_version : réécrit les points ``visible=false``.
    await rig.hide(imported.document_id, imported.version_id)
    assert await rig.found() == set(), "précondition : le document manque"

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.HIDDEN_PUBLISHED_POINTS) == QUESTIONS
    assert await rig.visibility(imported.document_id) == [True] * QUESTIONS
    assert await rig.found() == {imported.document_id}


async def test_a_publication_waiting_for_its_synchronization_is_not_a_defect(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)
    assert (
        await rig.indexing.run_index(imported.version_id, rig.index_id) == "succeeded"
    )
    # Publiée, points encore invisibles, synchronisation planifiée et PENDING.
    syncs = await rig.items("sync_visibility", "PENDING")
    assert len(syncs) == 1

    report = await rig.cycle()

    assert report.count(K.HIDDEN_PUBLISHED_POINTS) == 0
    assert len(await rig.items("sync_visibility")) == 1


async def test_hidden_points_already_covered_by_pending_work_get_no_second_item(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.hide(imported.document_id, imported.version_id)

    await rig.cycle()
    await rig.cycle()  # le travail du premier cycle n'est pas forcément exécuté

    assert len(await rig.items("sync_visibility", "PENDING")) <= 1


# --- documents retirés ---------------------------------------------------------------


async def test_a_zombie_write_after_the_purge_is_removed_by_a_new_purge(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.s7.withdraw(imported.document_id)
    assert await rig.s7.run_purge(imported.document_id) == "succeeded"
    assert await rig.s7.points_of(imported.document_id) == []
    # La tentative zombie d'index_version écrit APRÈS la purge (§7.3 cond. 6).
    await rig.write_points(
        rig.fake_point(imported.document_id, imported.version_id),
        rig.fake_point(imported.document_id, imported.version_id),
    )

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.WITHDRAWN_DOCUMENT_POINTS) == 2
    assert await rig.s7.points_of(imported.document_id) == []
    document = await rig.document(imported.document_id)
    assert document["status"] == "WITHDRAWN"
    assert document["purged_at"] is not None


async def test_a_withdrawn_document_waiting_for_its_purge_is_left_to_that_purge(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.s7.withdraw(imported.document_id)
    points = await rig.point_ids_of(imported.document_id)
    purges = await rig.items("purge_document")
    assert [p["status"] for p in purges] == ["PENDING"]

    report = await rig.cycle()

    assert report.count(K.WITHDRAWN_DOCUMENT_POINTS) == 0
    assert await rig.point_ids_of(imported.document_id) == points
    assert await rig.items("purge_document") == purges


async def test_a_failed_purge_is_reopened_and_completes(rig: S8Rig) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.s7.withdraw(imported.document_id)
    (purge,) = await rig.items("purge_document")
    await rig.fail_item(purge["id"], "UNEXPECTED_ERROR", message="Qdrant injoignable")

    report = await rig.cycle()

    assert report.count(K.FAILED_PURGE) == 1
    reopened = (await rig.items("purge_document"))[0]
    assert (reopened["status"], reopened["attempts"]) == ("PENDING", 0)
    await rig.settle()
    assert await rig.s7.points_of(imported.document_id) == []
    assert (await rig.document(imported.document_id))["purged_at"] is not None
    assert (await rig.items("purge_document"))[0]["status"] == "SUCCEEDED"


# --- manifest ------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["lost_point", "wrong_hash"])
async def test_a_damaged_manifest_is_detected_and_the_indexation_redone(
    rig: S8Rig, damage: str
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    points = sorted(await rig.point_ids_of(imported.document_id), key=str)
    expected = await rig.indexing.expected_manifest(imported.version_id, rig.index_id)
    if damage == "lost_point":
        await rig.delete_point(points[0])
    else:
        await rig.corrupt_hash(points[0])

    report = await rig.cycle()

    assert report.count(K.MANIFEST_DRIFT) == 1
    reopened = await rig.indexation(imported.version_id)
    assert reopened["status"] == "PENDING"
    assert reopened["manifest_digest"] is None
    assert reopened["completed_at"] is None
    await rig.settle()
    healed = await rig.indexation(imported.version_id)
    assert healed["status"] == "COMPLETED"
    assert healed["manifest_digest"] == expected.digest
    assert healed["point_count"] == QUESTIONS
    assert await rig.point_ids_of(imported.document_id) == set(points)
    assert await rig.visibility(imported.document_id) == [True] * QUESTIONS
    assert await rig.found() == {imported.document_id}


class GateAfterUpsert(GatedVectorIndex):
    """Le premier upsert est APPLIQUÉ, puis la barrière : le test observe
    l'index au pire moment de la réparation (points réécrits invisibles)."""

    def __init__(self, inner: Any, *, gate_on: str) -> None:
        super().__init__(inner, gate_on=gate_on)
        self.written: list[UUID] = []

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        await self._inner.upsert(index_id, points, wait=wait)
        self.written = [p.point_id for p in points]
        await self._gate("upsert")


async def test_repairing_a_published_version_keeps_it_searchable_throughout(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    lost, *intact = sorted(await rig.point_ids_of(imported.document_id), key=str)
    await rig.delete_point(lost)
    assert await rig.found() == {imported.document_id}

    report = await rig.cycle()

    assert report.count(K.MANIFEST_DRIFT) == 1
    assert await rig.found() == {imported.document_id}
    (repair,) = [
        i
        for i in await rig.items("index_version", "PENDING")
        if i["payload"]["version_id"] == str(imported.version_id)
    ]
    gated = GateAfterUpsert(rig.vector, gate_on="upsert")
    handler = rig.indexing.index_handler_over(gated)
    running = asyncio.create_task(rig.s7.indexing.extraction.run(repair["id"], handler))
    async with asyncio.timeout(TIMEOUT):
        await gated.reached.wait()
    try:
        # Barrière : seul le point perdu a été réécrit (invisible) ; les
        # autres sont restés visibles, le document reste trouvable.
        assert gated.written == [lost]
        visible = {
            UUID(str(r.id)): bool(payload(r)["visible"])
            for r in await rig.s7.points_of(imported.document_id)
        }
        assert visible == {lost: False, **dict.fromkeys(intact, True)}
        assert await rig.found() == {imported.document_id}
    finally:
        gated.release.set()
    assert await running == "succeeded"

    await rig.settle()
    assert await rig.visibility(imported.document_id) == [True] * QUESTIONS
    assert await rig.found() == {imported.document_id}
    assert (await rig.indexation(imported.version_id))["status"] == "COMPLETED"


async def test_a_recorded_digest_that_no_longer_matches_the_chunks_is_detected(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    expected = await rig.indexing.expected_manifest(imported.version_id, rig.index_id)
    await rig.execute(
        "UPDATE knowledge_base.indexations SET manifest_digest = :d "
        "WHERE version_id = :v",
        d="0" * 64,
        v=imported.version_id,
    )

    report = await rig.cycle()
    await rig.settle()

    assert report.count(K.MANIFEST_DRIFT) == 1
    assert (await rig.indexation(imported.version_id))["manifest_digest"] == (
        expected.digest
    )


async def test_each_cycle_verifies_at_most_the_sampled_number_of_manifests(
    rig: S8Rig,
) -> None:
    first, second = await rig.publish_in_two_documents()
    for imported in (first, second):
        lost = sorted(await rig.point_ids_of(imported.document_id), key=str)[0]
        await rig.delete_point(lost)
    one_per_cycle = settings_with(reconcile_manifest_sample_size=1)

    reports = [
        await rig.cycle(settings=one_per_cycle, sampler=first_k) for _ in range(3)
    ]

    assert [r.count(K.MANIFEST_DRIFT) for r in reports] == [1, 1, 0]
    statuses = {(await rig.indexation(i.version_id))["status"] for i in (first, second)}
    assert statuses == {"PENDING"}


async def test_a_sample_size_of_zero_verifies_nothing(rig: S8Rig) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.delete_point(
        sorted(await rig.point_ids_of(imported.document_id), key=str)[0]
    )

    report = await rig.cycle(settings=settings_with(reconcile_manifest_sample_size=0))

    assert report.count(K.MANIFEST_DRIFT) == 0
    assert (await rig.indexation(imported.version_id))["status"] == "COMPLETED"


async def test_only_completed_indexations_are_sampled(rig: S8Rig) -> None:
    pending = await rig.s7.import_extract(QA_SHEET)  # PENDING : aucun manifest
    published = await rig.s7.publish(PYDANTIC_SHEET, title="Publiée")
    await rig.delete_point(
        sorted(await rig.point_ids_of(published.document_id), key=str)[0]
    )

    report = await rig.cycle(
        settings=settings_with(reconcile_manifest_sample_size=1), sampler=first_k
    )

    assert report.count(K.MANIFEST_DRIFT) == 1
    assert (await rig.indexation(pending.version_id))["status"] == "PENDING"
    assert (await rig.indexation(published.version_id))["status"] == "PENDING"


# --- second cycle --------------------------------------------------------------------


async def test_a_second_cycle_after_the_correction_finds_nothing_and_changes_nothing(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.hide(imported.document_id, imported.version_id)
    await rig.write_points(rig.fake_point(uuid.uuid4(), uuid.uuid4()))
    await rig.settle()
    state = await rig.snapshot()
    points = await rig.indexing.point_ids(rig.index_id)

    report = await rig.cycle()

    assert report.clean, report
    assert await rig.snapshot() == state
    assert await rig.indexing.point_ids(rig.index_id) == points

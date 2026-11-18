"""Décisions de l'orchestrateur pour S8, postérieures aux tests rouges, sur
PostgreSQL, Qdrant et volume réels (knowledge-base.md §16, précisions S8) :

- 6 : la réouverture d'une purge ``FAILED`` par la réconciliation est BORNÉE
  (``reconcile_max_purge_reopenings``, compteur ``work_items.reopen_count``) ;
  au-delà, la purge reste ``FAILED``, signalée à chaque cycle, rejouable par
  l'API ;
- 7 : une purge replanifiée après une purge réussie (points écrits par une
  tentative zombie) prend la génération suivante de sa clé,
  ``purge_document:<id>:<n>`` ; la clé d'origine reste celle de l'item
  terminé ;
- 8 (bout en bout ; règle unitaire dans ``test_reindex_published_version``) :
  une indexation rouverte (manifest) d'une version publiée est refaite SOUS
  UNE NOUVELLE GÉNÉRATION de ``index_version``, que l'annulation par un
  retrait atteint (``cancel`` annule la famille de la clé) ;
- 9 : un original laissé sous ``quarantine/`` par un crash est remis à son
  chemin s'il est détenu et que ce chemin est libre, supprimé sinon une fois
  plus vieux que le délai de grâce ;
- 12 : points « en trop » d'une version légitime (hors de son manifest)
  supprimés par identifiant quand son manifest est revérifié.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_base.adapters.controller.composition import (
    build_reconcile_knowledge_base_handler,
)
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.services.reconciliation import DiscrepancyKind as K
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconciliation_report import (
    ReconciliationReport,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET
from tests.integration.knowledge_base.s8_support import (
    PREFIX,
    S8Rig,
    first_k,
    open_s8_rig,
    settings_with,
)

pytestmark = pytest.mark.integration
QUESTIONS = 3


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


async def cycle_at(rig: S8Rig, clock: Callable[[], datetime]) -> ReconciliationReport:
    handler = build_reconcile_knowledge_base_handler(
        rig.session_factory, rig.vector, rig.settings, clock=clock
    )
    return await handler.handle(ReconcileKnowledgeBase())


# --- 6 : réouverture bornée d'une purge en échec -------------------------------------


async def test_a_failed_purge_is_reopened_at_most_the_configured_number_of_times(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.s7.withdraw(imported.document_id)
    (purge,) = await rig.items("purge_document")
    two_reopenings = settings_with(reconcile_max_purge_reopenings=2)

    reopenings = []
    for _ in range(3):
        # Qdrant durablement en panne : chaque reprise échoue de nouveau.
        await rig.fail_item(purge["id"], "UNEXPECTED_ERROR", message="Qdrant")
        report = await rig.cycle(settings=two_reopenings)
        (item,) = await rig.items("purge_document")
        reopenings.append((report.count(K.FAILED_PURGE), item["status"]))

    assert reopenings == [(1, "PENDING"), (1, "PENDING"), (1, "FAILED")]
    (left,) = await rig.items("purge_document")
    assert left["reopen_count"] == 2
    assert not (await rig.cycle(settings=two_reopenings)).clean, "signalée"
    assert (await rig.items("purge_document"))[0]["status"] == "FAILED"
    # Le rejeu explicite reste possible, et il est compté.
    retried = await rig.http.post(f"{PREFIX}/failures/{purge['id']}/retry")
    assert retried.status_code == 202, retried.text
    (reopened,) = await rig.items("purge_document")
    assert (reopened["status"], reopened["reopen_count"]) == ("PENDING", 3)
    await rig.settle()
    assert await rig.s7.points_of(imported.document_id) == []


async def test_a_retry_through_the_api_counts_as_a_reopening(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")

    response = await rig.http.post(f"{PREFIX}/failures/{imported.item_id}/retry")

    assert response.status_code == 202, response.text
    assert (await extraction.item(imported.item_id))["reopen_count"] == 1


# --- 7 : purge replanifiée sous la génération suivante -------------------------------


async def test_each_new_purge_after_zombie_writes_takes_the_next_generation(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    document_id = imported.document_id
    await rig.s7.withdraw(document_id)
    assert await rig.s7.run_purge(document_id) == "succeeded"

    keys = []
    for _ in range(2):
        await rig.write_points(rig.fake_point(document_id, imported.version_id))
        await rig.cycle()
        keys.append(
            [(i["dedup_key"], i["status"]) for i in await rig.items("purge_document")]
        )
        await rig.settle()
        assert await rig.s7.points_of(document_id) == []

    base = f"purge_document:{document_id}"
    assert keys == [
        [(base, "SUCCEEDED"), (f"{base}:2", "PENDING")],
        [(base, "SUCCEEDED"), (f"{base}:2", "SUCCEEDED"), (f"{base}:3", "PENDING")],
    ]


# --- 8 : génération d'index_version annulée par un retrait ---------------------------


async def test_a_withdrawal_cancels_a_reindexation_scheduled_by_the_reconciliation(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    lost = sorted(await rig.point_ids_of(imported.document_id), key=str)[0]
    await rig.delete_point(lost)
    await rig.cycle()
    base = f"index_version:{imported.version_id}:{rig.index_id}"
    assert [
        (i["dedup_key"], i["status"]) for i in await rig.items("index_version")
    ] == [(base, "SUCCEEDED"), (f"{base}:2", "PENDING")]

    await rig.s7.withdraw(imported.document_id)

    statuses = {i["dedup_key"]: i["status"] for i in await rig.items("index_version")}
    assert statuses == {base: "SUCCEEDED", f"{base}:2": "CANCELLED"}
    assert (await rig.indexation(imported.version_id))["status"] == "CANCELLED"


# --- 9 : quarantaine laissée par un crash --------------------------------------------


def quarantine(rig: S8Rig, content: bytes, *, from_path: bool) -> Path:
    """Pose le fichier sous ``quarantine/<empreinte>-<uuid>``, comme
    ``delete_if_unchanged`` avant un crash : déplacé depuis son chemin, ou
    copie d'un original resté (ou redéposé) à son chemin."""
    digest = hashlib.sha256(content).hexdigest()
    target = rig.blob_root / "quarantine" / f"{digest}-{uuid.uuid4().hex}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if from_path:
        blob_path(rig.blob_root, content).rename(target)
    else:
        target.write_bytes(content)
    return target


def later(by: timedelta) -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + by


async def test_a_held_blob_left_in_quarantine_is_put_back_and_extractable(
    rig: S8Rig,
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    quarantined = quarantine(rig, QA_SHEET, from_path=True)
    assert not blob_path(rig.blob_root, QA_SHEET).exists()

    report = await rig.cycle()

    assert report.count(K.QUARANTINED_BLOB) == 1
    assert not quarantined.exists()
    assert blob_path(rig.blob_root, QA_SHEET).read_bytes() == QA_SHEET
    assert await extraction.run(imported.item_id) == "succeeded"
    assert (await rig.cycle()).clean


async def test_an_unheld_quarantined_blob_is_deleted_only_after_the_grace_period(
    rig: S8Rig,
) -> None:
    orphan = quarantine(rig, b"orphelin en quarantaine", from_path=False)

    young = await cycle_at(rig, later(timedelta(hours=1)))
    assert young.count(K.QUARANTINED_BLOB) == 0
    assert orphan.is_file()

    old = await cycle_at(rig, later(timedelta(hours=25)))

    assert old.count(K.QUARANTINED_BLOB) == 1
    assert not orphan.exists()


async def test_a_quarantined_copy_of_a_blob_back_in_place_is_deleted_after_grace(
    rig: S8Rig,
) -> None:
    await rig.s7.publish(QA_SHEET)
    copy = quarantine(rig, QA_SHEET, from_path=False)  # chemin déjà occupé

    report = await cycle_at(rig, later(timedelta(hours=25)))

    assert report.count(K.QUARANTINED_BLOB) == 1
    assert not copy.exists()
    assert blob_path(rig.blob_root, QA_SHEET).read_bytes() == QA_SHEET


# --- 12 : points en trop d'une version légitime --------------------------------------


async def test_points_outside_the_manifest_of_a_verified_version_are_deleted(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    legitimate = await rig.point_ids_of(imported.document_id)
    stray_chunk = uuid.uuid4()
    await rig.write_points(
        rig.fake_point(imported.document_id, imported.version_id, chunk_id=stray_chunk)
    )

    report = await rig.cycle(
        settings=settings_with(reconcile_manifest_sample_size=1), sampler=first_k
    )

    assert report.count(K.ORPHAN_POINTS) == 1
    assert report.count(K.MANIFEST_DRIFT) == 0
    assert await rig.point_ids_of(imported.document_id) == legitimate
    assert point_id(rig.index_id, stray_chunk) not in legitimate
    assert (await rig.indexation(imported.version_id))["status"] == "COMPLETED"
    assert await rig.visibility(imported.document_id) == [True] * QUESTIONS
    assert (await rig.cycle()).clean

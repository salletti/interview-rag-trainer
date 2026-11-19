"""Entretien périodique par la réconciliation (S8) : blobs orphelins, temporaires
de dépôt abandonnés, clés d'idempotence expirées (O15). PostgreSQL et volume de
blobs réels.

Contrat vérifié :

- ``CleanBlobOrphans`` (S7) est ordonnancé par la réconciliation avec le délai
  ``settings.blob_orphan_grace`` : un orphelin plus vieux est supprimé, un jeune
  ou un blob référencé (même retiré, non purgé) est conservé ; le décompte
  ``ORPHAN_BLOB`` est le nombre de fichiers supprimés.
- Les temporaires de ``<blob_root>/tmp/`` plus vieux que le MÊME délai sont
  supprimés (``STALE_UPLOAD``) ; un temporaire récent (import en cours) et les
  blobs adressés sont intacts.
- Les clés d'idempotence plus vieilles que ``settings.idempotency_key_ttl``
  (24 h par défaut, O15) sont purgées (``EXPIRED_IDEMPOTENCY_KEY``) ; les
  récentes sont conservées. La purge d'une clé n'altère aucune version : la
  contrainte ``UNIQUE(content_hash)`` protège de toute façon des doublons.
- Les trois réglages sont lus des ``settings`` du cycle, pas figés.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.services.reconciliation import DiscrepancyKind as K
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET
from tests.integration.knowledge_base.s7_support import age
from tests.integration.knowledge_base.s8_support import (
    OLD,
    YOUNG,
    S8Rig,
    open_s8_rig,
    settings_with,
)

pytestmark = pytest.mark.integration


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


async def drop(rig: S8Rig, content: bytes, by: timedelta) -> Path:
    async def one_chunk() -> AsyncIterator[bytes]:
        yield content

    await FileSystemBlobStore(rig.blob_root).put(one_chunk(), max_bytes=64 * 1024)
    path = blob_path(rig.blob_root, content)
    age(path, by)
    return path


# --- blobs orphelins -----------------------------------------------------------------


async def test_the_cycle_deletes_old_orphans_and_keeps_young_and_referenced_blobs(
    rig: S8Rig,
) -> None:
    published = await rig.s7.publish(QA_SHEET)
    age(blob_path(rig.blob_root, QA_SHEET), OLD)  # ancien mais référencé
    old = await drop(rig, b"orphelin ancien", OLD)
    young = await drop(rig, b"orphelin recent", YOUNG)

    report = await rig.cycle()

    assert report.count(K.ORPHAN_BLOB) == 1
    assert not old.exists()
    assert young.is_file()
    assert blob_path(rig.blob_root, QA_SHEET).is_file()
    assert (await rig.document(published.document_id))["status"] == "ACTIVE"


async def test_the_orphan_grace_period_is_the_one_of_the_settings(rig: S8Rig) -> None:
    two_hours = await drop(rig, b"orphelin de deux heures", OLD / 12)

    await rig.cycle()  # délai par défaut : 24 h
    assert two_hours.is_file()

    report = await rig.cycle(settings=settings_with(blob_orphan_grace_seconds=3600))

    assert report.count(K.ORPHAN_BLOB) == 1
    assert not two_hours.exists()


# --- temporaires de dépôt ------------------------------------------------------------


async def test_the_cycle_deletes_abandoned_uploads_and_keeps_running_ones(
    rig: S8Rig,
) -> None:
    await rig.s7.publish(QA_SHEET)
    abandoned = rig.stage_upload("upload-abandoned.part", age=OLD)
    running = rig.stage_upload("upload-running.part", age=YOUNG)

    report = await rig.cycle()

    assert report.count(K.STALE_UPLOAD) == 1
    assert not abandoned.exists()
    assert running.is_file()
    assert blob_path(rig.blob_root, QA_SHEET).is_file()


async def test_the_upload_grace_period_is_the_one_of_the_settings(rig: S8Rig) -> None:
    upload = rig.stage_upload("upload-two-hours.part", age=OLD / 12)

    await rig.cycle()
    assert upload.is_file()

    report = await rig.cycle(settings=settings_with(blob_orphan_grace_seconds=3600))

    assert report.count(K.STALE_UPLOAD) == 1
    assert not upload.exists()


async def test_a_volume_without_staging_directory_is_not_an_error(rig: S8Rig) -> None:
    assert not (rig.blob_root / "tmp").exists()

    report = await rig.cycle()

    assert report.count(K.STALE_UPLOAD) == 0


# --- clés d'idempotence (O15) --------------------------------------------------------


async def test_expired_idempotency_keys_are_purged_and_recent_ones_kept(
    rig: S8Rig,
) -> None:
    await rig.add_idempotency_key("vieille", age=OLD)
    await rig.add_idempotency_key("recente", age=YOUNG)

    report = await rig.cycle()

    assert report.count(K.EXPIRED_IDEMPOTENCY_KEY) == 1
    assert await rig.idempotency_keys() == {"recente"}


async def test_the_idempotency_ttl_is_the_one_of_the_settings(rig: S8Rig) -> None:
    await rig.add_idempotency_key("deux-heures", age=OLD / 12)

    await rig.cycle()  # 24 h par défaut
    assert await rig.idempotency_keys() == {"deux-heures"}

    report = await rig.cycle(settings=settings_with(idempotency_key_ttl_seconds=3600))

    assert report.count(K.EXPIRED_IDEMPOTENCY_KEY) == 1
    assert await rig.idempotency_keys() == set()


async def test_purging_a_key_does_not_let_the_same_content_be_imported_twice(
    rig: S8Rig,
) -> None:
    imports = rig.s7.indexing.extraction.imports
    first = await imports.post_document(
        QA_SHEET,
        kind="QA_SHEET",
        title="Fiche",
        filename="fiche.md",
        idempotency_key="cle-1",
    )
    assert first.status_code == 202
    await rig.execute(
        "UPDATE knowledge_base.idempotency_keys "
        "SET created_at = now() - interval '2 days'"
    )

    await rig.cycle()
    again = await imports.post_document(
        QA_SHEET,
        kind="QA_SHEET",
        title="Fiche",
        filename="fiche.md",
        idempotency_key="cle-1",
    )

    assert again.status_code == 200
    assert again.json()["document_id"] == first.json()["document_id"]
    assert len(await imports.documents()) == 1


# --- idempotence du cycle ------------------------------------------------------------


async def test_a_second_cycle_has_nothing_left_to_clean(rig: S8Rig) -> None:
    await drop(rig, b"orphelin ancien", OLD)
    rig.stage_upload("upload-abandoned.part", age=OLD)
    await rig.add_idempotency_key("vieille", age=OLD)
    first = await rig.cycle()

    second = await rig.cycle()

    assert not first.clean
    assert second.clean, second

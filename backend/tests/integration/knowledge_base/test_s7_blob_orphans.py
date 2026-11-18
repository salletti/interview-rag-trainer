"""Nettoyage des blobs orphelins (S7 ; ADR 0012 points 3 et 6), sur le vrai
volume (``FileSystemBlobStore``) et les versions de PostgreSQL.

L'écriture du blob précède la transaction d'import : un échec laisse un fichier
sans version. ``CleanBlobOrphansHandler`` (composé par
``composition.build_clean_blob_orphans_handler(session_factory, settings)``)
supprime les fichiers plus vieux que le délai de grâce que plus aucun document
non purgé ne référence. Le déclenchement périodique est celui de S8.

Un blob « partagé » (référencé par la version d'un autre document) ne peut pas
être fabriqué ici : ``UNIQUE(content_hash)`` (O2) l'interdit en base. Le cas est
couvert avec des doubles (``test_purge_document``, ``test_clean_blob_orphans``) ;
ici, les cas réalisables : référencé par un document actif, par un document
retiré non purgé, déposé par un import encore en cours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
from tests.integration.knowledge_base.s7_support import (
    GRACE,
    PYDANTIC_SHEET,
    S7Rig,
    age,
    open_s7_rig,
)

pytestmark = pytest.mark.integration

OLD = GRACE + timedelta(hours=1)
YOUNG = timedelta(minutes=5)


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S7Rig]:
    async with open_s7_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def drop(rig: S7Rig, content: bytes, by: timedelta) -> Path:
    """Fichier déposé sans version (``put`` seul), daté de ``by``."""

    async def one_chunk() -> AsyncIterator[bytes]:
        yield content

    await FileSystemBlobStore(rig.blob_root).put(one_chunk(), max_bytes=64 * 1024)
    path = blob_path(rig.blob_root, content)
    age(path, by)
    return path


async def test_an_old_orphan_is_deleted_and_a_young_one_is_kept(rig: S7Rig) -> None:
    old = await drop(rig, b"orphelin ancien", OLD)
    young = await drop(rig, b"orphelin recent", YOUNG)

    await rig.clean_orphans()

    assert not old.exists()
    assert young.is_file()


async def test_the_blobs_of_documents_are_kept_however_old_they_are(
    rig: S7Rig,
) -> None:
    await rig.publish(QA_SHEET, title="Active")
    withdrawn = await rig.publish(PYDANTIC_SHEET, title="Retirée")
    await rig.withdraw(withdrawn.document_id)  # retirée, pas encore purgée
    for content in (QA_SHEET, PYDANTIC_SHEET):
        age(blob_path(rig.blob_root, content), OLD)
    orphan = await drop(rig, b"orphelin ancien", OLD)

    await rig.clean_orphans()

    assert not orphan.exists()
    assert blob_path(rig.blob_root, QA_SHEET).is_file()
    assert blob_path(rig.blob_root, PYDANTIC_SHEET).is_file()


async def test_a_blob_dropped_by_a_running_import_survives_until_it_is_referenced(
    rig: S7Rig,
) -> None:
    path = await drop(rig, QA_SHEET_V2, YOUNG)  # l'import n'a pas encore commité
    await rig.clean_orphans()
    assert path.is_file()

    # L'import aboutit (le dépôt est idempotent) : la version référence le blob.
    imports = rig.indexing.extraction.imports
    response = await imports.post_document(
        QA_SHEET_V2, kind="QA_SHEET", title="Arrivée", filename="fiche.md"
    )
    assert response.status_code == 202, response.text
    age(path, OLD)

    await rig.clean_orphans()

    assert path.is_file()


async def test_the_blob_redeposited_by_a_refused_reimport_of_purged_content_is_cleaned(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    await rig.withdraw(mine.document_id)
    await rig.run_purge(mine.document_id)
    path = blob_path(rig.blob_root, QA_SHEET)
    assert not path.exists()

    refused = await rig.indexing.extraction.imports.post_document(
        QA_SHEET, kind="QA_SHEET", title="Retour", filename="fiche.md"
    )
    assert refused.status_code == 409
    assert path.is_file(), "le dépôt précède la transaction : le fichier revient"
    age(path, OLD)

    await rig.clean_orphans()

    assert not path.exists()


async def test_a_second_cleaning_changes_nothing(rig: S7Rig) -> None:
    await rig.publish(QA_SHEET, title="Active")
    young = await drop(rig, b"orphelin recent", YOUNG)
    await drop(rig, b"orphelin ancien", OLD)
    await rig.clean_orphans()
    before = sorted(rig.indexing.extraction.imports.blob_files())

    await rig.clean_orphans()

    assert sorted(rig.indexing.extraction.imports.blob_files()) == before
    assert young in before
    assert blob_path(rig.blob_root, QA_SHEET) in before

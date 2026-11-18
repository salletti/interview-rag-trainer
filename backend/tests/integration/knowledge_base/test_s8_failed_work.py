"""Items ``FAILED`` et état métier (S8 ; O14 ; ADR 0004 points 7, 8, 10 ;
knowledge-base.md §5.2, §5.3), sur PostgreSQL réel.

Quand un item termine ``FAILED`` sans que le hook métier ait pu s'exécuter
(``recover`` : ``LEASE_EXPIRED_MAX_ATTEMPTS`` ; hook en erreur à la dernière
tentative : ``TERMINAL_HOOK_FAILED``), l'agrégat reste ``RECEIVED`` (version) ou
``PENDING`` (indexation) pour toujours. Recommandation retenue pour O14 : la
réconciliation écrit l'échec métier à la place du hook, avec le code de
l'item et ``retryable`` d'après le catalogue des erreurs.

Contrat vérifié :

- item ``extract_version`` ``FAILED`` + version ``RECEIVED`` -> version
  ``FAILED`` (code, message, ``retryable = is_replayable(code)``) ;
- item ``index_version`` ``FAILED`` + indexation ``PENDING`` -> indexation
  ``FAILED`` (idem), la version reste ``EXTRACTED`` (§5.2) ;
- l'item n'est jamais rouvert par la réconciliation : seule une commande
  explicite (``RetryIngestion``) rejoue une extraction ou une indexation ;
- erreur déterministe : UNE tentative, ``FAILED``, jamais de boucle, ni par la
  work queue ni par la réconciliation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from knowledge_base.domain.services.reconciliation import DiscrepancyKind as K
from knowledge_base.domain.value_objects.error_catalog import is_replayable
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
from tests.integration.knowledge_base.s8_support import S8Rig, open_s8_rig

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


@pytest.mark.parametrize(
    "code",
    [
        "LEASE_EXPIRED_MAX_ATTEMPTS",
        "TERMINAL_HOOK_FAILED",
        "BLOB_MISSING",
        "EMPTY_DOCUMENT",
        "INVALID_DOCUMENT",
    ],
)
async def test_an_extraction_failed_without_its_hook_marks_the_version_failed(
    rig: S8Rig, code: str
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(
        QA_SHEET, kind="QA_SHEET"
    )
    await rig.fail_item(imported.item_id, code, message="diagnostic de l'item")
    before = await rig.snapshot()

    report = await rig.cycle()

    assert report.count(K.FAILED_WORK_WITHOUT_STATE) == 1
    version = await rig.version(imported.version_id)
    assert version["status"] == "FAILED"
    assert version["failure_code"] == code
    assert version["failure_retryable"] is is_replayable(code)
    assert "diagnostic de l'item" in version["failure_message"]
    assert await rig.snapshot() == before, "l'item n'est ni rouvert ni modifié"
    assert (await rig.cycle()).clean


async def test_an_indexation_failed_without_its_hook_marks_the_indexation_failed(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)
    item = await rig.item_of("index_version", imported.version_id)
    await rig.fail_item(item["id"], "LEASE_EXPIRED_MAX_ATTEMPTS")
    before = await rig.snapshot()

    report = await rig.cycle()

    assert report.count(K.FAILED_WORK_WITHOUT_STATE) == 1
    indexation = await rig.indexation(imported.version_id)
    assert indexation["status"] == "FAILED"
    assert indexation["failure_code"] == "LEASE_EXPIRED_MAX_ATTEMPTS"
    assert indexation["failure_retryable"] is True
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
    assert await rig.snapshot() == before
    assert (await rig.cycle()).clean


async def test_a_failure_the_aggregate_already_knows_is_not_reported(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(b"\n \n")
    assert await rig.s7.indexing.extraction.run(imported.item_id) == "failed"
    version = await rig.version(imported.version_id)
    assert version["status"] == "FAILED"

    report = await rig.cycle()

    assert report.clean, report
    assert await rig.version(imported.version_id) == version


async def test_the_failed_item_of_a_discarded_version_is_left_alone(rig: S8Rig) -> None:
    old = await rig.s7.indexing.extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(old.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    await rig.s7.post_version(old.document_id, QA_SHEET_V2)  # v1 -> DISCARDED
    assert (await rig.version(old.version_id))["status"] == "DISCARDED"

    report = await rig.cycle()

    assert report.count(K.FAILED_WORK_WITHOUT_STATE) == 0
    assert (await rig.version(old.version_id))["status"] == "DISCARDED"


# --- erreur déterministe : aucune boucle ---------------------------------------------


async def test_a_deterministic_error_runs_once_and_nothing_ever_reopens_it(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(b"\n \n")

    assert await rig.s7.indexing.extraction.run(imported.item_id) == "failed"
    await rig.settle()
    await rig.settle()

    item = await rig.s7.indexing.extraction.item(imported.item_id)
    assert (item["status"], item["attempts"]) == ("FAILED", 1)
    assert item["last_error_code"] == "EMPTY_DOCUMENT"
    assert await rig.items(status="PENDING") == []
    version = await rig.version(imported.version_id)
    assert (version["status"], version["failure_retryable"]) == ("FAILED", False)


async def test_a_replayable_failure_waits_for_an_explicit_retry_and_never_loops(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(
        QA_SHEET, kind="QA_SHEET"
    )
    blob_path(rig.blob_root, QA_SHEET).unlink()  # volume restauré à moitié

    assert await rig.s7.indexing.extraction.run(imported.item_id) == "failed"
    await rig.settle()

    item = await rig.s7.indexing.extraction.item(imported.item_id)
    assert (item["status"], item["attempts"]) == ("FAILED", 1)
    assert item["last_error_code"] == "BLOB_MISSING"
    version = await rig.version(imported.version_id)
    assert (version["status"], version["failure_retryable"]) == ("FAILED", True)
    assert await rig.items(status="PENDING") == []

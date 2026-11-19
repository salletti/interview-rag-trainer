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
  work queue ni par la réconciliation ;
- adoption O14 et ``RetryIngestion`` simultanés (deux sessions, barrière dans
  la transaction de l'un) : l'item est verrouillé, l'un passe entièrement
  avant l'autre, jamais d'état mêlé (item rouvert ET version ``FAILED``)
  (revue S8, M1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from knowledge_base.domain.services.reconciliation import DiscrepancyKind as K
from knowledge_base.domain.value_objects.error_catalog import is_replayable
from knowledge_base.use_cases.reconcile_knowledge_base.failed_work_reconciliation import (  # noqa: E501
    FailedWorkReconciliation,
)
from knowledge_base.use_cases.retry_ingestion import retry_ingestion_handler
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
from tests.integration.knowledge_base.s7_support import TIMEOUT
from tests.integration.knowledge_base.s8_support import PREFIX, S8Rig, open_s8_rig

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


# --- adoption O14 et rejeu simultanés (revue S8, M1) ---------------------------------


class Barrier:
    """Arrête une transaction après son écriture, avant son commit."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def holding(
        self, step: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        async def held(*args: Any) -> Any:
            result = await step(*args)
            self.reached.set()
            async with asyncio.timeout(TIMEOUT):
                await self.release.wait()
            return result

        return held


async def still_waiting(task: asyncio.Task[Any]) -> bool:
    """La tâche concurrente est bloquée (verrou de l'item), pas finie."""
    await asyncio.sleep(0.3)
    return not task.done()


async def test_an_adoption_holding_the_item_makes_a_concurrent_retry_wait(
    rig: S8Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    barrier = Barrier()
    adopt = FailedWorkReconciliation._fail_version
    monkeypatch.setattr(
        FailedWorkReconciliation, "_fail_version", staticmethod(barrier.holding(adopt))
    )

    cycle = asyncio.create_task(rig.cycle())
    async with asyncio.timeout(TIMEOUT):
        await barrier.reached.wait()
    retry = asyncio.create_task(
        rig.http.post(f"{PREFIX}/failures/{imported.item_id}/retry")
    )
    try:
        assert await still_waiting(retry)
        # Adoption écrite, pas commitée : rien n'est visible de l'extérieur.
        assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
        assert (await extraction.item(imported.item_id))["status"] == "FAILED"
    finally:
        barrier.release.set()
    report = await cycle
    response = await retry

    # L'adoption est passée entière, puis le rejeu l'a vue et défaite.
    assert report.count(K.FAILED_WORK_WITHOUT_STATE) == 1
    assert response.status_code == 202, response.text
    version = await rig.version(imported.version_id)
    assert (version["status"], version["failure_code"]) == ("RECEIVED", None)
    assert (await extraction.item(imported.item_id))["status"] == "PENDING"


async def test_a_retry_holding_the_item_makes_a_concurrent_adoption_give_up(
    rig: S8Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    barrier = Barrier()
    reset = retry_ingestion_handler._reset_extraction
    monkeypatch.setattr(
        retry_ingestion_handler, "_reset_extraction", barrier.holding(reset)
    )

    retry = asyncio.create_task(
        rig.http.post(f"{PREFIX}/failures/{imported.item_id}/retry")
    )
    async with asyncio.timeout(TIMEOUT):
        await barrier.reached.wait()
    cycle = asyncio.create_task(rig.cycle())
    try:
        assert await still_waiting(cycle)
    finally:
        barrier.release.set()
    response = await retry
    report = await cycle

    # Le rejeu est passé entier ; l'adoption a trouvé l'item rouvert et n'a
    # rien écrit.
    assert response.status_code == 202, response.text
    assert report.count(K.FAILED_WORK_WITHOUT_STATE) == 0
    version = await rig.version(imported.version_id)
    assert (version["status"], version["failure_code"]) == ("RECEIVED", None)
    assert (await extraction.item(imported.item_id))["status"] == "PENDING"

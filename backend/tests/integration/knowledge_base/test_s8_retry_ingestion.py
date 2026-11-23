"""``RetryIngestion`` (S8 ; knowledge-base.md §5.2, §5.3, §6 ; ADR 0004 points
10 et 11 : un item ``FAILED`` est consultable ET relançable ; S0 §2 et §3 :
transition ``reopen``), par l'API réelle sur PostgreSQL, Qdrant et work queue.

Contrat vérifié :

- ``POST /api/v1/knowledge-base/failures/{item_id}/retry`` -> 202 ``{"item_id",
  "kind", "status": "PENDING"}`` ; l'item ``FAILED`` est ROUVERT (et non
  remplacé : ``dedup_key`` unique pour toujours) : ``PENDING``, ``attempts = 0``,
  disponible tout de suite, ``dispatched_at`` et ``completed_at`` effacés,
  dernière erreur CONSERVÉE (``last_error_*``), jeton de fencing jamais reculé.
- Dans la MÊME transaction, l'état métier : version ``FAILED`` -> ``RECEIVED``
  (``retry_extraction``, cause effacée), indexation ``FAILED`` -> ``PENDING``
  (``retry``) ; une version encore ``RECEIVED`` ou une indexation encore
  ``PENDING`` (hook jamais exécuté, O14) : seul l'item est rouvert ;
  ``purge_document`` : l'item seul.
- Refus, sans aucune écriture : 404 ``FAILURE_NOT_FOUND`` ; 409 ``NOT_FAILED``
  (avec ``status``) ; 409 ``NOT_REPLAYABLE`` (avec ``failure_code``) pour un
  code que le catalogue dit non rejouable ou ne connaît pas ; 409
  ``OBSOLETE_WORK`` pour une version supplantée (``DISCARDED``) ou un document
  retiré.
- Deux rejeux simultanés : un seul aboutit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import Response

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
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


async def retry(rig: S8Rig, item_id: UUID | str) -> Response:
    return await rig.http.post(f"{PREFIX}/failures/{item_id}/retry")


async def restore(rig: S8Rig, content: bytes) -> None:
    async def one_chunk() -> AsyncIterator[bytes]:
        yield content

    await FileSystemBlobStore(rig.blob_root).put(one_chunk(), max_bytes=64 * 1024)


# --- rejeux qui aboutissent ----------------------------------------------------------


async def test_retrying_a_missing_blob_failure_extracts_and_publishes_the_version(
    rig: S8Rig,
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    blob_path(rig.blob_root, QA_SHEET).unlink()
    assert await extraction.run(imported.item_id) == "failed"
    failed = await rig.version(imported.version_id)
    assert (failed["status"], failed["failure_code"]) == ("FAILED", "BLOB_MISSING")
    assert failed["failure_retryable"] is True
    token_before = (await extraction.item(imported.item_id))["fencing_token"]
    await restore(rig, QA_SHEET)  # le volume est réparé

    response = await retry(rig, imported.item_id)

    assert response.status_code == 202, response.text
    assert response.json() == {
        "item_id": str(imported.item_id),
        "kind": "extract_version",
        "status": "PENDING",
    }
    reopened = await extraction.item(imported.item_id)
    assert (reopened["status"], reopened["attempts"]) == ("PENDING", 0)
    assert reopened["dispatched_at"] is None
    assert reopened["completed_at"] is None
    assert reopened["last_error_code"] == "BLOB_MISSING"
    assert reopened["fencing_token"] >= token_before
    version = await rig.version(imported.version_id)
    assert version["status"] == "RECEIVED"
    assert version["failure_code"] is None
    assert await extraction.run(imported.item_id) == "succeeded"
    await rig.settle()
    assert (await rig.version(imported.version_id))["status"] == "PUBLISHED"
    assert await rig.found() == {imported.document_id}


async def test_retrying_an_index_failure_reopens_the_indexation_not_the_extraction(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)
    collection = f"kb_index_{rig.index_id.hex}"
    await rig.s7.indexing.qdrant.delete_collection(collection)  # collection perdue
    assert await rig.indexing.run_index(imported.version_id, rig.index_id) == "failed"
    indexation = await rig.indexation(imported.version_id)
    assert (indexation["status"], indexation["failure_code"]) == (
        "FAILED",
        "COLLECTION_MISMATCH",
    )
    assert indexation["failure_retryable"] is True
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
    await rig.vector.ensure_collection(rig.index_id, rig.indexing.profile)  # réparée
    item = await rig.item_of("index_version", imported.version_id)

    response = await retry(rig, item["id"])

    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "index_version"
    reopened = await rig.indexation(imported.version_id)
    assert (reopened["status"], reopened["failure_code"]) == ("PENDING", None)
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
    assert (
        await rig.indexing.run_index(imported.version_id, rig.index_id) == "succeeded"
    )
    await rig.settle()
    assert (await rig.version(imported.version_id))["status"] == "PUBLISHED"
    assert await rig.found() == {imported.document_id}


async def test_retrying_a_failed_purge_reopens_the_item_and_the_purge_completes(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.s7.withdraw(imported.document_id)
    (purge,) = await rig.items("purge_document")
    await rig.fail_item(purge["id"], "UNEXPECTED_ERROR", message="Qdrant injoignable")

    response = await retry(rig, purge["id"])

    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "purge_document"
    assert await rig.s7.run_purge(imported.document_id) == "succeeded"
    assert await rig.s7.points_of(imported.document_id) == []


async def test_a_retry_works_when_the_failure_hook_never_ran(rig: S8Rig) -> None:
    """O14 : item ``FAILED`` par ``recover``, version restée ``RECEIVED``."""
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")

    response = await retry(rig, imported.item_id)

    assert response.status_code == 202, response.text
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
    assert await extraction.run(imported.item_id) == "succeeded"
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"


# --- refus ---------------------------------------------------------------------------


async def test_a_deterministic_failure_cannot_be_replayed(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(b"\n \n")
    assert await extraction.run(imported.item_id) == "failed"
    before = await extraction.item(imported.item_id)

    response = await retry(rig, imported.item_id)

    assert response.status_code == 409, response.text
    body = response.json()
    assert (body["code"], body["failure_code"]) == ("NOT_REPLAYABLE", "EMPTY_DOCUMENT")
    assert await extraction.item(imported.item_id) == before
    assert (await rig.version(imported.version_id))["status"] == "FAILED"


async def test_a_failure_with_a_code_unknown_to_the_catalog_is_not_replayable(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(QA_SHEET)
    await rig.fail_item(imported.item_id, "CODE_D_UNE_VERSION_FUTURE")

    response = await retry(rig, imported.item_id)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "NOT_REPLAYABLE"
    assert (await rig.s7.indexing.extraction.item(imported.item_id))["status"] == (
        "FAILED"
    )


async def test_an_unknown_item_is_a_404(rig: S8Rig) -> None:
    response = await retry(rig, uuid4())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "FAILURE_NOT_FOUND"


@pytest.mark.parametrize("state", ["PENDING", "SUCCEEDED"])
async def test_an_item_that_did_not_fail_is_not_reopened(
    rig: S8Rig, state: str
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    if state == "SUCCEEDED":
        assert await extraction.run(imported.item_id) == "succeeded"
    before = await extraction.item(imported.item_id)

    response = await retry(rig, imported.item_id)

    assert response.status_code == 409, response.text
    assert (response.json()["code"], response.json()["status"]) == (
        "NOT_FAILED",
        state,
    )
    assert await extraction.item(imported.item_id) == before


async def test_a_second_retry_finds_the_item_already_reopened(rig: S8Rig) -> None:
    imported = await rig.s7.indexing.extraction.import_document(QA_SHEET)
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")

    first = await retry(rig, imported.item_id)
    second = await retry(rig, imported.item_id)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["code"] == "NOT_FAILED"


async def test_two_simultaneous_retries_reopen_the_item_once(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    blob_path(rig.blob_root, QA_SHEET).unlink()
    assert await extraction.run(imported.item_id) == "failed"
    await restore(rig, QA_SHEET)
    assert (await rig.version(imported.version_id))["status"] == "FAILED"

    responses = await asyncio.gather(
        retry(rig, imported.item_id), retry(rig, imported.item_id)
    )

    assert sorted(r.status_code for r in responses) == [202, 409]
    assert len(await rig.items("extract_version")) == 1
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
    assert await extraction.run(imported.item_id) == "succeeded"


async def test_a_superseded_version_is_not_replayed(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    old = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(old.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    await rig.s7.post_version(old.document_id, QA_SHEET_V2)  # v1 -> DISCARDED
    before = await extraction.item(old.item_id)

    response = await retry(rig, old.item_id)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "OBSOLETE_WORK"
    assert await extraction.item(old.item_id) == before
    assert (await rig.version(old.version_id))["status"] == "DISCARDED"


async def test_a_withdrawn_document_is_not_replayed(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    await rig.s7.withdraw(imported.document_id)

    response = await retry(rig, imported.item_id)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "OBSOLETE_WORK"
    assert (await extraction.item(imported.item_id))["status"] == "FAILED"


async def test_an_invalid_identifier_is_a_422(rig: S8Rig) -> None:
    assert (await rig.http.post(f"{PREFIX}/failures/nope/retry")).status_code == 422

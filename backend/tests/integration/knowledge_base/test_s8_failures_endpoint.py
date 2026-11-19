"""Consultation des échecs (S8 ; roadmap S8 ; ADR 0004 point 11 / O10 : « l'état
``FAILED`` en base joue le rôle de dead-letter, consultable et relançable »),
par l'API réelle.

O10 est TRANCHÉE par l'ADR 0004 (point 11) : pas de DLQ RabbitMQ en V1 ; rien
n'est perdu puisque le message ne porte qu'un identifiant et que l'item reste
en base. Ce que S8 doit donc livrer, et que ces tests vérifient : un item
``FAILED`` est LISTÉ avec son code, son message et ses tentatives, et il est
rejouable (``test_s8_retry_ingestion``).

``GET /api/v1/knowledge-base/failures`` -> ``{"items": [...]}`` : uniquement les
items ``FAILED`` (jamais ``PENDING``, ``SUCCEEDED``, ``CANCELLED``), du plus
récent au plus ancien (``failed_at`` décroissant), au plus ``limit``. Champs :
``item_id``, ``kind``, ``code``, ``message``, ``attempts``, ``max_attempts``,
``failed_at``, ``replayable``, ``document_id``, ``version_id``, ``index_id``
(lus du payload de l'item, ``null`` s'ils n'y figurent pas).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
from tests.integration.knowledge_base.s8_support import PREFIX, S8Rig, open_s8_rig

pytestmark = pytest.mark.integration

OTHER_SHEET = b"# Autre\n\n## Question : Q ?\n\nR.\n"
OTHER_SHEET_V2 = b"# Autre 2\n\n## Question : Q2 ?\n\nR2.\n"
WITHDRAWN_SHEET = b"# Z\n\n## Question : Z ?\n\nZ.\n"
LOST_SHEET = b"# Perdu\n\n## Question : P ?\n\nP.\n"


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


async def failures(rig: S8Rig, **params: int) -> list[dict[str, object]]:
    response = await rig.http.get(f"{PREFIX}/failures", params=params)
    assert response.status_code == 200, response.text
    items: list[dict[str, object]] = response.json()["items"]
    return items


async def test_without_any_failure_the_list_is_empty(rig: S8Rig) -> None:
    await rig.s7.publish(QA_SHEET)

    assert await failures(rig) == []


async def test_a_deterministic_extraction_failure_is_listed_with_its_diagnostic(
    rig: S8Rig,
) -> None:
    extraction = rig.s7.indexing.extraction
    imported = await extraction.import_document(b"\n \n")
    assert await extraction.run(imported.item_id) == "failed"
    stored = await extraction.item(imported.item_id)

    (listed,) = await failures(rig)

    assert listed["item_id"] == str(imported.item_id)
    assert listed["kind"] == "extract_version"
    assert listed["code"] == "EMPTY_DOCUMENT"
    assert listed["message"] == stored["last_error_message"]
    assert listed["message"]
    assert listed["attempts"] == 1
    assert isinstance(listed["max_attempts"], int)
    assert listed["max_attempts"] >= 1
    assert listed["replayable"] is False
    assert listed["document_id"] == str(imported.document_id)
    assert listed["version_id"] == str(imported.version_id)
    assert listed["index_id"] is None
    failed_at = datetime.fromisoformat(str(listed["failed_at"]))
    assert failed_at.tzinfo is not None
    assert failed_at == stored["completed_at"]


async def test_only_failed_items_are_listed_whatever_their_kind(rig: S8Rig) -> None:
    extraction = rig.s7.indexing.extraction
    await rig.s7.publish(QA_SHEET)  # des items SUCCEEDED
    await extraction.import_document(QA_SHEET_V2, kind="QA_SHEET")  # PENDING
    two_versions = await rig.s7.import_extract(OTHER_SHEET)
    await rig.s7.post_version(two_versions.document_id, OTHER_SHEET_V2)  # CANCELLED
    withdrawn = await rig.s7.import_extract(WITHDRAWN_SHEET)
    await rig.s7.withdraw(withdrawn.document_id)  # purge_document PENDING
    lost = await extraction.import_document(LOST_SHEET, kind="QA_SHEET")
    await rig.fail_item(lost.item_id, "BLOB_MISSING")
    (purge,) = await rig.items("purge_document", "PENDING")
    await rig.fail_item(purge["id"], "UNEXPECTED_ERROR")
    assert {"SUCCEEDED", "PENDING", "CANCELLED", "FAILED"} <= {
        i["status"] for i in await rig.items()
    }

    listed = await failures(rig)

    assert {(i["kind"], i["code"]) for i in listed} == {
        ("extract_version", "BLOB_MISSING"),
        ("purge_document", "UNEXPECTED_ERROR"),
    }
    by_kind = {i["kind"]: i for i in listed}
    assert by_kind["extract_version"]["replayable"] is True
    assert by_kind["extract_version"]["version_id"] == str(lost.version_id)
    assert by_kind["purge_document"]["replayable"] is True
    assert by_kind["purge_document"]["document_id"] == str(withdrawn.document_id)
    assert by_kind["purge_document"]["version_id"] is None


async def test_an_index_failure_carries_its_index(rig: S8Rig) -> None:
    imported = await rig.s7.import_extract(QA_SHEET)
    item = await rig.item_of("index_version", imported.version_id)
    await rig.fail_item(item["id"], "COLLECTION_MISMATCH")

    (listed,) = await failures(rig)

    assert listed["kind"] == "index_version"
    assert listed["index_id"] == str(rig.index_id)
    assert listed["version_id"] == str(imported.version_id)


async def test_a_code_unknown_to_the_catalog_is_listed_as_not_replayable(
    rig: S8Rig,
) -> None:
    imported = await rig.s7.indexing.extraction.import_document(QA_SHEET)
    await rig.fail_item(imported.item_id, "CODE_D_UNE_VERSION_FUTURE")

    (listed,) = await failures(rig)

    assert (listed["code"], listed["replayable"]) == (
        "CODE_D_UNE_VERSION_FUTURE",
        False,
    )


async def test_the_most_recent_failures_come_first_and_the_limit_applies(
    rig: S8Rig,
) -> None:
    extraction = rig.s7.indexing.extraction
    ids = []
    for n in range(3):
        imported = await extraction.import_document(
            f"# F{n}\n\n## Question : Q{n} ?\n\nR{n}.\n".encode(), kind="QA_SHEET"
        )
        await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
        ids.append(str(imported.item_id))

    everything = [i["item_id"] for i in await failures(rig)]
    limited = [i["item_id"] for i in await failures(rig, limit=2)]

    assert everything == ids[::-1]
    assert limited == ids[::-1][:2]


async def test_a_retried_failure_leaves_the_list(rig: S8Rig) -> None:
    imported = await rig.s7.indexing.extraction.import_document(QA_SHEET)
    await rig.fail_item(imported.item_id, "LEASE_EXPIRED_MAX_ATTEMPTS")
    assert len(await failures(rig)) == 1

    retried = await rig.http.post(f"{PREFIX}/failures/{imported.item_id}/retry")

    assert retried.status_code == 202, retried.text
    assert await failures(rig) == []


async def test_a_limit_out_of_range_is_a_422(rig: S8Rig) -> None:
    for limit in (0, -1):
        response = await rig.http.get(f"{PREFIX}/failures", params={"limit": limit})
        assert response.status_code == 422

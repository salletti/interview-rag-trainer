"""Slice S4, bout en bout : fichier Markdown importé par l'API de S2, item
``extract_version`` (S3), item ``index_version`` puis ``sync_visibility``, le tout
exécuté par ``execute_work_item`` (S0) sur PostgreSQL et Qdrant réels.

``hash`` : ``Embedder`` de test déterministe ; ``model`` : vrai modèle
``multilingual-e5-base`` (marqueur ``model``, ignoré sans ``sentence_transformers``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.domain.services.point_id import point_id
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import (
    EMBEDDER_PARAMS,
    QA_SHEET,
    QA_SHEET_V2,
    IndexingRig,
    make_embedder,
    open_indexing_rig,
    payload,
)

pytestmark = pytest.mark.integration


@pytest.fixture(params=EMBEDDER_PARAMS)
async def rig(
    request: pytest.FixtureRequest,
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[IndexingRig]:
    embedder = make_embedder(request.param)
    async with open_indexing_rig(
        integration_settings, session_factory, tmp_path, monkeypatch, embedder
    ) as opened:
        yield opened


async def test_a_markdown_version_goes_from_import_to_published(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()

    assert (await rig.indexation(imported.version_id, index_id))["status"] == "PENDING"
    assert await rig.run_index(imported.version_id, index_id) == "succeeded"

    version = await rig.extraction.version(imported.version_id)
    document = await rig.document(imported.document_id)
    chunks = await rig.extraction.chunks(imported.version_id)
    indexation = await rig.indexation(imported.version_id, index_id)
    expected = await rig.expected_manifest(imported.version_id, index_id)
    assert version["status"] == "PUBLISHED"
    assert document["published_version_id"] == imported.version_id
    assert document["visibility_revision"] == 1
    assert len(chunks) == 3
    assert indexation["status"] == "COMPLETED"
    assert indexation["point_count"] == 3
    assert indexation["manifest_digest"] == expected.digest
    assert indexation["completed_at"] is not None


async def test_the_points_follow_the_deterministic_identifiers_and_the_payload(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    await rig.run_index(imported.version_id, index_id)

    records = await rig.points(index_id)

    chunks = {c["id"]: c for c in await rig.extraction.chunks(imported.version_id)}
    assert {UUID(str(r.id)) for r in records} == {
        point_id(index_id, chunk_id) for chunk_id in chunks
    }
    for record in records:
        body = payload(record)
        chunk = chunks[UUID(body["chunk_id"])]
        assert UUID(str(record.id)) == point_id(index_id, chunk["id"])
        assert body["chunk_hash"] == chunk["chunk_hash"]
        assert body["unit_id"] == str(chunk["unit_id"])
        assert body["version_id"] == str(imported.version_id)
        assert body["document_id"] == str(imported.document_id)
        assert body["index_id"] == str(index_id)
        assert body["topics"] == ["python", "asyncio"]
        assert body["kind"] == "QA_SHEET"
        assert body["language"] == "fr"


async def test_points_stay_invisible_until_the_synchronization_runs(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    await rig.run_index(imported.version_id, index_id)

    assert [payload(r)["visible"] for r in await rig.points(index_id)] == [False] * 3

    assert await rig.run_sync(imported.document_id, 1) == "succeeded"

    assert [payload(r)["visible"] for r in await rig.points(index_id)] == [True] * 3


async def test_the_index_version_item_is_enqueued_by_the_extraction_not_before(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.extraction.import_document(QA_SHEET, kind="QA_SHEET")

    items = await rig.extraction.imports.rows(
        "SELECT * FROM knowledge_base.work_items WHERE kind = 'index_version'"
    )
    assert items == []

    assert await rig.extraction.run(imported.item_id) == "succeeded"

    (item,) = await rig.extraction.imports.rows(
        "SELECT * FROM knowledge_base.work_items WHERE kind = 'index_version'"
    )
    assert item["dedup_key"] == f"index_version:{imported.version_id}:{index_id}"
    assert item["status"] == "PENDING"


async def test_without_an_index_the_extraction_still_succeeds_and_nothing_is_indexed(
    rig: IndexingRig,
) -> None:
    imported = await rig.import_and_extract()

    assert (await rig.extraction.version(imported.version_id))["status"] == "EXTRACTED"
    assert await rig.extraction.imports.count("indexations") == 0


async def test_a_second_version_replaces_the_first_after_synchronization(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    first = await rig.import_and_extract()
    await rig.run_index(first.version_id, index_id)
    await rig.run_sync(first.document_id, 1)
    second = await rig.import_version_and_extract(first.document_id, QA_SHEET_V2)
    await rig.run_index(second.version_id, index_id)

    assert (await rig.extraction.version(first.version_id))["status"] == "SUPERSEDED"
    assert await rig.run_sync(first.document_id, 2) == "succeeded"

    records = await rig.points(index_id)
    assert {payload(r)["version_id"] for r in records} == {str(second.version_id)}
    assert len(records) == 2
    assert all(payload(r)["visible"] for r in records)

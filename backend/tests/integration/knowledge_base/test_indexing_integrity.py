"""Intégrité de l'index (knowledge-base.md §9 ; ADR 0010 §3) : un point perdu ou
modifié empêche ``COMPLETED`` ; une collection non conforme est un échec
TERMINAL ; la reprise après une divergence converge (PostgreSQL et Qdrant réels).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from qdrant_client.models import Distance, VectorParams

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import (
    EMBEDDER_PARAMS,
    IndexingRig,
    Tamper,
    TamperingVectorIndex,
    corrupt_one_hash,
    delete_one_point,
    make_embedder,
    open_indexing_rig,
    wait_for_backoff,
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


@pytest.mark.parametrize("tamper", [delete_one_point, corrupt_one_hash])
async def test_a_lost_or_modified_point_prevents_completed_then_the_retry_heals(
    rig: IndexingRig, tamper: Tamper
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    tampering = TamperingVectorIndex(rig.vector, rig.qdrant, tamper)
    handler = rig.index_handler_over(tampering)

    outcome = await rig.run_index(imported.version_id, index_id, handler)

    assert tampering.tampered
    assert outcome == "retry_scheduled"
    indexation = await rig.indexation(imported.version_id, index_id)
    assert indexation["status"] == "PENDING"
    assert indexation["manifest_digest"] is None
    version = await rig.extraction.version(imported.version_id)
    assert version["status"] == "EXTRACTED"
    assert (await rig.document(imported.document_id))["published_version_id"] is None

    await wait_for_backoff()
    assert await rig.run_index(imported.version_id, index_id, handler) == "succeeded"

    expected = await rig.expected_manifest(imported.version_id, index_id)
    assert (await rig.indexation(imported.version_id, index_id))[
        "manifest_digest"
    ] == expected.digest
    assert await rig.point_ids(index_id) == {p for p, _ in expected.entries}


@pytest.mark.parametrize(
    ("size_delta", "distance"),
    [(1, Distance.COSINE), (0, Distance.DOT)],
    ids=["wrong-dimension", "wrong-distance"],
)
async def test_a_nonconforming_collection_fails_terminally_without_writing(
    rig: IndexingRig, size_delta: int, distance: Distance
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    name = QdrantVectorIndex.collection_name(index_id)
    await rig.qdrant.delete_collection(name)
    await rig.qdrant.create_collection(
        collection_name=name,
        vectors_config=VectorParams(
            size=rig.profile.dimension + size_delta, distance=distance
        ),
    )

    outcome = await rig.run_index(imported.version_id, index_id)

    assert outcome == "failed"
    item = await rig.extraction.item(
        await rig.index_item(imported.version_id, index_id)
    )
    assert item["status"] == "FAILED"
    assert item["last_error_code"] == "COLLECTION_MISMATCH"
    indexation = await rig.indexation(imported.version_id, index_id)
    assert indexation["status"] == "FAILED"
    assert indexation["failure_code"] == "COLLECTION_MISMATCH"
    assert await rig.points(index_id) == []
    assert (await rig.extraction.version(imported.version_id))["status"] == "EXTRACTED"


async def test_a_missing_collection_fails_terminally(rig: IndexingRig) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    await rig.qdrant.delete_collection(QdrantVectorIndex.collection_name(index_id))

    outcome = await rig.run_index(imported.version_id, index_id)

    assert outcome == "failed"
    indexation = await rig.indexation(imported.version_id, index_id)
    assert indexation["status"] == "FAILED"
    assert indexation["failure_code"] == "COLLECTION_MISMATCH"

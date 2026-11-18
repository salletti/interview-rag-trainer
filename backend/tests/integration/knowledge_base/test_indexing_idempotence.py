"""Effets Qdrant rejoués : identifiants déterministes, écrasement sans
accumulation, reprise après une panne entre les upserts et la finalisation
(knowledge-base.md §7.3 ; S0 §6 ; ADR 0010 §2 et §9 ; PostgreSQL et Qdrant réels).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import (
    EMBEDDER_PARAMS,
    CrashBeforeFinalize,
    IndexingRig,
    make_embedder,
    open_indexing_rig,
    payload,
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


async def test_a_crash_between_the_upserts_and_the_finalization_leaves_points_only(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    crashing = cast(WorkHandler, CrashBeforeFinalize(rig.index_handler))

    outcome = await rig.run_index(imported.version_id, index_id, crashing)

    assert outcome == "retry_scheduled"
    assert len(await rig.points(index_id)) == 3
    assert (await rig.indexation(imported.version_id, index_id))["status"] == "PENDING"
    assert (await rig.extraction.version(imported.version_id))["status"] == "EXTRACTED"


async def test_the_retry_after_a_crash_completes_without_any_duplicate(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    crashing = cast(WorkHandler, CrashBeforeFinalize(rig.index_handler))
    await rig.run_index(imported.version_id, index_id, crashing)
    after_crash = await rig.point_ids(index_id)
    await wait_for_backoff()

    outcome = await rig.run_index(imported.version_id, index_id)

    expected = await rig.expected_manifest(imported.version_id, index_id)
    assert outcome == "succeeded"
    assert await rig.point_ids(index_id) == after_crash
    assert len(await rig.points(index_id)) == 3
    assert {p for p, _ in expected.entries} == after_crash
    indexation = await rig.indexation(imported.version_id, index_id)
    assert indexation["status"] == "COMPLETED"
    assert indexation["manifest_digest"] == expected.digest
    assert indexation["point_count"] == 3
    assert (await rig.extraction.version(imported.version_id))["status"] == "PUBLISHED"


async def test_two_crashes_then_a_success_give_the_same_points_and_manifest(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    crashing = cast(WorkHandler, CrashBeforeFinalize(rig.index_handler))
    await rig.run_index(imported.version_id, index_id, crashing)
    first = {str(r.id): payload(r)["chunk_hash"] for r in await rig.points(index_id)}
    await wait_for_backoff()
    await rig.run_index(imported.version_id, index_id, crashing)
    second = {str(r.id): payload(r)["chunk_hash"] for r in await rig.points(index_id)}
    await wait_for_backoff()

    await rig.run_index(imported.version_id, index_id)

    final = {str(r.id): payload(r)["chunk_hash"] for r in await rig.points(index_id)}
    assert first == second == final
    assert len(final) == 3


async def test_a_zombie_rewrite_after_the_finalization_leaves_one_point_per_chunk(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    imported = await rig.import_and_extract()
    await rig.run_index(imported.version_id, index_id)
    await rig.run_sync(imported.document_id, 1)
    before = await rig.point_ids(index_id)
    chunks = await rig.extraction.chunks(imported.version_id)

    # Tentative zombie : mêmes effets Qdrant rejoués après la finalisation.
    await rig.vector.upsert(
        index_id,
        [
            zombie_point(rig, index_id, imported.document_id, imported.version_id, c)
            for c in chunks
        ],
    )

    assert await rig.point_ids(index_id) == before
    assert len(await rig.points(index_id)) == 3
    # Le rejeu écrase les points de la version publiée en ``visible=false`` :
    # aucun doublon, mais le document manque des résultats (jamais l'inverse).
    # Rien ne replanifie ``sync_visibility`` : aucune synchronisation
    # « suivante » ne corrige cet état avant la réconciliation S8 (points d'une
    # version PUBLISHED de document ACTIVE avec visible=false -> replanifier
    # sync_visibility ; §7.3 condition 6). Depuis la revue S4, ``keeping_lease``
    # annule le travail au premier heartbeat refusé : seule une fenêtre
    # résiduelle (au plus un intervalle de heartbeat) permet ce rejeu.
    assert [payload(r)["visible"] for r in await rig.points(index_id)] == [False] * 3


def zombie_point(
    rig: IndexingRig,
    index_id: UUID,
    document_id: UUID,
    version_id: UUID,
    chunk: dict[str, Any],
) -> IndexedPoint:
    """Le point qu'une tentative zombie réécrirait pour ce chunk."""
    return IndexedPoint(
        point_id=point_id(index_id, chunk["id"]),
        vector=(*([0.0] * (rig.profile.dimension - 1)), 1.0),
        chunk_id=chunk["id"],
        unit_id=chunk["unit_id"],
        version_id=version_id,
        document_id=document_id,
        chunk_hash=chunk["chunk_hash"],
        index_id=index_id,
        visible=False,
        topics=("python", "asyncio"),
        kind="QA_SHEET",
        language="fr",
    )

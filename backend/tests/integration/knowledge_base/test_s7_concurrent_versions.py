"""Versions concurrentes (S7 ; knowledge-base.md §4.1 I4, I5, I6, §8, §10, §11),
sur PostgreSQL, Qdrant et work queue réels.

Scénarios :

1. N en cours d'indexation, N+1 importée, N termine : N n'est jamais publiée
   (invariant, vrai dès S4 par le garde-fou de ``CompleteIndexation``) ET
   l'import a annulé l'item et l'indexation de N, dont la tentative est périmée
   (S7) ; N+1 est ensuite publiée seule, et les points écrits par N après
   l'import ne sont jamais visibles puis disparaissent à la synchronisation.
2. Recherches pendant une republication : à aucun moment un résultat ne mêle
   des passages de deux versions du même document, y compris dans la fenêtre où
   Qdrant porte les deux versions visibles (entre ``set_visibility`` de la
   nouvelle et ``delete_points`` de l'ancienne), tenue ouverte par une barrière.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.adapters.controller.tasks.sync_visibility_handler import (
    build_sync_visibility_handler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import (
    QA_SHEET,
    QA_SHEET_V2,
    payload,
)
from tests.integration.knowledge_base.s7_support import (
    TIMEOUT,
    Background,
    GatedVectorIndex,
    S7Rig,
    background_tasks,
    open_s7_rig,
    query,
    versions_of,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def background(rig: S7Rig) -> AsyncIterator[Background]:
    """Détruit avant ``rig`` : aucune tâche ne survit au corpus."""
    async with background_tasks() as tasks:
        yield tasks


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


async def index_while_next_version_is_imported(
    rig: S7Rig, background: Background
) -> tuple[UUID, UUID, UUID, str]:
    """N indexe (pause avant son upsert Qdrant), N+1 est importée, N reprend et
    termine. Rend (document, N, N+1, résultat de la tentative de N)."""
    first = await rig.import_extract()
    gate = GatedVectorIndex(rig.indexing.vector, gate_on="upsert")
    attempt = background.spawn(
        rig.indexing.run_index(
            first.version_id, rig.index_id, rig.indexing.index_handler_over(gate)
        )
    )
    async with asyncio.timeout(TIMEOUT):
        await gate.reached.wait()
    second = await rig.post_version(first.document_id, QA_SHEET_V2)
    gate.release.set()
    outcome = await asyncio.wait_for(attempt, TIMEOUT)
    return first.document_id, first.version_id, second.version_id, str(outcome)


async def test_a_version_finishing_after_the_next_import_is_never_published(
    rig: S7Rig, background: Background
) -> None:
    document_id, first, _second, _outcome = await index_while_next_version_is_imported(
        rig, background
    )

    document = await rig.indexing.document(document_id)
    assert document["published_version_id"] is None
    assert document["visibility_revision"] == 0
    assert (await rig.indexing.extraction.version(first))["status"] == "DISCARDED"
    sync_items = [
        i for i in await rig.items(document_id) if i["kind"] == "sync_visibility"
    ]
    assert sync_items == []
    assert (await rig.search.search(query())).passages == ()


async def test_the_import_cancels_the_work_of_the_version_still_indexing(
    rig: S7Rig, background: Background
) -> None:
    document_id, first, _second, outcome = await index_while_next_version_is_imported(
        rig, background
    )

    assert outcome == "stale"
    indexation = await rig.indexing.indexation(first, rig.index_id)
    assert indexation["status"] == "CANCELLED"
    (item,) = [i for i in await rig.items(document_id) if i["kind"] == "index_version"]
    assert item["status"] == "CANCELLED"


async def test_the_points_written_by_the_late_version_stay_invisible_then_vanish(
    rig: S7Rig, background: Background
) -> None:
    document_id, first, second, _outcome = await index_while_next_version_is_imported(
        rig, background
    )
    late = await rig.points_of(document_id)
    assert late, "la tentative de N a écrit ses points après l'import de N+1"
    assert {payload(p)["version_id"] for p in late} == {str(first)}
    assert not any(payload(p)["visible"] for p in late)

    # N+1 suit son cours normal.
    await rig.extract(await rig.imported_version(document_id, second))
    assert await rig.indexing.run_index(second, rig.index_id) == "succeeded"
    await rig.synchronize(document_id)

    document = await rig.indexing.document(document_id)
    assert document["published_version_id"] == second
    remaining = await rig.points_of(document_id)
    assert {payload(p)["version_id"] for p in remaining} == {str(second)}
    assert all(payload(p)["visible"] for p in remaining)
    assert versions_of(await rig.search.search(query()), document_id) == {second}


async def test_searches_during_a_republication_never_mix_two_versions(
    rig: S7Rig, background: Background
) -> None:
    first = await rig.publish()
    second = await rig.post_version(first.document_id, QA_SHEET_V2)
    await rig.extract(second)
    document_id = first.document_id
    # La synchronisation s'arrête avant de supprimer les points de l'ancienne
    # version : Qdrant porte alors les deux versions visibles.
    gate = GatedVectorIndex(rig.indexing.vector, gate_on="delete_points")
    syncing = build_sync_visibility_handler(rig.session_factory, vector_index=gate)

    # (phase au lancement de la recherche, versions rendues pour le document)
    seen: list[tuple[str, set[UUID]]] = []
    phase = "before_publication"
    stop = asyncio.Event()
    in_window = asyncio.Event()  # assez de recherches lancées dans la fenêtre 2

    async def searcher() -> None:
        while not stop.is_set():
            started = phase  # lu AVANT de lancer la recherche
            found = versions_of(await rig.search.search(query()), document_id)
            seen.append((started, found))
            if sum(1 for p, _ in seen if p == "both_visible") >= 6:
                in_window.set()

    searchers = [background.spawn(searcher()) for _ in range(3)]

    # Fenêtre 1 : publication SQL commitée, synchronisation pas encore faite.
    assert await rig.indexing.run_index(second.version_id, rig.index_id) == "succeeded"
    phase = "published_not_synchronized"
    after_publication = await rig.search.search(query())
    assert first.version_id not in versions_of(after_publication, document_id)

    # Fenêtre 2 : les deux versions sont visibles dans Qdrant, SQL arbitre.
    revision = (await rig.indexing.document(document_id))["visibility_revision"]
    sync = background.spawn(
        rig.indexing.extraction.run(
            await rig.item_id(f"sync_visibility:{document_id}:{revision}"), syncing
        )
    )
    async with asyncio.timeout(TIMEOUT):
        await gate.reached.wait()
    phase = "both_visible"
    visible = {
        payload(p)["version_id"]
        for p in await rig.points_of(document_id)
        if payload(p)["visible"]
    }
    assert visible == {str(first.version_id), str(second.version_id)}
    async with asyncio.timeout(TIMEOUT):
        await in_window.wait()  # des recherches concurrentes ont tourné ici

    gate.release.set()
    assert await asyncio.wait_for(sync, TIMEOUT) == "succeeded"
    stop.set()
    await asyncio.gather(*searchers)

    assert all(len(versions) <= 1 for _, versions in seen), seen
    after_commit = [v for p, v in seen if p != "before_publication"]
    assert all(first.version_id not in versions for versions in after_commit), seen
    in_both_visible = [v for p, v in seen if p == "both_visible"]
    assert len(in_both_visible) >= 6
    assert all(versions == {second.version_id} for versions in in_both_visible), seen
    after_sync = await rig.search.search(query())
    assert versions_of(after_sync, document_id) == {second.version_id}
    assert {payload(p)["version_id"] for p in await rig.points_of(document_id)} == {
        str(second.version_id)
    }


async def test_the_other_documents_are_unaffected_by_a_republication(
    rig: S7Rig,
) -> None:
    other = await rig.publish(QA_SHEET.replace(b"FastAPI", b"Pydantic"), title="Autre")
    mine = await rig.publish()
    second = await rig.post_version(mine.document_id, QA_SHEET_V2)
    await rig.extract(second)
    assert await rig.indexing.run_index(second.version_id, rig.index_id) == "succeeded"
    await rig.synchronize(mine.document_id)

    result = await rig.search.search(query())

    assert versions_of(result, other.document_id) == {other.version_id}
    assert versions_of(result, mine.document_id) == {second.version_id}

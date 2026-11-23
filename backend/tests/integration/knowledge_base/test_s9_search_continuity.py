"""Recherche ininterrompue pendant la bascule (S9 ; roadmap « recherche
ininterrompue pendant la bascule » ; knowledge-base.md §13 ; ADR 0011
Conséquences : « la recherche lit toujours l'index ACTIVE dans PostgreSQL ») sur
PostgreSQL et Qdrant réels.

Une recherche tourne en boucle pendant que l'index est activé, sans `sleep` : la
boucle signale son premier résultat avant l'activation (``first``) et le test
attend qu'elle en ait rendu plusieurs APRÈS (``enough``). Contrat :

- aucune requête n'échoue, avant, pendant ni après le commit de l'activation ;
- chaque résultat est complet et cohérent : les mêmes documents, chacun dans sa
  version publiée, qu'il vienne de l'ancien ou du nouvel index ;
- la suite des index qui ont servi est monotone : l'ancien, puis le nouveau,
  jamais un retour en arrière ;
- supprimer ensuite la collection de l'ancien index ne dérange aucune requête.

Hors périmètre (décision à trancher, voir le rapport) : une requête commencée
AVANT le commit, qui interroge Qdrant APRÈS la suppression de la collection de
l'ancien index.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s7_support import Background, background_tasks
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    QUERY,
    TIMEOUT,
    S9Rig,
    documents_of,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S9Rig]:
    async with open_s9_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


@pytest.fixture
async def background(rig: S9Rig) -> AsyncIterator[Background]:
    """Détruit avant ``rig`` : aucune tâche ne survit au corpus."""
    async with background_tasks() as tasks:
        yield tasks


class SearchLoop:
    """Recherches séquentielles jusqu'à ``after`` résultats après ``marker`` (ou
    jusqu'à ``stop``)."""

    def __init__(self, rig: S9Rig, after: int = 3) -> None:
        self.results: list[KnowledgeSearchResult] = []
        self.first = asyncio.Event()
        self.marker = asyncio.Event()
        self.enough = asyncio.Event()
        self.stop = asyncio.Event()
        self._service = rig.search_service()
        self._after = after

    async def run(self) -> None:
        seen_after = 0
        while seen_after < self._after and not self.stop.is_set():
            self.results.append(
                await self._service.search(KnowledgeQuery(text=QUERY, limit=20))
            )
            self.first.set()
            if self.marker.is_set():
                seen_after += 1
        self.enough.set()


async def published_versions(rig: S9Rig) -> dict[UUID, UUID]:
    rows = await rig.rows(
        "SELECT id, published_version_id FROM knowledge_base.documents"
    )
    return {r["id"]: r["published_version_id"] for r in rows}


async def test_searches_during_the_activation_never_fail_and_stay_consistent(
    rig: S9Rig, background: Background
) -> None:
    documents = [
        (await rig.publish(long_sheet(tag), title=f"Fiche {tag}")).document_id
        for tag in ("a", "b", "c")
    ]
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    loop = SearchLoop(rig)
    task = background.spawn(loop.run())
    async with asyncio.timeout(TIMEOUT):
        await loop.first.wait()  # au moins un résultat de l'ancien index

    await rig.activate(new)
    loop.marker.set()
    async with asyncio.timeout(TIMEOUT):
        await loop.enough.wait()
    await task  # relève toute exception de la boucle

    published = await published_versions(rig)
    assert len(loop.results) >= 4
    for result in loop.results:
        assert result.index_id in {rig.index_id, new}
        assert documents_of(result) == set(documents)
        assert result.complete is True
        for passage in result.passages:
            assert (
                passage.citation.version_id == published[passage.citation.document_id]
            )
    served = [r.index_id for r in loop.results]
    assert served[0] == rig.index_id
    assert served[-1] == new
    switch = served.index(new)
    assert set(served[:switch]) == {rig.index_id}
    assert set(served[switch:]) == {new}


async def test_searches_during_the_build_are_served_by_the_active_index_only(
    rig: S9Rig, background: Background
) -> None:
    documents = [
        (await rig.publish(long_sheet(tag), title=f"Fiche {tag}")).document_id
        for tag in ("a", "b")
    ]
    loop = SearchLoop(rig, after=1_000_000)  # tourne jusqu'à ``stop``
    task = background.spawn(loop.run())
    async with asyncio.timeout(TIMEOUT):
        await loop.first.wait()

    new = await rig.start(PROFILE_B, POLICY_SAME)  # pendant que la boucle tourne
    await rig.drain(index_id=new)
    loop.stop.set()
    await task

    assert {r.index_id for r in loop.results} == {rig.index_id}
    assert all(documents_of(r) == set(documents) for r in loop.results)


async def test_dropping_the_old_collection_disturbs_no_search(
    rig: S9Rig, background: Background
) -> None:
    documents = [
        (await rig.publish(long_sheet(tag), title=f"Fiche {tag}")).document_id
        for tag in ("a", "b")
    ]
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    await rig.activate(new)
    loop = SearchLoop(rig, after=4)
    task = background.spawn(loop.run())
    async with asyncio.timeout(TIMEOUT):
        await loop.first.wait()  # recherches déjà servies par le nouvel index

    assert await rig.run_key(f"drop_index_collection:{rig.index_id}") == "succeeded"
    loop.marker.set()
    async with asyncio.timeout(TIMEOUT):
        await loop.enough.wait()
    await task

    assert not await rig.collection_exists(rig.index_id)
    assert {r.index_id for r in loop.results} == {new}
    assert all(documents_of(r) == set(documents) for r in loop.results)

"""Recherche groupée de ``QdrantVectorIndex`` sur un Qdrant réel (S5 ; port
``ChunkSearcher`` ; knowledge-base.md §12 étape 3).

Points synthétiques, sans modèle ni PostgreSQL : la requête est le vecteur
d'un point connu, ce qui fixe le classement en tête. Vérifie ce que le filtre
construit (testé unitairement) produit vraiment dans Qdrant : groupement par
unité, limite en unités distinctes, exclusion, ``visible`` toujours exigé,
sémantique des filtres de métadonnées.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from knowledge_base.domain.value_objects.search_filters import SearchFilters
from runtime.settings import Settings
from runtime.vector_store import create_qdrant_client
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE, hash_vector

pytestmark = pytest.mark.integration

DOC_FR, DOC_EN = UUID(int=0xF), UUID(int=0xE)


@pytest.fixture
async def qdrant(integration_settings: Settings) -> AsyncIterator[AsyncQdrantClient]:
    client = create_qdrant_client(integration_settings)
    yield client
    await client.close()


@pytest.fixture
async def index_id(qdrant: AsyncQdrantClient) -> AsyncIterator[UUID]:
    created = uuid4()
    await QdrantVectorIndex(qdrant).ensure_collection(created, PROFILE)
    yield created
    await qdrant.delete_collection(QdrantVectorIndex.collection_name(created))


def unit_id(unit: int) -> UUID:
    return UUID(int=1_000 + unit)


def chunk(
    index_id: UUID,
    unit: int,
    rank: int,
    *,
    document_id: UUID = DOC_FR,
    topics: tuple[str, ...] = ("python",),
    kind: str = "QA_SHEET",
    language: str = "fr",
) -> IndexedPoint:
    chunk_id = UUID(int=100_000 + unit * 100 + rank)
    return IndexedPoint(
        point_id=UUID(int=index_id.int ^ chunk_id.int),
        vector=tuple(hash_vector(f"{unit}:{rank}")),
        chunk_id=chunk_id,
        unit_id=unit_id(unit),
        version_id=UUID(int=document_id.int + 1),
        document_id=document_id,
        chunk_hash=f"h{unit}-{rank}",
        index_id=index_id,
        topics=topics,
        kind=kind,
        language=language,
    )


async def store_visible(
    adapter: QdrantVectorIndex, index_id: UUID, points: list[IndexedPoint]
) -> None:
    """Écrit les points (toujours invisibles) puis les rend visibles, version
    par version, comme ``sync_visibility``."""
    await adapter.upsert(index_id, points)
    for document_id, version_id in {(p.document_id, p.version_id) for p in points}:
        await adapter.set_visibility(
            index_id, document_id=document_id, version_id=version_id, visible=True
        )


def query(unit: int, rank: int = 0) -> list[float]:
    return hash_vector(f"{unit}:{rank}")


async def search(
    adapter: QdrantVectorIndex,
    index_id: UUID,
    vector: list[float],
    *,
    unit_limit: int = 10,
    filters: SearchFilters | None = None,
    excluded: tuple[UUID, ...] = (),
) -> list[tuple[UUID, UUID, float]]:
    hits = await adapter.search(
        index_id,
        vector,
        unit_limit=unit_limit,
        filters=filters or SearchFilters(),
        excluded_unit_ids=excluded,
    )
    return [(h.unit_id, h.chunk_id, h.score) for h in hits]


async def test_hits_are_grouped_by_unit_and_limited_in_distinct_units(
    qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    adapter = QdrantVectorIndex(qdrant)
    await store_visible(
        adapter,
        index_id,
        [chunk(index_id, u, r) for u in range(1, 6) for r in range(4)],
    )

    found = await search(adapter, index_id, query(2, 1), unit_limit=3)

    units = list(dict.fromkeys(u for u, _, _ in found))
    assert len(units) == 3
    assert units[0] == unit_id(2)
    # Le chunk de la requête est le premier de son unité, score maximal.
    _, first_chunk, first_score = found[0]
    assert first_chunk == chunk(index_id, 2, 1).chunk_id
    assert first_score == pytest.approx(1.0, abs=1e-5)
    # Au plus CHUNKS_PER_UNIT chunks par unité, tous de cette unité.
    for unit in units:
        assert 1 <= sum(1 for u, _, _ in found if u == unit) <= 3


async def test_excluded_units_are_never_returned(
    qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    adapter = QdrantVectorIndex(qdrant)
    await store_visible(adapter, index_id, [chunk(index_id, u, 0) for u in range(1, 6)])

    found = await search(adapter, index_id, query(1), excluded=(unit_id(1), unit_id(3)))

    assert {u for u, _, _ in found} == {unit_id(2), unit_id(4), unit_id(5)}


async def test_invisible_points_are_never_returned_even_as_best_match(
    qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    adapter = QdrantVectorIndex(qdrant)
    await store_visible(adapter, index_id, [chunk(index_id, 2, 0)])
    # Même document et même version, écrit APRÈS la synchronisation : un upsert
    # écrit toujours invisible, ce point le reste.
    await adapter.upsert(index_id, [chunk(index_id, 1, 0)])

    found = await search(adapter, index_id, query(1))

    assert {u for u, _, _ in found} == {unit_id(2)}


async def test_metadata_filters_select_documents(
    qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    adapter = QdrantVectorIndex(qdrant)
    await store_visible(
        adapter,
        index_id,
        [
            chunk(index_id, 1, 0, topics=("python", "asyncio"), kind="COURSE"),
            chunk(index_id, 2, 0, topics=("python",)),
            chunk(
                index_id, 3, 0, document_id=DOC_EN, language="en", topics=("docker",)
            ),
        ],
    )

    async def units(filters: SearchFilters) -> set[UUID]:
        return {
            u for u, _, _ in await search(adapter, index_id, query(1), filters=filters)
        }

    assert await units(SearchFilters(topics=("python", "asyncio"))) == {unit_id(1)}
    assert await units(SearchFilters(topics=("python",))) == {unit_id(1), unit_id(2)}
    assert await units(SearchFilters(kinds=("COURSE", "TECHNICAL"))) == {unit_id(1)}
    assert await units(SearchFilters(language="en")) == {unit_id(3)}
    assert await units(SearchFilters(document_ids=(DOC_EN,))) == {unit_id(3)}
    assert await units(SearchFilters(topics=("kubernetes",))) == set()

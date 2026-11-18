"""Contrat de l'adapter ``QdrantVectorIndex`` sur un Qdrant réel (S4 ;
knowledge-base.md §9 ; ADR 0010) : collection par index, index de payload,
upserts idempotents, relecture sans vecteurs, visibilité et suppressions par
filtre. Les points sont synthétiques : ni modèle, ni PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance as QdrantDistance
from qdrant_client.models import PayloadSchemaType, VectorParams

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.value_objects.embedding_profile import (
    Distance,
    EmbeddingProfile,
)
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from runtime.settings import Settings
from runtime.vector_store import create_qdrant_client
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE, hash_vector

pytestmark = pytest.mark.integration

DOC_A, DOC_B = UUID(int=0xA), UUID(int=0xB)
V1, V2, V3 = UUID(int=1), UUID(int=2), UUID(int=3)


@pytest.fixture
async def qdrant(integration_settings: Settings) -> AsyncIterator[AsyncQdrantClient]:
    client = create_qdrant_client(integration_settings)
    yield client
    await client.close()


@pytest.fixture
async def index_id(qdrant: AsyncQdrantClient) -> AsyncIterator[UUID]:
    created = uuid4()
    yield created
    await qdrant.delete_collection(QdrantVectorIndex.collection_name(created))


@pytest.fixture
def adapter(qdrant: AsyncQdrantClient) -> QdrantVectorIndex:
    return QdrantVectorIndex(qdrant)


def point(
    index_id: UUID,
    document_id: UUID,
    version_id: UUID,
    n: int,
    *,
    visible: bool = False,
    chunk_hash: str | None = None,
) -> IndexedPoint:
    # Le document entre dans l'identifiant : deux documents ont des chunks
    # distincts, donc des points distincts (sinon l'upsert de l'un écrase
    # l'autre).
    chunk_id = UUID(int=(document_id.int << 64) + (version_id.int << 16) + n)
    return IndexedPoint(
        point_id=UUID(int=index_id.int ^ chunk_id.int),
        vector=tuple(hash_vector(f"{version_id}:{n}")),
        chunk_id=chunk_id,
        unit_id=UUID(int=n),
        version_id=version_id,
        document_id=document_id,
        chunk_hash=chunk_hash or f"hash-{version_id.int}-{n}",
        index_id=index_id,
        visible=visible,
        topics=("python", "asyncio"),
        kind="COURSE",
        language="fr",
    )


async def visible_flags(qdrant: AsyncQdrantClient, index_id: UUID) -> dict[UUID, bool]:
    records, _ = await qdrant.scroll(
        QdrantVectorIndex.collection_name(index_id),
        limit=1000,
        with_payload=True,
    )
    return {UUID(str(r.id)): bool((r.payload or {})["visible"]) for r in records}


# --- collection -----------------------------------------------------------------------


async def test_ensure_collection_uses_the_dimension_and_distance_of_the_profile(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)

    info = await qdrant.get_collection(QdrantVectorIndex.collection_name(index_id))
    vectors = info.config.params.vectors
    assert isinstance(vectors, VectorParams)
    assert (vectors.size, vectors.distance) == (
        PROFILE.dimension,
        QdrantDistance.COSINE,
    )


async def test_ensure_collection_creates_the_payload_indexes_of_the_filters(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)

    info = await qdrant.get_collection(QdrantVectorIndex.collection_name(index_id))
    schema = {name: field.data_type for name, field in info.payload_schema.items()}
    assert schema["visible"] == PayloadSchemaType.BOOL
    for name in ("unit_id", "document_id", "version_id", "topics", "kind", "language"):
        assert schema[name] == PayloadSchemaType.KEYWORD, name


async def test_ensure_collection_twice_changes_nothing_and_keeps_the_points(
    adapter: QdrantVectorIndex, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    stored = point(index_id, DOC_A, V1, 0)
    await adapter.upsert(index_id, [stored])

    await adapter.ensure_collection(index_id, PROFILE)

    assert list(await adapter.retrieve(index_id, [stored.point_id])) == [
        (stored.point_id, stored.chunk_hash)
    ]


async def test_check_collection_accepts_a_conforming_collection(
    adapter: QdrantVectorIndex, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)

    assert await adapter.check_collection(index_id, PROFILE) is True


@pytest.mark.parametrize(
    "other",
    [
        replace(PROFILE, dimension=PROFILE.dimension + 1),
        replace(PROFILE, distance=Distance.DOT),
    ],
    ids=["dimension", "distance"],
)
async def test_check_collection_refuses_a_collection_built_for_another_profile(
    adapter: QdrantVectorIndex, index_id: UUID, other: EmbeddingProfile
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)

    assert await adapter.check_collection(index_id, other) is False


async def test_check_collection_refuses_an_absent_collection(
    adapter: QdrantVectorIndex, index_id: UUID
) -> None:
    assert await adapter.check_collection(index_id, PROFILE) is False


# --- upsert et relecture --------------------------------------------------------------


async def test_upsert_then_retrieve_returns_the_pairs_of_the_existing_points_only(
    adapter: QdrantVectorIndex, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    points = [point(index_id, DOC_A, V1, n) for n in range(3)]
    await adapter.upsert(index_id, points, wait=True)
    absent = UUID(int=999)

    found = await adapter.retrieve(index_id, [*(p.point_id for p in points), absent])

    assert sorted(found) == sorted((p.point_id, p.chunk_hash) for p in points)


async def test_the_payload_carries_the_fields_of_the_manifest_and_the_filters(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    stored = point(index_id, DOC_A, V1, 7)
    await adapter.upsert(index_id, [stored])

    (record,) = await qdrant.retrieve(
        QdrantVectorIndex.collection_name(index_id),
        ids=[str(stored.point_id)],
        with_payload=True,
        with_vectors=True,
    )

    assert record.payload == {
        "chunk_id": str(stored.chunk_id),
        "unit_id": str(stored.unit_id),
        "version_id": str(V1),
        "document_id": str(DOC_A),
        "chunk_hash": stored.chunk_hash,
        "index_id": str(index_id),
        "visible": False,
        "topics": ["python", "asyncio"],
        "kind": "COURSE",
        "language": "fr",
    }
    assert record.vector == pytest.approx(list(stored.vector), abs=1e-6)


async def test_upserting_the_same_points_again_overwrites_without_accumulating(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    original = point(index_id, DOC_A, V1, 0)
    await adapter.upsert(index_id, [original])
    rewritten = replace(original, chunk_hash="nouveau-hash")

    await adapter.upsert(index_id, [rewritten])

    flags = await visible_flags(qdrant, index_id)
    assert list(flags) == [original.point_id]
    assert list(await adapter.retrieve(index_id, [original.point_id])) == [
        (original.point_id, "nouveau-hash")
    ]


async def test_a_point_is_readable_right_after_a_waited_upsert(
    adapter: QdrantVectorIndex, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    points = [point(index_id, DOC_A, V1, n) for n in range(50)]

    await adapter.upsert(index_id, points, wait=True)

    assert len(await adapter.retrieve(index_id, [p.point_id for p in points])) == 50


# --- visibilité et suppressions -------------------------------------------------------


async def test_set_visibility_touches_only_the_targeted_document_and_version(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    target = [point(index_id, DOC_A, V2, n) for n in range(2)]
    same_document = point(index_id, DOC_A, V1, 0)
    other_document = point(index_id, DOC_B, V2, 0)
    await adapter.upsert(index_id, [*target, same_document, other_document])

    await adapter.set_visibility(
        index_id, document_id=DOC_A, version_id=V2, visible=True
    )

    flags = await visible_flags(qdrant, index_id)
    assert [flags[p.point_id] for p in target] == [True, True]
    assert flags[same_document.point_id] is False
    assert flags[other_document.point_id] is False


async def test_set_visibility_can_hide_points_again(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    shown = point(index_id, DOC_A, V1, 0, visible=True)
    await adapter.upsert(index_id, [shown])

    await adapter.set_visibility(
        index_id, document_id=DOC_A, version_id=V1, visible=False
    )

    assert (await visible_flags(qdrant, index_id))[shown.point_id] is False


async def test_set_visibility_is_idempotent(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    stored = point(index_id, DOC_A, V1, 0)
    await adapter.upsert(index_id, [stored])

    for _ in range(2):
        await adapter.set_visibility(
            index_id, document_id=DOC_A, version_id=V1, visible=True
        )

    assert await visible_flags(qdrant, index_id) == {stored.point_id: True}


async def test_delete_points_of_a_version_keeps_the_other_versions_and_documents(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    doomed = [point(index_id, DOC_A, V1, n) for n in range(2)]
    kept = [point(index_id, DOC_A, V2, 0), point(index_id, DOC_B, V1, 0)]
    await adapter.upsert(index_id, [*doomed, *kept])

    await adapter.delete_points(index_id, PointSelector(DOC_A, V1))

    assert set(await visible_flags(qdrant, index_id)) == {p.point_id for p in kept}


async def test_delete_points_of_a_document_removes_all_its_versions(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    mine = [point(index_id, DOC_A, V1, 0), point(index_id, DOC_A, V2, 0)]
    other = point(index_id, DOC_B, V1, 0)
    await adapter.upsert(index_id, [*mine, other])

    await adapter.delete_points(index_id, PointSelector(DOC_A))

    assert set(await visible_flags(qdrant, index_id)) == {other.point_id}


async def test_delete_points_is_repeatable_and_tolerates_nothing_to_delete(
    adapter: QdrantVectorIndex, qdrant: AsyncQdrantClient, index_id: UUID
) -> None:
    await adapter.ensure_collection(index_id, PROFILE)
    stored = point(index_id, DOC_A, V1, 0)
    await adapter.upsert(index_id, [stored])

    await adapter.delete_points(index_id, PointSelector(DOC_A, V3))
    await adapter.delete_points(index_id, PointSelector(DOC_A, V1))
    await adapter.delete_points(index_id, PointSelector(DOC_A, V1))

    assert await visible_flags(qdrant, index_id) == {}

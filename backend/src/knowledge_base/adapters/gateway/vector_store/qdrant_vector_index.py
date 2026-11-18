"""Adapter ``VectorIndex`` sur Qdrant (knowledge-base.md §9, §11, §12 ;
ADR 0010, ADR 0011 §2).

- Une collection par ``SearchIndex``, nommée d'après ``index_id`` : jamais deux
  profils dans une même collection.
- Payload : les champs de ``IndexedPoint`` hors ``point_id`` et ``vector``
  (UUID en texte, ``topics`` en liste). Index de payload sur les champs filtrés
  par la recherche et la synchronisation (``visible`` booléen, les autres
  ``keyword``).
- Toutes les écritures attendent leur application (``wait=True``) et exigent
  le statut ``completed`` : une écriture non confirmée lève, la tentative est
  rejouée (les écritures sont idempotentes, §7.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance as QdrantDistance
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    UpdateResult,
    UpdateStatus,
    VectorParams,
)

from knowledge_base.domain.value_objects.embedding_profile import (
    Distance,
    EmbeddingProfile,
)
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)

COLLECTION_PREFIX: Final = "kb_index_"

_DISTANCES: Final[Mapping[Distance, QdrantDistance]] = {
    Distance.COSINE: QdrantDistance.COSINE,
    Distance.DOT: QdrantDistance.DOT,
    Distance.EUCLID: QdrantDistance.EUCLID,
}

# Filtres de la recherche (§12) et de la synchronisation (§11).
PAYLOAD_INDEXES: Final[Mapping[str, PayloadSchemaType]] = {
    "visible": PayloadSchemaType.BOOL,
    "unit_id": PayloadSchemaType.KEYWORD,
    "document_id": PayloadSchemaType.KEYWORD,
    "version_id": PayloadSchemaType.KEYWORD,
    "topics": PayloadSchemaType.KEYWORD,
    "kind": PayloadSchemaType.KEYWORD,
    "language": PayloadSchemaType.KEYWORD,
}


class VectorWriteNotAppliedError(Exception):
    """Qdrant n'a pas confirmé une écriture attendue (``wait=True``)."""

    def __init__(self, operation: str, collection: str, status: str) -> None:
        super().__init__(
            f"Qdrant : {operation} sur {collection} non confirmé (statut {status})"
        )
        self.operation = operation
        self.collection = collection
        self.status = status


class QdrantVectorIndex:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    @staticmethod
    def collection_name(index_id: UUID) -> str:
        return f"{COLLECTION_PREFIX}{index_id.hex}"

    async def ensure_collection(
        self, index_id: UUID, profile: EmbeddingProfile
    ) -> None:
        name = self.collection_name(index_id)
        if not await self._client.collection_exists(name):
            await self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=profile.dimension, distance=_DISTANCES[profile.distance]
                ),
            )
        # Recréer un index de payload existant est sans effet : la reprise
        # après un arrêt entre deux créations les complète.
        for field, schema in PAYLOAD_INDEXES.items():
            _require_applied(
                "create_payload_index",
                name,
                await self._client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=schema,
                    wait=True,
                ),
            )

    async def check_collection(self, index_id: UUID, profile: EmbeddingProfile) -> bool:
        name = self.collection_name(index_id)
        if not await self._client.collection_exists(name):
            return False
        vectors = (await self._client.get_collection(name)).config.params.vectors
        # Une collection à vecteurs nommés n'a pas été créée par cet adapter.
        if not isinstance(vectors, VectorParams):
            return False
        return (
            vectors.size == profile.dimension
            and vectors.distance == _DISTANCES[profile.distance]
        )

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        if not points:
            return
        name = self.collection_name(index_id)
        result = await self._client.upsert(
            collection_name=name,
            points=[
                PointStruct(
                    id=str(p.point_id), vector=list(p.vector), payload=_payload(p)
                )
                for p in points
            ],
            wait=wait,
        )
        if wait:
            _require_applied("upsert", name, result)

    async def retrieve(
        self, index_id: UUID, point_ids: Sequence[UUID]
    ) -> Sequence[tuple[UUID, str]]:
        if not point_ids:
            return []
        records = await self._client.retrieve(
            collection_name=self.collection_name(index_id),
            ids=[str(i) for i in point_ids],
            with_payload=["chunk_hash"],
            with_vectors=False,
        )
        return [
            (UUID(str(record.id)), str((record.payload or {}).get("chunk_hash", "")))
            for record in records
        ]

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None:
        name = self.collection_name(index_id)
        _require_applied(
            "set_payload",
            name,
            await self._client.set_payload(
                collection_name=name,
                payload={"visible": visible},
                points=_filter(PointSelector(document_id, version_id)),
                wait=True,
            ),
        )

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        name = self.collection_name(index_id)
        _require_applied(
            "delete",
            name,
            await self._client.delete(
                collection_name=name,
                points_selector=FilterSelector(filter=_filter(selector)),
                wait=True,
            ),
        )


def _payload(point: IndexedPoint) -> dict[str, Any]:
    return {
        "chunk_id": str(point.chunk_id),
        "unit_id": str(point.unit_id),
        "version_id": str(point.version_id),
        "document_id": str(point.document_id),
        "chunk_hash": point.chunk_hash,
        "index_id": str(point.index_id),
        "visible": point.visible,
        "topics": list(point.topics),
        "kind": point.kind,
        "language": point.language,
    }


def _filter(selector: PointSelector) -> Filter:
    conditions = [
        FieldCondition(
            key="document_id", match=MatchValue(value=str(selector.document_id))
        )
    ]
    if selector.version_id is not None:
        conditions.append(
            FieldCondition(
                key="version_id", match=MatchValue(value=str(selector.version_id))
            )
        )
    return Filter(must=conditions)


def _require_applied(operation: str, collection: str, result: UpdateResult) -> None:
    if result.status != UpdateStatus.COMPLETED:
        raise VectorWriteNotAppliedError(operation, collection, str(result.status))

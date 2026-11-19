"""Adapter ``VectorIndex`` sur Qdrant (knowledge-base.md §9, §11, §12 ;
ADR 0010, ADR 0011 §2).

- Une collection par ``SearchIndex``, nommée d'après ``index_id`` : jamais deux
  profils dans une même collection.
- Payload : les champs de ``IndexedPoint`` hors ``point_id`` et ``vector``
  (UUID en texte, ``topics`` en liste), plus ``visible``, toujours faux à
  l'écriture (seul ``set_visibility`` le rend vrai, §11). Index de payload sur
  les champs filtrés par la recherche et la synchronisation (``visible``
  booléen, les autres ``keyword``).
- Toutes les écritures attendent leur application (``wait=True``) et exigent
  le statut ``completed`` : une écriture non confirmée lève, la tentative est
  rejouée (les écritures sont idempotentes, §7.3).
- Réconciliation (S8, port ``PointInventory``) : parcours paginé (``scroll``)
  de toute la collection ou des points d'une version, payload réduit au
  propriétaire, à l'empreinte et à la visibilité, sans vecteur ; suppression
  par identifiants des points hors manifest.
- Maintenance (S9, port ``IndexCollections``) : suppression de la collection
  d'un index retiré, sans erreur si elle n'existe plus.
- Recherche (S5, port ``ChunkSearcher``) : ``query_points_groups`` groupé par
  ``unit_id``, ``CHUNKS_PER_UNIT`` chunks au plus par unité ; le filtre
  ``visible=true`` est toujours ajouté ici (§12 étape 3). Un hit sans
  ``chunk_id`` (point supprimé pendant la requête, S7) est ignoré.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance as QdrantDistance
from qdrant_client.models import (
    ExtendedPointId,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    Record,
    UpdateResult,
    UpdateStatus,
    VectorParams,
)

from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.embedding_profile import (
    Distance,
    EmbeddingProfile,
)
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from knowledge_base.domain.value_objects.point_record import PointRecord
from knowledge_base.domain.value_objects.search_filters import SearchFilters

COLLECTION_PREFIX: Final = "kb_index_"

# Chunks rendus par unité trouvée : ceux qui ancrent la fenêtre small-to-big
# d'une unité longue. Au-delà de trois, une unité de 512 tokens par chunk
# dépasserait de toute façon le budget de passage par défaut.
CHUNKS_PER_UNIT: Final = 3

# Parcours de la réconciliation (S8) : champs lus par point, et taille des
# pages de la lecture des points d'une version.
_SCANNED_FIELDS: Final = ("document_id", "version_id", "chunk_hash", "visible")
_SCROLL_BATCH: Final = 256

logger = logging.getLogger(__name__)

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

    async def delete_collection(self, index_id: UUID) -> None:
        name = self.collection_name(index_id)
        if not await self._client.collection_exists(name):
            logger.info("collection %s déjà absente : rien à supprimer", name)
            return
        if not await self._client.delete_collection(collection_name=name):
            raise VectorWriteNotAppliedError("delete_collection", name, "false")

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

    # --- parcours pour la réconciliation (S8, port PointInventory) ------------------

    async def scan_points(
        self, index_id: UUID, *, batch_size: int = 256
    ) -> AsyncIterator[PointRecord]:
        name = self.collection_name(index_id)
        offset: ExtendedPointId | None = None
        while True:
            records, offset = await self._client.scroll(
                collection_name=name,
                limit=batch_size,
                offset=offset,
                with_payload=list(_SCANNED_FIELDS),
                with_vectors=False,
            )
            for record in records:
                point = _point_record(record)
                if point is not None:
                    yield point
            if offset is None:
                return

    async def points_of(
        self, index_id: UUID, selector: PointSelector
    ) -> Mapping[UUID, str]:
        name = self.collection_name(index_id)
        found: dict[UUID, str] = {}
        offset: ExtendedPointId | None = None
        while True:
            records, offset = await self._client.scroll(
                collection_name=name,
                scroll_filter=_filter(selector),
                limit=_SCROLL_BATCH,
                offset=offset,
                with_payload=["chunk_hash"],
                with_vectors=False,
            )
            for record in records:
                found[UUID(str(record.id))] = str(
                    (record.payload or {}).get("chunk_hash", "")
                )
            if offset is None:
                return found

    async def delete_point_ids(
        self, index_id: UUID, point_ids: Collection[UUID]
    ) -> None:
        if not point_ids:
            return
        name = self.collection_name(index_id)
        _require_applied(
            "delete",
            name,
            await self._client.delete(
                collection_name=name,
                points_selector=PointIdsList(points=[str(i) for i in point_ids]),
                wait=True,
            ),
        )

    async def search(
        self,
        index_id: UUID,
        vector: Sequence[float],
        *,
        unit_limit: int,
        filters: SearchFilters,
        excluded_unit_ids: Collection[UUID],
    ) -> Sequence[ChunkHit]:
        result = await self._client.query_points_groups(
            collection_name=self.collection_name(index_id),
            query=list(vector),
            group_by="unit_id",
            limit=unit_limit,
            group_size=CHUNKS_PER_UNIT,
            query_filter=search_filter(filters, excluded_unit_ids),
            with_payload=["chunk_id", "unit_id"],
            with_vectors=False,
        )
        hits: list[ChunkHit] = []
        for group in result.groups:
            for point in group.hits:
                chunk_id = (point.payload or {}).get("chunk_id")
                if chunk_id is None:
                    # Point supprimé pendant la requête (purge ou
                    # synchronisation concurrente) : Qdrant peut rendre le hit
                    # avec un payload réduit à la clé de regroupement. Il
                    # n'existe plus ; PostgreSQL arbitre de toute façon (§11).
                    logger.debug(
                        "hit %s de l'unité %s sans chunk_id ignoré (point en "
                        "cours de suppression)",
                        point.id,
                        group.id,
                    )
                    continue
                hits.append(
                    ChunkHit(
                        chunk_id=UUID(str(chunk_id)),
                        unit_id=UUID(str(group.id)),
                        score=point.score,
                    )
                )
        return hits


def search_filter(
    filters: SearchFilters, excluded_unit_ids: Collection[UUID]
) -> Filter:
    """``visible=true`` toujours ; un sujet = une condition (tous requis) ;
    types et documents : l'un d'eux."""
    must = [FieldCondition(key="visible", match=MatchValue(value=True))]
    if filters.language is not None:
        must.append(
            FieldCondition(key="language", match=MatchValue(value=filters.language))
        )
    must.extend(
        FieldCondition(key="topics", match=MatchValue(value=topic))
        for topic in filters.topics
    )
    if filters.kinds:
        must.append(FieldCondition(key="kind", match=MatchAny(any=list(filters.kinds))))
    if filters.document_ids:
        must.append(
            FieldCondition(
                key="document_id",
                match=MatchAny(any=[str(i) for i in filters.document_ids]),
            )
        )
    must_not = []
    if excluded_unit_ids:
        must_not.append(
            FieldCondition(
                key="unit_id", match=MatchAny(any=[str(i) for i in excluded_unit_ids])
            )
        )
    return Filter(must=must, must_not=must_not or None)


def _payload(point: IndexedPoint) -> dict[str, Any]:
    return {
        "chunk_id": str(point.chunk_id),
        "unit_id": str(point.unit_id),
        "version_id": str(point.version_id),
        "document_id": str(point.document_id),
        "chunk_hash": point.chunk_hash,
        "index_id": str(point.index_id),
        # Un upsert remplace tout le payload : réécrire un point le masque
        # jusqu'à la prochaine synchronisation, jamais l'inverse (§11).
        "visible": False,
        "topics": list(point.topics),
        "kind": point.kind,
        "language": point.language,
    }


def _point_record(record: Record) -> PointRecord | None:
    """``None`` pour un point sans propriétaire lisible : il n'a pas été écrit
    par cet adapter (payload incomplet), la réconciliation l'ignore."""
    payload = record.payload or {}
    try:
        return PointRecord(
            point_id=UUID(str(record.id)),
            document_id=UUID(str(payload["document_id"])),
            version_id=UUID(str(payload["version_id"])),
            chunk_hash=str(payload.get("chunk_hash", "")),
            visible=bool(payload.get("visible", False)),
        )
    except (KeyError, ValueError):
        logger.warning(
            "point %s sans document_id ou version_id lisible, ignoré", record.id
        )
        return None


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

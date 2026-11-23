"""Recherche Qdrant pendant une suppression de points (S7 ; knowledge-base.md
§10, §11, I12 ; risque « courses entre purge et upsert »).

Constat de S7 (reproduit contre Qdrant v1.20.2, hors dépôt) : pendant qu'une
suppression par filtre est appliquée (``delete_points`` de
``sync_visibility`` ou de ``purge_document``), une requête groupée en cours peut
rendre un hit dont le point vient d'être supprimé et dont le payload est
incomplet (``unit_id`` seul, sans ``chunk_id``). Aujourd'hui l'adapter lève
``KeyError: 'chunk_id'`` : la recherche répond 500 au lieu de rendre les
passages qui restent.

Contrat figé de ``QdrantVectorIndex.search`` : un hit dont le payload n'a pas de
``chunk_id`` est IGNORÉ (le point n'existe plus ; PostgreSQL arbitre de toute
façon, §11) ; les autres hits de la même recherche sont rendus normalement ; la
recherche ne lève pas. Le client Qdrant, frontière réseau, est remplacé.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import GroupsResult, PointGroup, ScoredPoint

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.value_objects.search_filters import SearchFilters

INDEX = UUID(int=1)
UNIT_OK, UNIT_GONE, UNIT_OTHER = UUID(int=11), UUID(int=12), UUID(int=13)
CHUNK_OK, CHUNK_OTHER = UUID(int=21), UUID(int=23)


def hit(score: float, payload: dict[str, Any]) -> ScoredPoint:
    return ScoredPoint(id=str(UUID(int=900)), version=1, score=score, payload=payload)


def group(unit: UUID, *hits: ScoredPoint) -> PointGroup:
    return PointGroup(id=str(unit), hits=list(hits))


class StubClient:
    """Ce que Qdrant rend pendant une suppression : un groupe dont le hit a perdu
    son payload (hormis la clé de regroupement)."""

    def __init__(self, groups: list[PointGroup]) -> None:
        self._groups = groups

    async def query_points_groups(self, **_kwargs: Any) -> GroupsResult:
        return GroupsResult(groups=self._groups)


async def search(groups: list[PointGroup]) -> list[tuple[UUID, UUID]]:
    index = QdrantVectorIndex(cast(AsyncQdrantClient, StubClient(groups)))
    hits = await index.search(
        INDEX, [0.0] * 4, unit_limit=5, filters=SearchFilters(), excluded_unit_ids=()
    )
    return [(h.unit_id, h.chunk_id) for h in hits]


def complete(unit: UUID, chunk: UUID, score: float = 0.9) -> ScoredPoint:
    return hit(score, {"unit_id": str(unit), "chunk_id": str(chunk)})


def emptied(unit: UUID, score: float = 0.8) -> ScoredPoint:
    return hit(score, {"unit_id": str(unit)})


async def test_a_hit_that_lost_its_payload_is_ignored_without_failing() -> None:
    groups = [
        group(UNIT_OK, complete(UNIT_OK, CHUNK_OK)),
        group(UNIT_GONE, emptied(UNIT_GONE)),
    ]

    assert await search(groups) == [(UNIT_OK, CHUNK_OK)]


async def test_the_other_hits_of_the_same_group_are_kept() -> None:
    groups = [
        group(
            UNIT_OK,
            complete(UNIT_OK, CHUNK_OK),
            emptied(UNIT_OK),
            complete(UNIT_OK, CHUNK_OTHER, 0.7),
        ),
        group(UNIT_OTHER, complete(UNIT_OTHER, CHUNK_OTHER, 0.6)),
    ]

    found = await search(groups)

    assert found == [
        (UNIT_OK, CHUNK_OK),
        (UNIT_OK, CHUNK_OTHER),
        (UNIT_OTHER, CHUNK_OTHER),
    ]


async def test_a_search_where_every_hit_vanished_answers_nothing() -> None:
    assert await search([group(UNIT_GONE, emptied(UNIT_GONE))]) == []


async def test_complete_hits_are_unchanged() -> None:
    groups = [
        group(UNIT_OK, complete(UNIT_OK, CHUNK_OK)),
        group(UNIT_OTHER, complete(UNIT_OTHER, CHUNK_OTHER, 0.5)),
    ]

    assert await search(groups) == [(UNIT_OK, CHUNK_OK), (UNIT_OTHER, CHUNK_OTHER)]

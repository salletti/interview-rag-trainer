"""``SearchKnowledge`` : une réponse ne mêle jamais deux versions d'un même
document (knowledge-base.md §8, §10, §12 ; I4 ; review indépendante,
constat 1).

Chaque passe interrogeait le contrôle SQL sur ses SEULS nouveaux candidats :
une publication de N+1 commitée et synchronisée entre la passe 1 et le
complément laissait une unité de N, retenue à la passe 1, à côté d'une unité
de N+1 trouvée par le complément. Les unités rendues doivent toutes être
validées par une même lecture SQL (un instantané) : sous I4, une seule
version d'un document y est publiée.

La barrière est posée sur le moteur vectoriel : la publication et la
synchronisation de N+1 s'exécutent juste avant la deuxième recherche.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from uuid import UUID

from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.published_unit import PublishedUnit
from knowledge_base.domain.value_objects.search_filters import SearchFilters
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_result import (
    SearchKnowledgeResult,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import single_profile
from tests.unit.knowledge_base.use_cases.search_fakes import (
    FakeActiveIndexes,
    FakeChunkSearcher,
    FakePublishedUnits,
    RecordingEmbedder,
    active_index,
    citation_of,
    hit,
    uid,
    whole_unit,
)

REPUBLISHED = citation_of(1)


def unit_of_version(n: int, number: int) -> PublishedUnit:
    """Unité ``n`` de la version ``number`` du document ``REPUBLISHED``."""
    citation = replace(
        REPUBLISHED, version_id=UUID(int=300 + number), version_number=number
    )
    return replace(whole_unit(n), citation=citation)


class PublishingBetweenPasses:
    """Délègue au moteur vectoriel ; exécute ``between`` juste avant la
    deuxième recherche (le complément), puis la laisse se faire."""

    def __init__(self, inner: FakeChunkSearcher, between: Callable[[], None]) -> None:
        self._inner = inner
        self._between = between

    async def search(
        self,
        index_id: UUID,
        vector: Sequence[float],
        *,
        unit_limit: int,
        filters: SearchFilters,
        excluded_unit_ids: Collection[UUID],
    ) -> Sequence[ChunkHit]:
        if len(self._inner.calls) == 1:
            self._between()
        return await self._inner.search(
            index_id,
            vector,
            unit_limit=unit_limit,
            filters=filters,
            excluded_unit_ids=excluded_unit_ids,
        )


async def search_across_a_republication(
    *, complement_passes: int, after_sync: Sequence[ChunkHit], limit: int = 2
) -> tuple[SearchKnowledgeResult, FakeChunkSearcher, FakePublishedUnits]:
    """Passe 1 (``limit=2``, f=2, 4 unités) : l'unité 1, de la version 3
    publiée du document, et les unités 2 à 4 d'un document retiré dont Qdrant
    porte encore les points (rejetées en SQL). Avant le complément, la
    version 4 (unités 5 et 6) est publiée (3 → ``SUPERSEDED``) et
    synchronisée : ses points sont visibles, ceux de la version 3 supprimés ;
    ``after_sync`` est le classement de Qdrant à partir de là."""
    searcher = FakeChunkSearcher([hit(n, 1 - n / 100) for n in (1, 2, 3, 4)])
    old = unit_of_version(1, number=3)
    units = FakePublishedUnits({old.unit_id: old})
    later = {
        u.unit_id: u for u in (unit_of_version(5, number=4), unit_of_version(6, 4))
    }

    def publish_and_synchronize_the_next_version() -> None:
        units.units = later
        searcher.pool = after_sync

    handler = SearchKnowledgeHandler(
        embedders=single_profile(RecordingEmbedder()),
        chunk_searcher=PublishingBetweenPasses(
            searcher, publish_and_synchronize_the_next_version
        ),
        published_units=units,
        active_indexes=FakeActiveIndexes(active_index()),
        max_passage_tokens=1_000,
        oversampling_factor=2,
        complement_passes=complement_passes,
    )
    result = await handler.handle(SearchKnowledge(text="depends", limit=limit))
    return result, searcher, units


def versions_of_the_republished_document(result: SearchKnowledgeResult) -> set[int]:
    return {
        p.citation.version_number
        for p in result.passages
        if p.citation.document_id == REPUBLISHED.document_id
    }


async def test_a_publication_between_two_passes_never_mixes_two_versions() -> None:
    result, searcher, _ = await search_across_a_republication(
        complement_passes=1,
        after_sync=[hit(n, 1 - n / 100) for n in (2, 3, 4, 5)],
    )

    assert len(searcher.calls) == 2  # la barrière a bien été franchie
    assert versions_of_the_republished_document(result) == {4}
    assert [p.unit_id for p in result.passages] == [uid(5)]
    # L'unité de la version 3, écartée par le contrôle commun, est un rejet
    # que le complément (épuisé) n'a pas remplacé.
    assert result.complete is False


async def test_every_returned_unit_is_validated_by_the_last_sql_read() -> None:
    result, _, units = await search_across_a_republication(
        complement_passes=1,
        after_sync=[hit(n, 1 - n / 100) for n in (2, 3, 4, 5)],
    )

    assert len(units.calls) == 2  # une lecture par passe, pas une de plus
    assert {p.unit_id for p in result.passages} <= set(units.calls[-1])
    assert uid(1) in units.calls[-1]  # l'unité retenue à la passe 1 est relue


async def test_a_remaining_pass_replaces_a_unit_rejected_by_the_reread() -> None:
    # Deux passes de complément. La deuxième trouve l'unité 5 et l'unité 7 (non
    # publiée), et sa lecture écarte l'unité 1 (version 3) : une seule unité
    # retenue. La troisième trouve l'unité 6 et complète le résultat.
    result, searcher, units = await search_across_a_republication(
        complement_passes=2,
        after_sync=[hit(n, 1 - n / 100) for n in (2, 3, 4, 5, 7, 6)],
    )

    assert len(searcher.calls) == 3
    assert versions_of_the_republished_document(result) == {4}
    assert [p.unit_id for p in result.passages] == [uid(5), uid(6)]
    assert {p.unit_id for p in result.passages} <= set(units.calls[-1])
    assert result.complete is True

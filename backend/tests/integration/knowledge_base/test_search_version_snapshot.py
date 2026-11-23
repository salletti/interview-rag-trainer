"""Une publication entre deux passes de recherche ne mêle pas deux versions
(knowledge-base.md §8, §10, §12 ; I4 ; review indépendante, constat 1), sur
PostgreSQL et Qdrant réels.

Entrelacement imposé par une barrière sur le moteur vectoriel, sans délai :

1. le document D est publié en version N (une unité) ; le document W (trois
   unités) est publié puis retiré, ses points restent visibles dans Qdrant
   (le retrait ne synchronise pas, la purge n'a pas tourné) ; N+1 est importée
   et extraite ;
2. passe 1 (``limit=2``, f=2, filtrée sur D et W) : l'unité de N, acceptée,
   et les trois unités de W, rejetées en SQL ;
3. barrière : N+1 est indexée, donc publiée (N → ``SUPERSEDED``), puis
   synchronisée (ses points visibles, ceux de N supprimés) ;
4. complément : il trouve l'unité de N+1.

Avant la correction, la réponse rendait N et N+1 ensemble : chaque passe ne
validait que ses nouveaux candidats. Le test S7 de non-mélange
(``test_s7_concurrent_versions``) ne forçait pas cet ordre : ses recherches
tournent librement autour de la publication, sans barrière entre deux passes,
et sans rejet tant que N est publiée, donc sans complément.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_active_index_reader import (
    SqlAlchemyActiveIndexReader,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_published_unit_reader import (  # noqa: E501
    SqlAlchemyPublishedUnitReader,
)
from knowledge_base.domain.providers.chunk_searcher import ChunkSearcher
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.search_filters import SearchFilters
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s7_support import (
    PYDANTIC_SHEET,
    S7Rig,
    open_s7_rig,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.use_cases.indexing_fakes import single_profile

pytestmark = pytest.mark.integration

SINGLE_QUESTION = (
    "# Fiche Depends\n\n"
    "## Question : Qu'est-ce que Depends ?\n\nDepends injecte une dépendance.\n"
).encode()
SINGLE_QUESTION_V2 = (
    "# Fiche Depends\n\n"
    "## Question : Qu'est-ce que Depends ?\n\nDepends résout les dépendances.\n"
).encode()


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


class BetweenPasses:
    """Délègue à Qdrant ; exécute ``between`` juste avant la deuxième
    recherche et note les unités rendues par chaque passe."""

    def __init__(
        self, inner: ChunkSearcher, between: Callable[[], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._between = between
        self.found: list[set[UUID]] = []

    async def search(
        self,
        index_id: UUID,
        vector: Sequence[float],
        *,
        unit_limit: int,
        filters: SearchFilters,
        excluded_unit_ids: Collection[UUID],
    ) -> Sequence[ChunkHit]:
        if len(self.found) == 1:
            await self._between()
        hits = await self._inner.search(
            index_id,
            vector,
            unit_limit=unit_limit,
            filters=filters,
            excluded_unit_ids=excluded_unit_ids,
        )
        self.found.append({hit.unit_id for hit in hits})
        return hits


async def units_of_version(rig: S7Rig, version_id: UUID) -> set[UUID]:
    rows = await rig.indexing.extraction.imports.rows(
        "SELECT id FROM knowledge_base.knowledge_units WHERE version_id = :v",
        v=version_id,
    )
    return {row["id"] for row in rows}


async def test_a_publication_between_two_passes_never_mixes_two_versions(
    rig: S7Rig,
) -> None:
    withdrawn = await rig.publish(PYDANTIC_SHEET, title="Pydantic")
    first = await rig.publish(SINGLE_QUESTION, title="Depends")
    await rig.withdraw(withdrawn.document_id)
    second = await rig.post_version(first.document_id, SINGLE_QUESTION_V2)
    await rig.extract(second)

    async def publish_and_synchronize_the_next_version() -> None:
        indexed = await rig.indexing.run_index(second.version_id, rig.index_id)
        assert indexed == "succeeded"
        await rig.synchronize(first.document_id)

    searcher = BetweenPasses(
        rig.indexing.vector, publish_and_synchronize_the_next_version
    )
    handler = SearchKnowledgeHandler(
        embedders=single_profile(rig.indexing.embedder, COUNTER),
        chunk_searcher=searcher,
        published_units=SqlAlchemyPublishedUnitReader(rig.session_factory),
        active_indexes=SqlAlchemyActiveIndexReader(rig.session_factory),
        max_passage_tokens=1_024,
        oversampling_factor=2,
        complement_passes=1,
    )

    result = await handler.handle(
        SearchKnowledge(
            text="Qu'est-ce que Depends ?",
            limit=2,
            document_ids=(first.document_id, withdrawn.document_id),
        )
    )

    # L'entrelacement du constat a bien eu lieu : la passe 1 a vu l'unité de N
    # et les trois unités du document retiré, le complément l'unité de N+1.
    old_units = await units_of_version(rig, first.version_id)
    new_units = await units_of_version(rig, second.version_id)
    assert len(searcher.found) == 2
    assert old_units <= searcher.found[0]
    assert len(searcher.found[0]) == 4
    assert new_units <= searcher.found[1]

    versions = {
        p.citation.version_id
        for p in result.passages
        if p.citation.document_id == first.document_id
    }
    assert versions == {second.version_id}
    assert {p.unit_id for p in result.passages} == new_units
    assert result.complete is False

"""La recherche encode la requête avec le profil de l'index ACTIF (S9 ; ADR 0011
« la recherche lit toujours l'index ACTIVE dans PostgreSQL et charge le modèle
de son profil » ; S5 : garde-fou ``SEARCH_PROFILE_MISMATCH`` en attendant S9).

Contrat : ``SearchKnowledgeHandler(..., embedders=<EmbedderProvider>)`` à la
place de ``embedder=`` et ``token_counter=`` (le compteur du budget est celui du
modèle de l'index actif). Pour chaque requête : index actif lu, puis
``embedders.for_profile(index.profile)``, requête encodée UNE fois par ce
modèle, recherche dans la collection de cet index. DÉCISION (à valider) : un
profil que le processus ne sait pas charger reste refusé explicitement par
``SearchProfileMismatchError`` (code public ``SEARCH_PROFILE_MISMATCH``
inchangé), avant tout encodage et toute requête Qdrant : jamais de recherche
« à peu près » avec un autre modèle.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_corpus import SearchIndex
from knowledge_base.use_cases.search_knowledge.errors import (
    SearchProfileMismatchError,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE
from tests.unit.knowledge_base.use_cases.s9_support import (
    PROFILE_B,
    FakeEmbedderProvider,
    ProfileModel,
)
from tests.unit.knowledge_base.use_cases.search_fakes import (
    INDEX_ID,
    FakeChunkSearcher,
    FakePublishedUnits,
    active_index,
    hit,
    whole_unit,
)

INDEX_B = UUID(int=9_002)


class SwitchableIndex:
    """``ActiveIndexReader`` dont l'index actif change (la bascule)."""

    def __init__(self, index: SearchIndex | None) -> None:
        self.index = index

    async def get_active_index(self) -> SearchIndex | None:
        return self.index


def index_b() -> SearchIndex:
    return replace(active_index(), index_id=INDEX_B, profile=PROFILE_B)


class Rig:
    def __init__(self, index: SearchIndex, *models: ProfileModel) -> None:
        self.model_a, self.model_b = ProfileModel(PROFILE), ProfileModel(PROFILE_B)
        self.provider = FakeEmbedderProvider(*(models or (self.model_a, self.model_b)))
        self.searcher = FakeChunkSearcher([hit(1, 0.9)])
        self.active = SwitchableIndex(index)
        try:
            handler: Any = SearchKnowledgeHandler
            self.handler: Any = handler(
                embedders=self.provider,
                chunk_searcher=self.searcher,
                published_units=FakePublishedUnits(
                    {whole_unit(1).unit_id: whole_unit(1)}
                ),
                active_indexes=self.active,
                max_passage_tokens=1_000,
            )
        except TypeError as error:
            pytest.fail(
                f"contrat S9 absent : SearchKnowledgeHandler(embedders=...) ({error})",
                pytrace=False,
            )


async def test_the_query_is_encoded_by_the_model_of_the_active_index() -> None:
    rig = Rig(index_b())

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert rig.model_b.query_calls == ["depends"]
    assert rig.model_a.query_calls == []
    (call,) = rig.searcher.calls
    assert call.index_id == INDEX_B
    assert len(call.vector) == PROFILE_B.dimension
    assert result.index_id == INDEX_B


async def test_the_query_follows_the_active_index_across_the_switch() -> None:
    rig = Rig(active_index())
    before = await rig.handler.handle(SearchKnowledge(text="depends"))

    rig.active.index = index_b()
    after = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert (before.index_id, after.index_id) == (INDEX_ID, INDEX_B)
    assert rig.model_a.query_calls == ["depends"]
    assert rig.model_b.query_calls == ["depends"]
    assert [len(c.vector) for c in rig.searcher.calls] == [
        PROFILE.dimension,
        PROFILE_B.dimension,
    ]


async def test_a_profile_the_process_cannot_load_is_refused_before_any_query() -> None:
    other = ProfileModel(PROFILE)
    rig = Rig(index_b(), other)

    with pytest.raises(SearchProfileMismatchError) as refused:
        await rig.handler.handle(SearchKnowledge(text="depends"))

    assert refused.value.index_id == INDEX_B
    assert refused.value.index_profile == PROFILE_B
    assert other.query_calls == []
    assert rig.searcher.calls == []


async def test_the_provider_is_asked_for_the_profile_of_the_active_index_only() -> None:
    rig = Rig(index_b())

    await rig.handler.handle(SearchKnowledge(text="depends"))

    assert rig.provider.requested == [PROFILE_B]

"""Garde-fous de la recherche autour du cas d'usage (S5 ; ADR 0011 §2 ;
knowledge-base.md §14) : profil de l'index actif, traduction vers le contrat
public et ses erreurs."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from knowledge_base.adapters.controller.contracts.knowledge_search_adapter import (
    KnowledgeSearchAdapter,
)
from knowledge_base.contracts.providers import (
    KnowledgeSearchUnavailableError,
    UnavailabilityReason,
)
from knowledge_base.contracts.result import Citation, KnowledgeQuery
from knowledge_base.use_cases.search_knowledge.errors import (
    SearchProfileMismatchError,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE
from tests.unit.knowledge_base.use_cases.search_fakes import (
    INDEX_ID,
    active_index,
    hit,
    make_rig,
    uid,
    whole_unit,
)


async def test_an_index_built_with_another_profile_is_refused_before_any_query() -> (
    None
):
    other = replace(active_index(), profile=replace(PROFILE, revision="autre"))
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)], index=other)

    with pytest.raises(SearchProfileMismatchError) as raised:
        await rig.handler.handle(SearchKnowledge(text="depends"))

    assert raised.value.index_id == INDEX_ID
    assert rig.embedder.queries == []
    assert rig.searcher.calls == []


async def test_the_adapter_returns_the_contract_types() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])

    result = await KnowledgeSearchAdapter(rig.handler).search(
        KnowledgeQuery(text="depends", limit=1, kinds=("QA_SHEET",))
    )

    (passage,) = result.passages
    assert passage.unit_id == uid(1)
    assert passage.unit_type == "QA"
    assert passage.citation == Citation(
        document_id=UUID(int=101),
        document_title="Fiche FastAPI",
        version_id=UUID(int=201),
        version_number=3,
        section_path=("Fiche FastAPI", "Section 1"),
        page_start=1,
        page_end=2,
        source_filename="fastapi.md",
    )
    assert result.index_id == INDEX_ID
    assert result.complete is True


@pytest.mark.parametrize(
    ("rig_options", "reason"),
    [
        ({"with_index": False}, UnavailabilityReason.NO_ACTIVE_SEARCH_INDEX),
        (
            {"index": replace(active_index(), profile=replace(PROFILE, dimension=4))},
            UnavailabilityReason.SEARCH_PROFILE_MISMATCH,
        ),
    ],
    ids=["no-index", "profile"],
)
async def test_an_unusable_index_is_a_contract_error_with_its_reason(
    rig_options: dict[str, object], reason: UnavailabilityReason
) -> None:
    rig = make_rig(ranking=[], published=[], **rig_options)  # type: ignore[arg-type]

    with pytest.raises(KnowledgeSearchUnavailableError) as raised:
        await KnowledgeSearchAdapter(rig.handler).search(KnowledgeQuery(text="x"))

    assert raised.value.reason is reason


async def test_an_invalid_query_is_a_value_error_for_the_consumer() -> None:
    rig = make_rig(ranking=[], published=[])

    with pytest.raises(ValueError):
        await KnowledgeSearchAdapter(rig.handler).search(KnowledgeQuery(text=" "))
    with pytest.raises(ValueError):
        await KnowledgeSearchAdapter(rig.handler).search(
            KnowledgeQuery(text="x", kinds=("POEM",))
        )

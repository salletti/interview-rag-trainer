"""``SearchKnowledge`` : requête, contrôle SQL, complément, regroupement et
provenance (S5 ; knowledge-base.md §12, §14 ; ADR 0010 §6 ; O12).

Les ports, types et signatures figés par ces tests sont décrits dans
``search_fakes`` (module docstring). Paramètres de O12, Proposed jusqu'à la
mesure de S5 : suréchantillonnage ``f = 2`` (``unit_limit = limit x f`` à la
première passe), UNE passe de complément (configurable).

Points ambigus de la conception, volontairement NON figés ici :

- la valeur de ``complete`` quand moins de ``limit`` unités existent SANS aucun
  rejet (le commentaire du §14 dit « moins de ``limit`` passages » ; l'étape 5
  du §12 ne parle que des rejets) ;
- le nombre d'unités demandées à la passe de complément (``limit x f`` ou
  ``manquantes x f``) et la présence des unités déjà retenues dans l'exclusion :
  les tests ne figent que le résultat observable et l'exclusion des REJETÉES.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.value_objects.search_filters import SearchFilters
from knowledge_base.use_cases.search_knowledge.errors import (
    NoActiveSearchIndexError,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from tests.unit.knowledge_base.use_cases.search_fakes import (
    INDEX_ID,
    QUERY_VECTOR,
    FakeActiveIndexes,
    active_index,
    citation_of,
    hit,
    make_rig,
    uid,
    whole_unit,
)


def ids(*numbers: int) -> list[UUID]:
    return [uid(n) for n in numbers]


# --- commande -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_a_blank_query_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        SearchKnowledge(text=text)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_refused(limit: int) -> None:
    with pytest.raises(ValueError):
        SearchKnowledge(text="depends", limit=limit)


def test_the_default_limit_is_five() -> None:
    assert SearchKnowledge(text="depends").limit == 5


# --- requête vectorielle ------------------------------------------------------


async def test_the_raw_query_is_embedded_and_searched_in_the_active_index() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])

    result = await rig.handler.handle(SearchKnowledge(text="Qu'est-ce que Depends ?"))

    # Le préfixe ``query: `` est l'affaire de l'adapter d'embeddings (S4) :
    # le cas d'usage transmet le texte brut.
    assert rig.embedder.queries == ["Qu'est-ce que Depends ?"]
    (call,) = rig.searcher.calls
    assert call.index_id == INDEX_ID
    assert list(call.vector) == QUERY_VECTOR
    assert result.index_id == INDEX_ID


async def test_the_filters_of_the_query_reach_the_vector_search_unchanged() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])
    document = UUID(int=4242)

    await rig.handler.handle(
        SearchKnowledge(
            text="depends",
            language="fr",
            topics=("python", "fastapi"),
            kinds=("QA_SHEET",),
            document_ids=(document,),
        )
    )

    (call,) = rig.searcher.calls
    assert call.filters == SearchFilters(
        language="fr",
        topics=("python", "fastapi"),
        kinds=("QA_SHEET",),
        document_ids=(document,),
    )


async def test_without_filters_the_search_is_unfiltered() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])

    await rig.handler.handle(SearchKnowledge(text="depends"))

    assert rig.searcher.calls[0].filters == SearchFilters()


async def test_the_first_pass_oversamples_by_the_factor_of_the_configuration() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 4)],
        published=[whole_unit(n) for n in range(1, 4)],
        oversampling_factor=3,
    )

    await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    assert rig.searcher.calls[0].unit_limit == 6


async def test_the_default_oversampling_factor_is_two() -> None:
    # O12 (Proposed) : f = 2 quand le constructeur ne reçoit pas de facteur.
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])
    handler = SearchKnowledgeHandler(
        embedder=rig.embedder,
        chunk_searcher=rig.searcher,
        published_units=rig.units,
        active_indexes=FakeActiveIndexes(active_index()),
        max_passage_tokens=1_000,
    )

    await handler.handle(SearchKnowledge(text="depends", limit=5))

    assert rig.searcher.calls[0].unit_limit == 10


async def test_without_a_complement_default_one_pass_is_made() -> None:
    # O12 (Proposed) : UNE passe de complément par défaut.
    rig = make_rig(ranking=[hit(n, 1 - n / 100) for n in range(1, 13)], published=[])
    handler = SearchKnowledgeHandler(
        embedder=rig.embedder,
        chunk_searcher=rig.searcher,
        published_units=rig.units,
        active_indexes=FakeActiveIndexes(active_index()),
        max_passage_tokens=1_000,
    )

    await handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(rig.searcher.calls) == 2


async def test_without_an_active_index_the_search_fails_before_any_query() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)], with_index=False)

    with pytest.raises(NoActiveSearchIndexError):
        await rig.handler.handle(SearchKnowledge(text="depends"))

    assert rig.searcher.calls == []
    assert rig.embedder.queries == []


# --- contrôle SQL final -------------------------------------------------------


async def test_a_unit_whose_version_is_not_published_is_never_returned() -> None:
    # Unités 1 et 2 : encore ``visible`` dans Qdrant (fenêtre de
    # synchronisation, §11) mais plus publiées en SQL.
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 7)],
        published=[whole_unit(n) for n in range(3, 7)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert [p.unit_id for p in result.passages] == ids(3, 4, 5)
    assert uid(1) not in {p.unit_id for p in result.passages}
    assert uid(2) not in {p.unit_id for p in result.passages}


async def test_the_sql_control_is_asked_about_every_unit_found_by_qdrant() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 5)],
        published=[whole_unit(3), whole_unit(4)],
    )

    await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    asked = {unit for call in rig.units.calls for unit in call}
    assert {uid(1), uid(2)} <= asked


async def test_when_every_unit_is_rejected_the_result_is_empty() -> None:
    rig = make_rig(ranking=[hit(1, 0.9), hit(2, 0.8)], published=[])

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    assert result.passages == ()
    assert result.complete is False


# --- passe de complément ------------------------------------------------------


async def test_no_complement_pass_when_enough_units_survive_the_sql_control() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 7)],
        published=[whole_unit(n) for n in range(3, 7)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(rig.searcher.calls) == 1
    assert result.complete is True


async def test_a_complement_pass_replaces_the_rejected_units_and_excludes_them() -> (
    None
):
    # limit=3, f=2 : la première passe voit les unités 1 à 6 ; 1 à 4 sont
    # rejetées, il n'en reste que 2. Le complément trouve la 7.
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 9)],
        published=[whole_unit(n) for n in (5, 6, 7, 8)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(rig.searcher.calls) == 2
    assert {uid(n) for n in (1, 2, 3, 4)} <= rig.searcher.calls[1].excluded_unit_ids
    assert [p.unit_id for p in result.passages] == ids(5, 6, 7)
    assert result.complete is True


async def test_the_first_pass_excludes_nothing() -> None:
    rig = make_rig(ranking=[hit(1, 0.9)], published=[whole_unit(1)])

    await rig.handler.handle(SearchKnowledge(text="depends"))

    assert rig.searcher.calls[0].excluded_unit_ids == frozenset()


async def test_the_complement_keeps_the_filters_of_the_query() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 9)],
        published=[whole_unit(n) for n in (5, 6, 7, 8)],
    )

    await rig.handler.handle(
        SearchKnowledge(text="depends", limit=3, language="en", topics=("python",))
    )

    assert rig.searcher.calls[1].filters == SearchFilters(
        language="en", topics=("python",)
    )
    assert rig.searcher.calls[1].vector == rig.searcher.calls[0].vector
    assert rig.embedder.queries == ["depends"]  # la requête n'est encodée qu'une fois


async def test_rejections_beyond_the_complement_make_the_result_incomplete() -> None:
    # limit=3 : première passe = unités 1 à 6 (toutes rejetées), complément =
    # 7 à 12 dont seule la 11 est publiée : 1 passage sur 3 demandés.
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 13)],
        published=[whole_unit(11)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(rig.searcher.calls) == 2  # une seule passe de complément
    assert [p.unit_id for p in result.passages] == ids(11)
    assert result.complete is False


async def test_a_rejected_unit_returned_by_the_complement_is_still_filtered() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 9)],
        published=[whole_unit(5), whole_unit(6), whole_unit(8)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert [p.unit_id for p in result.passages] == ids(5, 6, 8)
    assert uid(7) not in {p.unit_id for p in result.passages}


async def test_without_any_complement_pass_the_result_is_incomplete() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 9)],
        published=[whole_unit(n) for n in (5, 6, 7, 8)],
        complement_passes=0,
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(rig.searcher.calls) == 1
    assert [p.unit_id for p in result.passages] == ids(5, 6)
    assert result.complete is False


async def test_the_number_of_complement_passes_is_configurable() -> None:
    # 8 unités rejetées d'abord (1 à 8) : il faut trois passes de 4 unités
    # (limit=2, f=2) pour atteindre les unités 11 et 12.
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 13)],
        published=[whole_unit(11), whole_unit(12)],
        complement_passes=2,
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    assert len(rig.searcher.calls) == 3
    assert [p.unit_id for p in result.passages] == ids(11, 12)
    assert result.complete is True


async def test_one_complement_pass_does_not_reach_units_beyond_its_reach() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 13)],
        published=[whole_unit(11), whole_unit(12)],
        complement_passes=1,
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    assert len(rig.searcher.calls) == 2
    assert result.passages == ()
    assert result.complete is False


# --- regroupement par unité, score, limite ------------------------------------


async def test_the_chunks_of_one_unit_make_a_single_passage() -> None:
    rig = make_rig(
        ranking=[hit(1, 0.9, rank=0), hit(1, 0.7, rank=1), hit(1, 0.5, rank=2)],
        published=[whole_unit(1, chunk_count=3, token_count=90)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    (passage,) = result.passages
    assert passage.unit_id == uid(1)
    assert set(passage.matched_chunk_ids) == {
        hit(1, 0, rank=r).chunk_id for r in (0, 1, 2)
    }


async def test_the_score_of_a_unit_is_the_best_score_of_its_chunks() -> None:
    rig = make_rig(
        # Le meilleur chunk n'est ni le premier ni le dernier de la liste.
        ranking=[hit(1, 0.4, rank=1), hit(1, 0.9, rank=0), hit(1, 0.2, rank=2)],
        published=[whole_unit(1, chunk_count=3, token_count=90)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert result.passages[0].score == pytest.approx(0.9)


async def test_a_unit_with_many_average_chunks_does_not_outrank_a_better_unit() -> None:
    # Unité 1 : somme 2,1 (> 0,8), meilleur 0,75 ; unité 2 : un chunk à 0,8.
    # Classer par somme mettrait l'unité 1 devant ; le meilleur score non. Le
    # meilleur chunk de l'unité 1 n'est ni le premier ni le dernier.
    rig = make_rig(
        ranking=[
            hit(1, 0.65, rank=0),
            hit(2, 0.8, rank=0),
            hit(1, 0.75, rank=1),
            hit(1, 0.7, rank=2),
        ],
        published=[
            whole_unit(1, chunk_count=3, token_count=90),
            whole_unit(2),
        ],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert [p.unit_id for p in result.passages] == ids(2, 1)
    assert [p.score for p in result.passages] == pytest.approx([0.8, 0.75])


async def test_passages_are_ordered_by_decreasing_score() -> None:
    # Le moteur renvoie les chunks dans le désordre : le classement est celui
    # du cas d'usage.
    rig = make_rig(
        ranking=[hit(3, 0.5), hit(1, 0.9), hit(2, 0.7)],
        published=[whole_unit(n) for n in (1, 2, 3)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert [p.unit_id for p in result.passages] == ids(1, 2, 3)


async def test_at_most_limit_passages_are_returned_even_if_more_survive() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 8)],
        published=[whole_unit(n) for n in range(1, 8)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert [p.unit_id for p in result.passages] == ids(1, 2, 3)
    assert result.complete is True


async def test_a_full_result_is_complete() -> None:
    rig = make_rig(
        ranking=[hit(n, 1 - n / 100) for n in range(1, 4)],
        published=[whole_unit(n) for n in range(1, 4)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=3))

    assert len(result.passages) == 3
    assert result.complete is True


async def test_a_unit_with_one_excellent_chunk_outranks_a_better_average() -> None:
    # Unité 1 : meilleur 0,9, moyenne 0,63 ; unité 2 : un chunk à 0,8. Classer
    # par moyenne mettrait l'unité 2 devant.
    rig = make_rig(
        ranking=[
            hit(1, 0.5, rank=0),
            hit(1, 0.9, rank=1),
            hit(2, 0.8, rank=0),
            hit(1, 0.5, rank=2),
        ],
        published=[whole_unit(1, chunk_count=3, token_count=90), whole_unit(2)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert [p.unit_id for p in result.passages] == ids(1, 2)


# --- provenance ---------------------------------------------------------------


async def test_every_passage_carries_the_full_citation_of_its_published_unit() -> None:
    rig = make_rig(
        ranking=[hit(1, 0.9), hit(2, 0.8)],
        published=[whole_unit(1), whole_unit(2)],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends", limit=2))

    assert [p.citation for p in result.passages] == [citation_of(1), citation_of(2)]
    first = result.passages[0].citation
    assert first.document_id == UUID(int=101)
    assert first.version_id == UUID(int=201)
    assert first.version_number == 3
    assert first.document_title == "Fiche FastAPI"
    assert first.source_filename == "fastapi.md"
    assert first.section_path.parts == ("Fiche FastAPI", "Section 1")
    assert (first.page_start, first.page_end) == (1, 2)


async def test_the_citation_comes_from_the_current_sql_state_not_from_qdrant() -> None:
    # Le moteur vectoriel ne rend que des identifiants : la citation d'un
    # passage est celle que PostgreSQL rend pour l'unité publiée.
    unit = whole_unit(1)
    rig = make_rig(ranking=[hit(1, 0.9)], published=[unit])

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    assert result.passages[0].citation is unit.citation


async def test_the_passage_exposes_the_type_and_the_root_question_of_the_unit() -> None:
    rig = make_rig(
        ranking=[hit(1, 0.9)],
        published=[whole_unit(1, question="Qu'est-ce que Depends ?")],
    )

    result = await rig.handler.handle(SearchKnowledge(text="depends"))

    passage = result.passages[0]
    assert passage.unit_type.value == "QA"
    assert passage.question == "Qu'est-ce que Depends ?"

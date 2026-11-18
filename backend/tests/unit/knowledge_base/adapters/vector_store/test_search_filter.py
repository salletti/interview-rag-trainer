"""Filtre Qdrant de la recherche (S5 ; knowledge-base.md §12 étape 3).

Le drapeau ``visible`` est la seule protection AVANT le top-k contre les
points obsolètes (§11) : il doit figurer dans chaque filtre, sans dépendre de
l'appelant. Le contrôle SQL final reste l'arbitre (tests d'intégration).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    search_filter,
)
from knowledge_base.domain.value_objects.search_filters import SearchFilters

VISIBLE = FieldCondition(key="visible", match=MatchValue(value=True))


def conditions(built: Filter, key: str) -> list[FieldCondition]:
    must = built.must
    assert isinstance(must, list)
    return [c for c in must if isinstance(c, FieldCondition) and c.key == key]


@pytest.mark.parametrize(
    "filters",
    [
        SearchFilters(),
        SearchFilters(language="fr"),
        SearchFilters(topics=("python",), kinds=("COURSE",)),
        SearchFilters(document_ids=(UUID(int=1),)),
    ],
    ids=["none", "language", "topics-kinds", "documents"],
)
def test_the_visible_flag_is_always_required(filters: SearchFilters) -> None:
    assert conditions(search_filter(filters, ()), "visible") == [VISIBLE]


def test_without_filters_only_visibility_is_required_and_nothing_excluded() -> None:
    built = search_filter(SearchFilters(), ())

    assert built.must == [VISIBLE]
    assert built.must_not is None


def test_every_topic_is_required() -> None:
    built = search_filter(SearchFilters(topics=("python", "asyncio")), ())

    assert conditions(built, "topics") == [
        FieldCondition(key="topics", match=MatchValue(value="python")),
        FieldCondition(key="topics", match=MatchValue(value="asyncio")),
    ]


def test_any_of_the_kinds_and_any_of_the_documents_match() -> None:
    document = UUID(int=7)
    built = search_filter(
        SearchFilters(kinds=("COURSE", "QA_SHEET"), document_ids=(document,)), ()
    )

    assert conditions(built, "kind") == [
        FieldCondition(key="kind", match=MatchAny(any=["COURSE", "QA_SHEET"]))
    ]
    assert conditions(built, "document_id") == [
        FieldCondition(key="document_id", match=MatchAny(any=[str(document)]))
    ]


def test_the_language_is_matched_exactly() -> None:
    built = search_filter(SearchFilters(language="en"), ())

    assert conditions(built, "language") == [
        FieldCondition(key="language", match=MatchValue(value="en"))
    ]


def test_excluded_units_are_in_must_not() -> None:
    excluded = [UUID(int=1), UUID(int=2)]

    built = search_filter(SearchFilters(), excluded)

    assert built.must_not == [
        FieldCondition(key="unit_id", match=MatchAny(any=[str(i) for i in excluded]))
    ]


def test_an_unknown_kind_is_refused_before_any_search() -> None:
    with pytest.raises(ValueError, match="type de document inconnu"):
        SearchFilters(kinds=("POEM",))

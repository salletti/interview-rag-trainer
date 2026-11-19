"""Métriques du jeu de référence (S5) : Recall@k, MRR, intégrité d'unité.

Une métrique fausse validerait n'importe quelle recherche : elles sont donc
vérifiées sur des classements écrits à la main, avec les valeurs calculées à
la main.
"""

from __future__ import annotations

import pytest

from tests.integration.knowledge_base.retrieval_support import (
    Expected,
    is_expected_unit,
    mean_reciprocal_rank,
    normalize,
    rank_of_expected,
    recall_at_k,
    unit_integrity,
    unit_is_intact,
)

EXPECTED = Expected(
    file="retrieval/qa_x.md",
    answer_snippet="Depends injecte une dépendance",
    question="Qu'est-ce que Depends ?",
    code_snippet="def get_db",
)


def test_recall_counts_the_queries_whose_rank_is_within_k() -> None:
    ranks = [1, 3, 5, 6, None]

    assert recall_at_k(ranks, 1) == pytest.approx(1 / 5)
    assert recall_at_k(ranks, 3) == pytest.approx(2 / 5)
    assert recall_at_k(ranks, 5) == pytest.approx(3 / 5)


def test_recall_at_k_includes_rank_k_itself_and_excludes_k_plus_one() -> None:
    assert recall_at_k([5], 5) == 1.0
    assert recall_at_k([6], 5) == 0.0


def test_a_query_without_rank_is_a_miss_for_every_k() -> None:
    assert recall_at_k([None, None], 100) == 0.0


def test_mrr_averages_the_reciprocal_ranks_and_scores_a_miss_as_zero() -> None:
    assert mean_reciprocal_rank([1, 2, None, 4]) == pytest.approx(
        (1 + 1 / 2 + 0 + 1 / 4) / 4
    )


def test_mrr_of_perfect_results_is_one() -> None:
    assert mean_reciprocal_rank([1, 1, 1]) == 1.0


@pytest.mark.parametrize("metric", [lambda r: recall_at_k(r, 5), mean_reciprocal_rank])
def test_a_metric_over_no_query_is_refused(metric) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        metric([])


def test_the_rank_is_the_first_passage_of_the_expected_unit() -> None:
    results = [
        ("autre.md", "Depends injecte une dépendance"),  # bon texte, mauvais fichier
        ("qa_x.md", "Rien à voir."),
        ("qa_x.md", "Réponse : Depends injecte une dépendance dans l'endpoint."),
        ("qa_x.md", "Depends injecte une dépendance"),
    ]

    assert rank_of_expected(results, EXPECTED) == 3


def test_the_rank_is_none_when_the_expected_unit_is_absent() -> None:
    assert rank_of_expected([("qa_x.md", "Rien.")], EXPECTED) is None
    assert rank_of_expected([], EXPECTED) is None


def test_matching_ignores_whitespace_layout_and_inline_code_marks() -> None:
    text = "Depends\ninjecte   une `dépendance`"

    assert is_expected_unit("qa_x.md", text, EXPECTED)
    assert normalize(text) == "Depends injecte une dépendance"


def test_a_passage_is_intact_with_question_answer_and_code() -> None:
    text = (
        "Qu'est-ce que Depends ?\n\nDepends injecte une dépendance.\n\n"
        "def get_db(): ..."
    )

    assert unit_is_intact(text, EXPECTED)


@pytest.mark.parametrize(
    "text",
    [
        "Depends injecte une dépendance. def get_db()",  # question absente
        "Qu'est-ce que Depends ? def get_db()",  # réponse absente
        "Qu'est-ce que Depends ? Depends injecte une dépendance",  # code absent
    ],
)
def test_a_passage_missing_one_expected_part_is_not_intact(text: str) -> None:
    assert not unit_is_intact(text, EXPECTED)


def test_a_unit_without_question_or_code_only_needs_its_answer() -> None:
    plain = Expected("a.md", "la réponse", None, None)

    assert unit_is_intact("voici la réponse", plain)
    assert not unit_is_intact("autre chose", plain)


def test_the_integrity_is_the_share_of_retrieved_units_that_are_intact() -> None:
    intact = "Qu'est-ce que Depends ? Depends injecte une dépendance def get_db"
    cut = "Depends injecte une dépendance"
    found = [(EXPECTED, intact), (EXPECTED, cut), (EXPECTED, None)]

    # La requête non retrouvée n'entre pas dans le dénominateur.
    assert unit_integrity(found) == pytest.approx(0.5)


def test_the_integrity_is_undefined_when_nothing_was_retrieved() -> None:
    assert unit_integrity([(EXPECTED, None)]) is None

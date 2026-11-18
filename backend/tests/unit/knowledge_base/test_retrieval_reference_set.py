"""Jeu de référence de l'évaluation S5 : cohérence des données (sans I/O réseau).

Un jeu mal formé fausserait toutes les mesures : on vérifie ici qu'il respecte
la roadmap (30 à 50 requêtes FR et EN, documents QA_SHEET et cours, requêtes
inter-langues) et que chaque attendu existe VRAIMENT dans le document source
(extrait de réponse, question, code, filtres satisfaits).
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from tests.integration.knowledge_base.retrieval_support import (
    REFERENCE,
    THRESHOLDS,
    Reference,
    ReferenceQuery,
    load_reference,
    normalize,
)

REF = load_reference()
KINDS = {"QA_SHEET", "COURSE", "REVISION_SHEET", "TECHNICAL"}


def source(file: str) -> str:
    return normalize((REF.document(file)).path.read_text(encoding="utf-8"))


def test_the_reference_has_between_30_and_50_queries() -> None:
    assert 30 <= len(REF.queries) <= 50


def test_query_ids_are_unique() -> None:
    assert len({q.id for q in REF.queries}) == len(REF.queries)


def test_both_languages_are_represented_by_the_queries_and_the_documents() -> None:
    assert {q.language for q in REF.queries} == {"fr", "en"}
    assert {d.language for d in REF.documents} == {"fr", "en"}


def test_the_documents_are_qa_sheets_and_courses() -> None:
    kinds = Counter(d.kind for d in REF.documents)

    assert kinds["QA_SHEET"] >= 4
    assert kinds["COURSE"] >= 2
    assert set(kinds) <= KINDS


def test_there_are_cross_language_queries_in_both_directions() -> None:
    tagged = Counter(t for q in REF.queries for t in q.tags)

    assert tagged["fr_to_en"] >= 5
    assert tagged["en_to_fr"] >= 3


@pytest.mark.parametrize("query", REF.queries, ids=lambda q: q.id)
def test_the_cross_language_tag_matches_the_languages(query: ReferenceQuery) -> None:
    document = REF.document(query.expected.file)

    assert query.cross_language == (query.language != document.language)


@pytest.mark.parametrize("query", REF.queries, ids=lambda q: q.id)
def test_the_expected_answer_exists_in_its_source_document(
    query: ReferenceQuery,
) -> None:
    assert normalize(query.expected.answer_snippet) in source(query.expected.file)


@pytest.mark.parametrize("query", REF.queries, ids=lambda q: q.id)
def test_the_expected_code_exists_in_its_source_document(query: ReferenceQuery) -> None:
    code = query.expected.code_snippet
    if code is not None:
        assert normalize(code) in source(query.expected.file)


@pytest.mark.parametrize(
    "query", [q for q in REF.queries if q.expected.question], ids=lambda q: q.id
)
def test_the_expected_question_is_a_heading_of_its_source_document(
    query: ReferenceQuery,
) -> None:
    text = REF.document(query.expected.file).path.read_text(encoding="utf-8")
    headings = [
        re.sub(r"^#+\s*(?:(?:Question|Q)\s*:\s*)?", "", line, flags=re.I).strip()
        for line in text.splitlines()
        if line.startswith("#")
    ]

    assert query.expected.question in headings


@pytest.mark.parametrize(
    "query", [q for q in REF.queries if q.filters is not None], ids=lambda q: q.id
)
def test_the_expected_document_satisfies_the_filters_of_its_query(
    query: ReferenceQuery,
) -> None:
    document = REF.document(query.expected.file)
    filters = query.filters
    assert filters is not None

    assert filters.language in (None, document.language)
    assert set(filters.topics) <= set(document.topics)
    assert not filters.kinds or document.kind in filters.kinds


def test_most_queries_have_a_filtered_variant() -> None:
    assert sum(1 for q in REF.queries if q.filters is not None) >= 20


def test_every_document_of_the_reference_is_the_target_of_a_query_or_a_distractor() -> (
    None
):
    """Un document sans requête sert de distracteur ; il en faut peu : le jeu
    doit couvrir presque tout son corpus."""
    targeted = {q.expected.file for q in REF.queries}
    distractors = [d.file for d in REF.documents if d.file not in targeted]

    assert len(distractors) <= 2


def test_the_queries_do_not_copy_the_expected_answer_verbatim() -> None:
    """Un jeu trop facile : la requête ne doit pas être l'extrait de réponse."""
    for query in REF.queries:
        assert normalize(query.text) not in normalize(query.expected.answer_snippet)
        assert normalize(query.expected.answer_snippet) not in normalize(query.text)


def test_the_reference_and_threshold_files_are_valid_json() -> None:
    assert json.loads(REFERENCE.read_text(encoding="utf-8"))["queries"]
    assert json.loads(THRESHOLDS.read_text(encoding="utf-8")) is not None


def test_documents_are_loaded_as_a_reference() -> None:
    assert isinstance(REF, Reference)

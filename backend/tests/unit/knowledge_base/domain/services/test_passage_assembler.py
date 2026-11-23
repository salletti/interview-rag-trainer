"""``assemble_passage`` sur des chunks produits par le VRAI ``ChunkPlanner``
(S5 ; knowledge-base.md §12 étape 6).

Les tests du cas d'usage (``test_search_knowledge_passages``) figent le
comportement observable sur des doubles ; ceux-ci vérifient l'accord avec le
format réel de l'en-tête contextuel (S3) : la fenêtre ne répète pas l'en-tête
de chaque chunk, entier ou tronqué, et garde la question une fois, en tête ; et
la règle de budget : la ligne de question compte, avec le compteur reçu.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import pytest

from knowledge_base.domain.services.chunk_planner import (
    TRUNCATION_MARK,
    strip_contextual_header,
)
from knowledge_base.domain.services.passage_assembler import (
    AssembledPassage,
    assemble_passage,
)
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.published_unit import (
    PublishedUnit,
    UnitChunk,
)
from knowledge_base.domain.value_objects.structured_document import render_blocks
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    base_citation,
    doc,
    h,
    make_unit,
    p,
    pipeline,
    plan,
    words,
)

QUESTION = "Pourquoi isoler chaque test ?"


def published(unit: KnowledgeUnit, chunks: tuple[DocumentChunk, ...]) -> PublishedUnit:
    return PublishedUnit(
        unit_id=unit.unit_id,
        unit_type=unit.unit_type,
        question_text=unit.question_text,
        text=render_blocks(unit.blocks),
        blocks_token_count=unit.token_count,
        chunks=tuple(
            UnitChunk(c.chunk_id, c.ordinal_in_unit, c.contextual_text, c.token_count)
            for c in chunks
        ),
        citation=chunks[0].citation,
    )


def long_qa_unit() -> tuple[PublishedUnit, tuple[DocumentChunk, ...]]:
    """Six paragraphes de 30 mots, un par chunk (budget de chunk 45)."""
    unit = make_unit(
        *(p(words(30, prefix=f"p{n}-")) for n in range(6)),
        unit_type=UnitType.QA,
        question=QUESTION,
        path=("Tests",),
    )
    chunks = plan(unit, max_tokens=45)
    assert len(chunks) == 6
    return published(unit, chunks), chunks


def hit(chunk: DocumentChunk, score: float) -> ChunkHit:
    return ChunkHit(chunk.chunk_id, chunk.unit_id, score)


def assemble(
    unit: PublishedUnit, hits: Sequence[ChunkHit], max_tokens: int
) -> AssembledPassage:
    return assemble_passage(unit, hits, max_tokens=max_tokens, token_counter=COUNTER)


def test_the_window_does_not_repeat_the_contextual_header_of_each_chunk() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble(unit, [hit(chunks[2], 0.9)], max_tokens=100)

    assert passage.truncated is True
    assert passage.text.count(QUESTION) == 1
    assert passage.text.startswith(f"Question : {QUESTION}\n\n")
    assert base_citation().document_title not in passage.text
    assert "p2-0 " in passage.text


def test_the_window_holds_whole_chunk_contents_in_reading_order() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble(unit, [hit(chunks[2], 0.9)], max_tokens=100)

    # 100 tokens : deux chunks de 40 ; le suivant d'abord, faute d'autre indice.
    assert words(30, prefix="p2-") in passage.text
    assert words(30, prefix="p3-") in passage.text
    assert "p1-" not in passage.text
    assert "p4-" not in passage.text
    assert passage.text.index("p2-0 ") < passage.text.index("p3-0 ")


def test_the_window_grows_towards_another_matched_chunk() -> None:
    # Meilleur chunk : 3 ; un autre chunk trouvé, 1, à deux rangs : la fenêtre
    # va vers lui (rang 2) plutôt que vers la suite (rang 4).
    unit, chunks = long_qa_unit()

    passage = assemble(unit, [hit(chunks[3], 0.9), hit(chunks[1], 0.5)], max_tokens=130)

    assert "p1-0 " in passage.text
    assert "p2-0 " in passage.text
    assert "p3-0 " in passage.text
    assert "p4-0 " not in passage.text
    assert passage.matched_chunk_ids == (chunks[3].chunk_id, chunks[1].chunk_id)


def test_a_single_chunk_larger_than_the_budget_is_returned_alone_cut_to_it() -> None:
    # Review indépendante, constat 2 : le texte rendu ne dépasse jamais le
    # budget ; le chunk trop grand est retenu seul puis tronqué aux mots.
    unit, chunks = long_qa_unit()

    passage = assemble(unit, [hit(chunks[4], 0.9)], max_tokens=20)

    assert passage.truncated is True
    assert COUNTER.count(passage.text) <= 20
    assert passage.text.endswith(f" {TRUNCATION_MARK}")
    assert "p4-0 " in passage.text
    assert "p3-" not in passage.text
    assert "p5-" not in passage.text
    assert passage.matched_chunk_ids == (chunks[4].chunk_id,)


def test_the_matched_chunks_outside_the_window_are_not_reported() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble(unit, [hit(chunks[5], 0.9), hit(chunks[0], 0.4)], max_tokens=50)

    assert passage.matched_chunk_ids == (chunks[5].chunk_id,)


def test_a_whole_unit_reports_every_matched_chunk_best_first() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble(
        unit, [hit(chunks[1], 0.4), hit(chunks[4], 0.8)], max_tokens=1_000
    )

    assert passage.truncated is False
    assert passage.matched_chunk_ids == (chunks[4].chunk_id, chunks[1].chunk_id)
    assert passage.text == f"Question : {QUESTION}\n\n{unit.text}"


def test_a_chunk_unknown_to_the_unit_is_a_loud_inconsistency() -> None:
    unit, _ = long_qa_unit()
    stranger = ChunkHit(UUID(int=999), unit.unit_id, 0.9)

    with pytest.raises(ValueError, match="absents de l'unité"):
        assemble(unit, [stranger], max_tokens=50)


def test_a_passage_needs_at_least_one_matched_chunk() -> None:
    unit, _ = long_qa_unit()

    with pytest.raises(ValueError):
        assemble(unit, [], max_tokens=50)


# --- budget : la question compte ----------------------------------------------


def question_line_tokens() -> int:
    return COUNTER.count(f"Question : {QUESTION}")


def test_a_unit_whose_blocks_fit_but_not_with_its_question_is_truncated() -> None:
    unit, chunks = long_qa_unit()
    budget = unit.blocks_token_count + question_line_tokens() - 1
    assert unit.blocks_token_count <= budget

    passage = assemble(unit, [hit(chunks[2], 0.9)], max_tokens=budget)

    assert passage.truncated is True


def test_a_unit_whose_blocks_and_question_fit_exactly_is_whole() -> None:
    unit, chunks = long_qa_unit()
    budget = unit.blocks_token_count + question_line_tokens()

    passage = assemble(unit, [hit(chunks[2], 0.9)], max_tokens=budget)

    assert passage.truncated is False


def test_the_question_is_counted_with_the_counter_it_receives() -> None:
    unit, chunks = long_qa_unit()
    budget = unit.blocks_token_count + question_line_tokens()

    class DoubleCounter:
        def count(self, text: str) -> int:
            return 2 * COUNTER.count(text)

    passage = assemble_passage(
        unit, [hit(chunks[2], 0.9)], max_tokens=budget, token_counter=DoubleCounter()
    )

    assert passage.truncated is True


def test_a_unit_without_question_is_budgeted_on_its_blocks_alone() -> None:
    unit = make_unit(
        *(p(words(30, prefix=f"s{n}-")) for n in range(3)), path=("Cours",)
    )
    chunks = plan(unit, max_tokens=45)
    section = published(unit, chunks)
    assert section.question_text is None

    passage = assemble(
        section, [hit(chunks[0], 0.9)], max_tokens=section.blocks_token_count
    )

    assert passage.truncated is False


# --- en-tête contextuel tronqué par le ChunkPlanner ---------------------------


LONG_QUESTION = " ".join(f"q{i}" for i in range(30)) + " ?"


def long_question_unit() -> tuple[PublishedUnit, tuple[DocumentChunk, ...]]:
    """En-tête de 35 mots pour un budget de chunk de 45 : le ``ChunkPlanner``
    le tronque à 22 mots (la moitié du budget), suivi de `` …`` ; six chunks
    de 37 tokens (90 de blocs, 123 avec la ligne de question de 33)."""
    unit = make_unit(
        *(p(words(15, prefix=f"p{n}-")) for n in range(6)),
        unit_type=UnitType.QA,
        question=LONG_QUESTION,
        path=("Tests",),
    )
    chunks = plan(unit, max_tokens=45)
    assert len(chunks) == 6
    assert all(TRUNCATION_MARK in c.contextual_text for c in chunks)
    return published(unit, chunks), chunks


def test_a_truncated_contextual_header_is_not_repeated_in_the_window() -> None:
    unit, chunks = long_question_unit()

    # 110 : la ligne de question (33 tokens) puis deux chunks de 37.
    passage = assemble(unit, [hit(chunks[1], 0.9), hit(chunks[2], 0.5)], max_tokens=110)

    assert passage.truncated is True
    assert "p1-0 " in passage.text
    assert "p2-0 " in passage.text
    # La question une seule fois, en tête ; ni titre ni marqueur de troncature.
    assert passage.text.startswith(f"Question : {LONG_QUESTION}\n\n")
    assert passage.text.count("q0 ") == 1
    assert TRUNCATION_MARK not in passage.text
    assert base_citation().document_title not in passage.text


def test_a_truncated_header_leaves_exactly_the_chunk_contents() -> None:
    unit, chunks = long_question_unit()

    # 70 : la ligne de question (33 tokens, moins de la moitié) puis un chunk.
    passage = assemble(unit, [hit(chunks[3], 0.9)], max_tokens=70)

    assert passage.text == (f"Question : {LONG_QUESTION}\n\n{words(15, prefix='p3-')}")


HEADER = "Titre\nSection\nQuestion : une longue question"


@pytest.mark.parametrize(
    ("contextual_text", "content"),
    [
        (f"{HEADER}\n\ncorps", "corps"),
        ("Titre\nSection\nQuestion : une …\n\ncorps", "corps"),
        ("Titre\nSec …\n\ncorps", "corps"),
        ("…\n\ncorps", "corps"),
        # Le marqueur dans le corps n'est pas un en-tête : texte inchangé.
        ("Autre en-tête\n\ncorps …\n\nsuite", "Autre en-tête\n\ncorps …\n\nsuite"),
    ],
    ids=["whole", "truncated-words", "truncated-early", "mark-only", "unknown"],
)
def test_the_contextual_header_is_stripped_whole_or_truncated(
    contextual_text: str, content: str
) -> None:
    assert strip_contextual_header(contextual_text, HEADER) == content


# --- budget : une question longue (review indépendante, constat 2) -------------
#
# Segmentation, chunking et assemblage réels, budget de chunk de production
# (508) et budget de passage proposé (1 024) : le ``ChunkPlanner`` a tronqué la
# question dans l'en-tête de chaque chunk, la fenêtre doit aussi la borner.

HUGE_QUESTION = words(5_000, prefix="q") + " ?"


def huge_question_unit(
    question: str = HUGE_QUESTION,
) -> tuple[PublishedUnit, tuple[DocumentChunk, ...]]:
    """Fiche Q/R : une question puis six paragraphes de 200 mots."""
    document = doc(
        h(1, "Fiche"),
        h(2, question),
        *(p(words(200, prefix=f"p{n}-")) for n in range(6)),
    )
    units, chunks = pipeline(document, DocumentKind.QA_SHEET, max_tokens=508)
    (unit,) = units
    assert unit.question_text == question
    return published(unit, chunks), chunks


def test_a_question_longer_than_the_budget_leaves_the_passage_within_it() -> None:
    unit, chunks = huge_question_unit()
    assert COUNTER.count(HUGE_QUESTION) == 5_001

    passage = assemble(unit, [hit(chunks[1], 0.9)], max_tokens=1_024)

    assert passage.truncated is True
    assert COUNTER.count(passage.text) <= 1_024
    assert passage.text.startswith("Question : q0 q1 ")
    assert words(200, prefix="p1-") in passage.text  # le chunk trouvé reste
    assert passage.matched_chunk_ids == (chunks[1].chunk_id,)


def test_a_question_over_half_the_budget_is_cut_at_words_with_a_mark() -> None:
    unit, chunks = huge_question_unit()

    passage = assemble(unit, [hit(chunks[1], 0.9)], max_tokens=1_024)

    question_line, _, _ = passage.text.partition("\n\n")
    assert question_line.endswith(f" {TRUNCATION_MARK}")
    assert COUNTER.count(question_line) <= 1_024 // 2
    assert "q4999" not in passage.text


def test_the_truncation_of_a_long_question_is_deterministic() -> None:
    unit, chunks = huge_question_unit()
    hits = [hit(chunks[3], 0.9)]

    assert assemble(unit, hits, max_tokens=1_024) == assemble(
        unit, hits, max_tokens=1_024
    )


def test_a_question_within_half_the_budget_is_kept_whole_in_the_window() -> None:
    question = words(300, prefix="q") + " ?"
    unit, chunks = huge_question_unit(question)

    passage = assemble(unit, [hit(chunks[1], 0.9)], max_tokens=1_024)

    assert passage.truncated is True
    assert passage.text.startswith(f"Question : {question}\n\n")
    assert COUNTER.count(passage.text) <= 1_024


@pytest.mark.parametrize("budget", [600, 1_024, 2_048])
def test_the_window_and_its_question_never_exceed_the_budget(budget: int) -> None:
    unit, chunks = huge_question_unit(words(400, prefix="q") + " ?")

    for matched in chunks:
        passage = assemble(unit, [hit(matched, 0.9)], max_tokens=budget)
        assert COUNTER.count(passage.text) <= budget, matched.ordinal_in_unit

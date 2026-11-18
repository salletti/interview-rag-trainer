"""``assemble_passage`` sur des chunks produits par le VRAI ``ChunkPlanner``
(S5 ; knowledge-base.md §12 étape 6).

Les tests du cas d'usage (``test_search_knowledge_passages``) figent le
comportement observable sur des doubles ; ceux-ci vérifient l'accord avec le
format réel de l'en-tête contextuel (S3) : la fenêtre ne répète pas l'en-tête
de chaque chunk, et garde la question une fois, en tête.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.services.passage_assembler import assemble_passage
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.published_unit import (
    PublishedUnit,
    UnitChunk,
)
from knowledge_base.domain.value_objects.structured_document import render_blocks
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    base_citation,
    make_unit,
    p,
    plan,
    words,
)

QUESTION = "Pourquoi isoler chaque test ?"


def published(unit: KnowledgeUnit, chunks: tuple[DocumentChunk, ...]) -> PublishedUnit:
    question_tokens = (
        0 if unit.question_text is None else COUNTER.count(unit.question_text) + 2
    )
    return PublishedUnit(
        unit_id=unit.unit_id,
        unit_type=unit.unit_type,
        question_text=unit.question_text,
        text=render_blocks(unit.blocks),
        token_count=unit.token_count + question_tokens,
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


def test_the_window_does_not_repeat_the_contextual_header_of_each_chunk() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble_passage(unit, [hit(chunks[2], 0.9)], max_tokens=100)

    assert passage.truncated is True
    assert passage.text.count(QUESTION) == 1
    assert passage.text.startswith(f"Question : {QUESTION}\n\n")
    assert base_citation().document_title not in passage.text
    assert "p2-0 " in passage.text


def test_the_window_holds_whole_chunk_contents_in_reading_order() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble_passage(unit, [hit(chunks[2], 0.9)], max_tokens=100)

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

    passage = assemble_passage(
        unit, [hit(chunks[3], 0.9), hit(chunks[1], 0.5)], max_tokens=130
    )

    assert "p1-0 " in passage.text
    assert "p2-0 " in passage.text
    assert "p3-0 " in passage.text
    assert "p4-0 " not in passage.text
    assert passage.matched_chunk_ids == (chunks[3].chunk_id, chunks[1].chunk_id)


def test_a_single_chunk_larger_than_the_budget_is_returned_alone() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble_passage(unit, [hit(chunks[4], 0.9)], max_tokens=10)

    assert passage.truncated is True
    assert words(30, prefix="p4-") in passage.text
    assert "p3-" not in passage.text
    assert "p5-" not in passage.text
    assert passage.matched_chunk_ids == (chunks[4].chunk_id,)


def test_the_matched_chunks_outside_the_window_are_not_reported() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble_passage(
        unit, [hit(chunks[5], 0.9), hit(chunks[0], 0.4)], max_tokens=50
    )

    assert passage.matched_chunk_ids == (chunks[5].chunk_id,)


def test_a_whole_unit_reports_every_matched_chunk_best_first() -> None:
    unit, chunks = long_qa_unit()

    passage = assemble_passage(
        unit, [hit(chunks[1], 0.4), hit(chunks[4], 0.8)], max_tokens=1_000
    )

    assert passage.truncated is False
    assert passage.matched_chunk_ids == (chunks[4].chunk_id, chunks[1].chunk_id)
    assert passage.text == f"Question : {QUESTION}\n\n{unit.text}"


def test_a_chunk_unknown_to_the_unit_is_a_loud_inconsistency() -> None:
    unit, _ = long_qa_unit()
    stranger = ChunkHit(UUID(int=999), unit.unit_id, 0.9)

    with pytest.raises(ValueError, match="absents de l'unité"):
        assemble_passage(unit, [stranger], max_tokens=50)


def test_a_passage_needs_at_least_one_matched_chunk() -> None:
    unit, _ = long_qa_unit()

    with pytest.raises(ValueError):
        assemble_passage(unit, [], max_tokens=50)

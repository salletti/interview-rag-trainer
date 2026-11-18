"""Fallbacks du ``ChunkPlanner`` non figés par ``test_chunk_planner`` (choix de
S3, documentés dans le README du contexte) :

- paragraphe seul plus grand que le budget : découpe aux phrases, puis aux
  mots ; liste : aux éléments, puis aux mots ; titre : aux mots ;
- tout fragment sauf le premier porte le marqueur de continuation, comme pour
  le code et les tableaux ;
- en-tête plus long que la moitié du budget : tronqué aux mots, suivi de
  ``…`` ; le budget n'est jamais dépassé ;
- définitions : les décorateurs restent avec leur ``def`` ;
- ordre des fallbacks de code : celui de la politique ;
- overlap > 0 : refusé explicitement (mesuré en S5/S9) ;
- budget sans place pour le contenu : ``ChunkBudgetError``.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.services.chunk_planner import (
    CONTINUATION_MARKER,
    TRUNCATION_MARK,
    ChunkBudgetError,
    ChunkPlanner,
)
from knowledge_base.domain.value_objects.chunking_policy import (
    ChunkingPolicy,
    CodeFallback,
)
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.structured_document import (
    Heading,
    ListBlock,
    Paragraph,
)
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    POLICY_VERSION,
    TITLE,
    base_citation,
    code,
    header,
    make_unit,
    p,
    plan,
    words,
)

HEAD = header(TITLE, ("Cours",))  # « Python avancé\nCours » : 3 tokens


def bodies(chunks: tuple[DocumentChunk, ...]) -> list[str]:
    return [c.contextual_text.removeprefix(f"{HEAD}\n\n") for c in chunks]


def without_marker(body: str) -> str:
    return body.removeprefix(f"{CONTINUATION_MARKER}\n")


def assert_within_budget(chunks: tuple[DocumentChunk, ...], budget: int) -> None:
    for chunk in chunks:
        assert chunk.token_count == COUNTER.count(chunk.contextual_text) <= budget


def test_an_oversized_paragraph_is_cut_between_sentences() -> None:
    sentences = [f"{words(8, f's{i}_')}." for i in range(4)]
    unit = make_unit(p(" ".join(sentences)))
    budget = 3 + 1 + 8 * 2  # en-tête, marqueur, deux phrases

    chunks = plan(unit, max_tokens=budget)

    assert [without_marker(b) for b in bodies(chunks)] == [
        " ".join(sentences[0:2]),
        " ".join(sentences[2:4]),
    ]
    assert bodies(chunks)[1].startswith(f"{CONTINUATION_MARKER}\n")
    assert_within_budget(chunks, budget)


def test_a_sentence_longer_than_the_budget_is_cut_between_words() -> None:
    unit = make_unit(p(words(50)))

    chunks = plan(unit, max_tokens=20)

    assert len(chunks) > 2
    rebuilt = " ".join(without_marker(b) for b in bodies(chunks))
    assert rebuilt == words(50)
    assert_within_budget(chunks, 20)


def test_an_oversized_list_is_cut_between_items() -> None:
    items = tuple(words(4, f"i{n}_") for n in range(6))
    unit = make_unit(ListBlock(items=items))
    budget = 3 + 1 + 2 * 5  # deux éléments « - » + 4 mots

    chunks = plan(unit, max_tokens=budget)

    assert [without_marker(b) for b in bodies(chunks)] == [
        f"- {items[0]}\n- {items[1]}",
        f"- {items[2]}\n- {items[3]}",
        f"- {items[4]}\n- {items[5]}",
    ]
    assert_within_budget(chunks, budget)


def test_an_oversized_heading_is_cut_between_words_without_loss() -> None:
    unit = make_unit(Heading(level=3, text=words(30)), p("fin"))

    chunks = plan(unit, max_tokens=15)

    assert " ".join(without_marker(b) for b in bodies(chunks)[:-1]) == (
        f"### {words(30)}"
    )
    assert bodies(chunks)[-1] == "fin"
    assert_within_budget(chunks, 15)


def test_a_header_over_half_the_budget_is_truncated_and_the_budget_holds() -> None:
    long_question = words(40, "q")
    unit = make_unit(
        p(words(5, "a")), unit_type=UnitType.QA, question=long_question, path=()
    )

    (chunk,) = plan(unit, max_tokens=30)

    head, _, body = chunk.contextual_text.partition("\n\n")
    assert head.startswith(f"{TITLE}\nQuestion : q0 q1")
    assert head.endswith(f" {TRUNCATION_MARK}")
    assert COUNTER.count(head) <= 15
    assert body == words(5, "a")
    assert chunk.token_count <= 30


def test_a_header_within_half_the_budget_is_kept_whole() -> None:
    # En-tête : 2 (titre) + 2 (« Question : ») + 10 = 14 tokens = 28 / 2.
    unit = make_unit(p("x"), unit_type=UnitType.QA, question=words(10, "q"), path=())

    (chunk,) = plan(unit, max_tokens=28)

    assert chunk.contextual_text == f"{TITLE}\nQuestion : {words(10, 'q')}\n\nx"


def test_decorators_stay_with_the_definition_they_decorate() -> None:
    groups = [
        [
            "@app.get(x)",
            f"def route_{i}(x):",
            *[f"    s_{i}_{j} = f(x)" for j in range(4)],
        ]
        for i in range(3)
    ]
    unit = make_unit(code("\n".join(line for g in groups for line in g)))
    # Un groupe : 1 + 2 + 4 x 4 mots = 19, + barrières (2) + marqueur + marge.
    budget = 3 + 19 + 2 + 1 + 3

    chunks = plan(unit, max_tokens=budget)

    assert len(chunks) == 3
    for chunk, group in zip(chunks, groups, strict=True):
        assert [
            line for line in chunk.contextual_text.splitlines() if line in group
        ] == (group)


def test_the_policy_decides_the_order_of_the_code_fallbacks() -> None:
    source = "a1 = 1\na2 = 2\n\nb1 = 1\nb2 = 2\nb3 = 3"
    unit = make_unit(code(source))
    budget = 3 + 2 + 1 + 9  # en-tête, barrières, marqueur, trois lignes

    def first_fragment(fallbacks: tuple[CodeFallback, ...]) -> str:
        policy = ChunkingPolicy(
            version=POLICY_VERSION, max_tokens=budget, fallbacks=fallbacks
        )
        chunks = ChunkPlanner(COUNTER, policy).plan(unit, base_citation())
        assert all(c.token_count <= budget for c in chunks)
        return chunks[0].contextual_text

    assert "b1 = 1" not in first_fragment(
        (CodeFallback.BLANK_LINES, CodeFallback.LINES)
    )
    assert "b1 = 1" in first_fragment((CodeFallback.LINES,))


def test_a_policy_with_overlap_is_refused() -> None:
    with pytest.raises(ValueError, match="overlap"):
        ChunkPlanner(
            COUNTER, ChunkingPolicy(version="v", max_tokens=100, overlap_tokens=10)
        )


def test_a_policy_with_a_repeated_fallback_is_refused() -> None:
    with pytest.raises(ValueError, match="fallback"):
        ChunkingPolicy(
            version="v",
            max_tokens=100,
            fallbacks=(CodeFallback.LINES, CodeFallback.LINES),
        )


def test_a_budget_without_room_for_content_is_an_explicit_error() -> None:
    unit = make_unit(code("x = 1"), path=())

    with pytest.raises(ChunkBudgetError):
        plan(unit, max_tokens=4)


def test_paragraphs_that_fit_are_never_cut() -> None:
    unit = make_unit(p(words(10, "a")), p(words(10, "b")))

    chunks = plan(unit, max_tokens=3 + 10)

    assert bodies(chunks) == [words(10, "a"), words(10, "b")]
    assert all(isinstance(b, Paragraph) for b in unit.blocks)
